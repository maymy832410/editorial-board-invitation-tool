"""OpenAlex API client for searching authors."""

import os
import time
from typing import Any, Generator, Optional
import requests

from config import OPENALEX_BASE_URL, OPENALEX_EMAIL


class OpenAlexRequestError(Exception):
    """Raised when an OpenAlex request fails after retry attempts."""


class OpenAlexRateLimitError(OpenAlexRequestError):
    """Raised when OpenAlex blocks requests due to rate limit or exhausted budget."""

    def __init__(self, message: str, retry_after_seconds: Optional[int] = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _normalize_orcid(orcid_value: str) -> str:
    """Normalize ORCID inputs to bare identifier format."""
    normalized = (orcid_value or "").strip().lower()
    if not normalized:
        return ""
    return normalized.replace("https://orcid.org/", "").replace("http://orcid.org/", "")


def _read_env_int(name: str, default: int, minimum: int) -> int:
    """Read an integer env var with fallback and lower bound."""
    raw_value = (os.environ.get(name, "") or "").strip()
    if not raw_value:
        return max(int(default), minimum)
    try:
        return max(int(raw_value), minimum)
    except ValueError:
        return max(int(default), minimum)


def _read_env_float(name: str, default: float, minimum: float) -> float:
    """Read a float env var with fallback and lower bound."""
    raw_value = (os.environ.get(name, "") or "").strip()
    if not raw_value:
        return max(float(default), minimum)
    try:
        return max(float(raw_value), minimum)
    except ValueError:
        return max(float(default), minimum)


class OpenAlexClient:
    """Client for querying OpenAlex API for author data."""
    
    def __init__(self, email: str = OPENALEX_EMAIL):
        self.email = email
        self.base_url = OPENALEX_BASE_URL
        self.max_retries = _read_env_int("OPENALEX_MAX_RETRIES", 3, minimum=1)
        self.request_timeout_seconds = _read_env_int("OPENALEX_REQUEST_TIMEOUT_SEC", 30, minimum=5)
        self.backoff_base_seconds = _read_env_float("OPENALEX_BACKOFF_BASE_SEC", 2.0, minimum=0.1)
        self.request_pause_seconds = _read_env_float("OPENALEX_REQUEST_PAUSE_SEC", 0.1, minimum=0.0)
        self.request_stats: dict[str, int] = {
            "requests": 0,
            "rate_limited": 0,
            "server_errors": 0,
            "network_errors": 0,
            "retry_exhausted": 0,
        }
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"AuthorEmailFinder/1.0 (mailto:{email})"
        })

    def _pause_between_requests(self) -> None:
        """Apply a configurable delay between OpenAlex requests."""
        if self.request_pause_seconds > 0:
            time.sleep(self.request_pause_seconds)
    
    def _make_request(self, endpoint: str, params: dict, max_retries: Optional[int] = None) -> dict:
        """Make a request with retry logic and exponential backoff."""
        retries = max(1, int(max_retries)) if max_retries is not None else self.max_retries
        params["mailto"] = self.email
        url = f"{self.base_url}/{endpoint}"
        
        for attempt in range(retries):
            try:
                self.request_stats["requests"] += 1
                response = self.session.get(url, params=params, timeout=self.request_timeout_seconds)
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    self.request_stats["rate_limited"] += 1

                    retry_after_seconds: Optional[int] = None
                    retry_after_raw = (response.headers.get("Retry-After") or "").strip()
                    if retry_after_raw:
                        try:
                            retry_after_seconds = max(int(float(retry_after_raw)), 0)
                        except ValueError:
                            retry_after_seconds = None

                    payload: dict[str, Any] = {}
                    try:
                        payload_candidate = response.json()
                        if isinstance(payload_candidate, dict):
                            payload = payload_candidate
                            payload_retry_after = payload.get("retryAfter")
                            if retry_after_seconds is None and payload_retry_after is not None:
                                try:
                                    retry_after_seconds = max(int(payload_retry_after), 0)
                                except (TypeError, ValueError):
                                    retry_after_seconds = None
                    except ValueError:
                        payload = {}

                    if attempt < retries - 1:
                        wait_time = (
                            float(retry_after_seconds)
                            if retry_after_seconds is not None
                            else (2 ** attempt) * self.backoff_base_seconds
                        )
                        time.sleep(max(wait_time, self.backoff_base_seconds))
                        continue

                    self.request_stats["retry_exhausted"] += 1
                    detail = (payload.get("message") or payload.get("error") or "").strip()
                    if detail:
                        raise OpenAlexRateLimitError(
                            f"OpenAlex rate limit exceeded: {detail}",
                            retry_after_seconds=retry_after_seconds,
                        )
                    raise OpenAlexRateLimitError(
                        "OpenAlex rate limit exceeded",
                        retry_after_seconds=retry_after_seconds,
                    )
                elif response.status_code >= 500:
                    # Server error - retry
                    self.request_stats["server_errors"] += 1
                    wait_time = (2 ** attempt) * self.backoff_base_seconds
                    time.sleep(wait_time)
                    continue
                else:
                    response.raise_for_status()
                    
            except requests.exceptions.RequestException as e:
                self.request_stats["network_errors"] += 1
                if attempt < retries - 1:
                    wait_time = (2 ** attempt) * self.backoff_base_seconds
                    time.sleep(wait_time)
                    continue
                self.request_stats["retry_exhausted"] += 1
                raise e

        self.request_stats["retry_exhausted"] += 1
        
        raise Exception(f"Failed to fetch data after {retries} retries")
    
    def search_topics(
        self, 
        keywords: list[str], 
        max_per_keyword: int = 3,
        max_total: int = 25
    ) -> tuple[list[str], list[dict]]:
        """
        Search for topic IDs matching keywords.
        
        Args:
            keywords: List of keywords to search for
            max_per_keyword: Maximum topics to return per keyword
            max_total: Maximum total topic IDs to return (OpenAlex has filter limits)
            
        Returns:
            Tuple of (list of topic IDs, list of topic details for display)
        """
        topic_ids = set()
        topic_details = []
        
        for keyword in keywords:
            keyword = keyword.strip()
            if not keyword:
                continue
            
            # Stop if we've reached the total limit
            if len(topic_ids) >= max_total:
                break
            
            params = {
                "search": keyword,
                "per_page": max_per_keyword
            }
            
            try:
                data = self._make_request("topics", params)
                
                for topic in data.get("results", []):
                    if len(topic_ids) >= max_total:
                        break
                    
                    topic_id = topic.get("id", "").split("/")[-1]
                    if topic_id and topic_id not in topic_ids:
                        topic_ids.add(topic_id)
                        topic_details.append({
                            "id": topic_id,
                            "name": topic.get("display_name", ""),
                            "subfield": (topic.get("subfield") or {}).get("display_name", ""),
                            "field": (topic.get("field") or {}).get("display_name", ""),
                            "works_count": topic.get("works_count", 0),
                            "keyword": keyword
                        })
                
                # Small delay between keyword searches
                self._pause_between_requests()
                
            except Exception:
                # Continue with other keywords if one fails
                continue
        
        return list(topic_ids), topic_details

    def search_topic_suggestions(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        """Return stable OpenAlex topic suggestions for the collection UI."""
        term = (query or "").strip()
        if len(term) < 2:
            return []
        data = self._make_request(
            "topics",
            {"search": term, "per_page": max(1, min(int(limit), 25))},
        )
        suggestions = []
        for topic in data.get("results", []):
            topic_id = (topic.get("id") or "").rstrip("/").split("/")[-1]
            name = topic.get("display_name") or ""
            if not topic_id or not name:
                continue
            suggestions.append({
                "id": topic_id,
                "name": name,
                "subfield": (topic.get("subfield") or {}).get("display_name", ""),
                "field": (topic.get("field") or {}).get("display_name", ""),
                "works_count": int(topic.get("works_count") or 0),
            })
        return suggestions
    
    def build_filter(
        self,
        h_index_min: Optional[int] = None,
        h_index_max: Optional[int] = None,
        exclude_country_codes: Optional[list[str]] = None,
        topic_ids: Optional[list[str]] = None,
        require_orcid: bool = True
    ) -> str:
        """Build the filter string for OpenAlex query."""
        filters = []
        
        if h_index_min is not None:
            filters.append(f"summary_stats.h_index:>{h_index_min - 1}")
        
        if h_index_max is not None:
            filters.append(f"summary_stats.h_index:<{h_index_max + 1}")
        
        if exclude_country_codes:
            # Negate each country code with ! and join with pipe (OR negation)
            negated = "|".join(f"!{code}" for code in exclude_country_codes)
            filters.append(f"last_known_institutions.country_code:{negated}")
        
        if topic_ids:
            # Use OR operator (pipe) to match any of the topics
            topic_filter = "|".join(topic_ids)
            filters.append(f"topics.id:{topic_filter}")
        
        if require_orcid:
            filters.append("has_orcid:true")
        
        return ",".join(filters)
    
    def search_authors(
        self,
        h_index_min: Optional[int] = None,
        h_index_max: Optional[int] = None,
        exclude_country_codes: Optional[list[str]] = None,
        topic_ids: Optional[list[str]] = None,
        require_orcid: bool = True,
        max_results: int = 1000,
        per_page: int = 200
    ) -> Generator[dict, None, None]:
        """
        Search for authors with specified filters.
        
        Args:
            h_index_min: Minimum h-index
            h_index_max: Maximum h-index
            exclude_country_codes: List of country codes to exclude
            topic_ids: List of OpenAlex topic IDs to filter by (OR logic)
            require_orcid: Only return authors with ORCID
            max_results: Maximum number of results to return
            per_page: Results per API page
        
        Yields author records one by one, handling pagination automatically.
        """
        filter_str = self.build_filter(
            h_index_min=h_index_min,
            h_index_max=h_index_max,
            exclude_country_codes=exclude_country_codes,
            topic_ids=topic_ids,
            require_orcid=require_orcid
        )
        
        cursor = "*"
        total_yielded = 0
        
        while cursor and total_yielded < max_results:
            params = {
                "filter": filter_str,
                "select": "id,display_name,orcid,summary_stats,last_known_institutions,works_count,cited_by_count,topics",
                "per_page": min(per_page, max_results - total_yielded),
                "cursor": cursor
            }
            
            data = self._make_request("authors", params)
            
            results = data.get("results", [])
            if not results:
                break
            
            for author in results:
                yield self._parse_author(author)
                total_yielded += 1
                
                if total_yielded >= max_results:
                    break
            
            # Get next cursor for pagination
            cursor = data.get("meta", {}).get("next_cursor")
            
            # Small delay to be polite
            self._pause_between_requests()

    def fetch_author_batch(
        self,
        h_index_min: Optional[int] = None,
        h_index_max: Optional[int] = None,
        exclude_country_codes: Optional[list[str]] = None,
        topic_ids: Optional[list[str]] = None,
        require_orcid: bool = True,
        cursor: str = "*",
        batch_size: int = 250,
        batch_index: int = 0,
    ) -> dict[str, Any]:
        """Fetch a single cursor-based batch of authors with pagination metadata."""
        filter_str = self.build_filter(
            h_index_min=h_index_min,
            h_index_max=h_index_max,
            exclude_country_codes=exclude_country_codes,
            topic_ids=topic_ids,
            require_orcid=require_orcid,
        )

        params = {
            "filter": filter_str,
            "select": "id,display_name,orcid,summary_stats,last_known_institutions,works_count,cited_by_count,topics",
            "per_page": min(max(batch_size, 1), 200),
            "cursor": cursor or "*",
        }

        data = self._make_request("authors", params)
        results = data.get("results", [])
        parsed_results = [self._parse_author(author) for author in results]
        next_cursor = data.get("meta", {}).get("next_cursor")
        start_index = batch_index * batch_size
        end_index = start_index + len(parsed_results)

        self._pause_between_requests()

        return {
            "results": parsed_results,
            "next_cursor": next_cursor,
            "batch_index": batch_index,
            "batch_size": batch_size,
            "count": len(parsed_results),
            "start_index": start_index,
            "end_index": end_index,
            "has_more": bool(parsed_results) and bool(next_cursor),
        }

    def fetch_recent_works(self, author_id: str, limit: int = 3) -> list[dict[str, Any]]:
        """Fetch recent publications for one author using their OpenAlex author ID."""
        if not author_id:
            return []

        normalized_author_id = str(author_id).rstrip("/").split("/")[-1]
        params = {
            "filter": f"authorships.author.id:{normalized_author_id}",
            "sort": "publication_date:desc",
            "select": "id,title,display_name,publication_year,publication_date,primary_location,doi",
            "per_page": max(1, min(int(limit or 3), 10)),
        }

        try:
            data = self._make_request("works", params)
        except Exception:
            return []

        works = []
        for work in data.get("results", []):
            title = work.get("title") or work.get("display_name") or ""
            if not title:
                continue

            primary_location = work.get("primary_location") or {}
            source = primary_location.get("source") or {}
            works.append({
                "id": work.get("id", ""),
                "title": title,
                "year": str(work.get("publication_year") or ""),
                "publication_date": work.get("publication_date") or "",
                "source": source.get("display_name") or "",
                "doi": work.get("doi") or "",
            })

        self._pause_between_requests()
        return works

    def fetch_author_by_orcid(self, orcid_id: str) -> Optional[dict[str, Any]]:
        """Fetch a single author record by ORCID with strict matching."""
        normalized_orcid = _normalize_orcid(orcid_id)
        if not normalized_orcid:
            return None

        canonical_orcid_url = f"https://orcid.org/{normalized_orcid}"
        params = {
            "filter": f"orcid:{canonical_orcid_url}",
            "select": "id,display_name,orcid,summary_stats,last_known_institutions,works_count,cited_by_count,topics",
            "per_page": 1,
        }

        try:
            data = self._make_request("authors", params)
        except OpenAlexRateLimitError:
            raise
        except Exception as exc:
            raise OpenAlexRequestError(
                f"OpenAlex ORCID lookup failed for {normalized_orcid}"
            ) from exc

        results = data.get("results", [])
        if not results:
            return None

        return self._parse_author(results[0])
    
    def _parse_author(self, author: dict) -> dict:
        """Parse author data into a cleaner format."""
        
        # Extract ORCID ID from URL
        orcid_url = author.get("orcid")
        orcid_id = None
        if orcid_url:
            orcid_id = orcid_url.split("/")[-1]
        
        # Get institution info
        institution_name = None
        institution_country = None
        institutions = author.get("last_known_institutions", [])
        if institutions:
            inst = institutions[0]
            institution_name = inst.get("display_name")
            institution_country = inst.get("country_code")
        
        # Get h-index
        summary_stats = author.get("summary_stats", {})
        h_index = summary_stats.get("h_index")
        
        # Get topics/research areas
        topics = author.get("topics", [])
        
        # Extract all topic names for filtering
        all_topics = [t.get("display_name") for t in topics if t.get("display_name")]
        
        # Get primary specialty (most specific topic)
        specialty = None
        subfield = None
        if topics:
            first_topic = topics[0]
            specialty = first_topic.get("display_name")
            # Get subfield from the topic structure
            subfield_data = first_topic.get("subfield", {})
            if subfield_data:
                subfield = subfield_data.get("display_name")
        
        # Get top 3 topics for display
        top_topics = all_topics[:3] if all_topics else []
        
        # Get discipline from OpenAlex field.display_name (most frequent across topics)
        field_counts = {}
        for t in topics:
            field_data = t.get("field", {})
            field_name = field_data.get("display_name")
            if field_name:
                field_counts[field_name] = field_counts.get(field_name, 0) + 1
        if field_counts:
            discipline = max(field_counts, key=field_counts.get)
        else:
            discipline = "Other"
        
        return {
            "author_id": author.get("id"),
            "name": author.get("display_name"),
            "orcid_id": orcid_id,
            "orcid_url": orcid_url,
            "h_index": h_index,
            "works_count": author.get("works_count"),
            "cited_by_count": author.get("cited_by_count"),
            "institution": institution_name,
            "country": institution_country,
            "discipline": discipline,
            "specialty": specialty,
            "subfield": subfield,
            "all_topics": all_topics,
            "research_areas": ", ".join(top_topics) if top_topics else None,
        }
    
    def get_total_count(
        self,
        h_index_min: Optional[int] = None,
        h_index_max: Optional[int] = None,
        exclude_country_codes: Optional[list[str]] = None,
        topic_ids: Optional[list[str]] = None,
        require_orcid: bool = True
    ) -> int:
        """Get total count of authors matching filters without fetching all data."""
        filter_str = self.build_filter(
            h_index_min=h_index_min,
            h_index_max=h_index_max,
            exclude_country_codes=exclude_country_codes,
            topic_ids=topic_ids,
            require_orcid=require_orcid
        )
        
        params = {
            "filter": filter_str,
            "per_page": 1
        }
        
        data = self._make_request("authors", params)
        return data.get("meta", {}).get("count", 0)
