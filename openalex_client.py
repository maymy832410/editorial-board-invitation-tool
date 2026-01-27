"""OpenAlex API client for searching authors."""

import time
from typing import Generator, Optional
import requests

from config import OPENALEX_BASE_URL, OPENALEX_EMAIL


class OpenAlexClient:
    """Client for querying OpenAlex API for author data."""
    
    def __init__(self, email: str = OPENALEX_EMAIL):
        self.email = email
        self.base_url = OPENALEX_BASE_URL
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"AuthorEmailFinder/1.0 (mailto:{email})"
        })
    
    def _make_request(self, endpoint: str, params: dict, max_retries: int = 3) -> dict:
        """Make a request with retry logic and exponential backoff."""
        params["mailto"] = self.email
        url = f"{self.base_url}/{endpoint}"
        
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, params=params, timeout=30)
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    # Rate limited - wait and retry
                    wait_time = (2 ** attempt) * 2
                    time.sleep(wait_time)
                    continue
                elif response.status_code >= 500:
                    # Server error - retry
                    wait_time = (2 ** attempt) * 2
                    time.sleep(wait_time)
                    continue
                else:
                    response.raise_for_status()
                    
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) * 2
                    time.sleep(wait_time)
                    continue
                raise e
        
        raise Exception(f"Failed to fetch data after {max_retries} retries")
    
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
                            "works_count": topic.get("works_count", 0),
                            "keyword": keyword
                        })
                
                # Small delay between keyword searches
                time.sleep(0.1)
                
            except Exception:
                # Continue with other keywords if one fails
                continue
        
        return list(topic_ids), topic_details
    
    def build_filter(
        self,
        h_index_min: Optional[int] = None,
        h_index_max: Optional[int] = None,
        country_codes: Optional[list[str]] = None,
        topic_ids: Optional[list[str]] = None,
        require_orcid: bool = True
    ) -> str:
        """Build the filter string for OpenAlex query."""
        filters = []
        
        if h_index_min is not None:
            filters.append(f"summary_stats.h_index:>{h_index_min - 1}")
        
        if h_index_max is not None:
            filters.append(f"summary_stats.h_index:<{h_index_max + 1}")
        
        if country_codes:
            # OpenAlex uses pipe for OR within a filter
            country_filter = "|".join(country_codes)
            filters.append(f"last_known_institutions.country_code:{country_filter}")
        
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
        country_codes: Optional[list[str]] = None,
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
            country_codes: List of country codes to filter by
            topic_ids: List of OpenAlex topic IDs to filter by (OR logic)
            require_orcid: Only return authors with ORCID
            max_results: Maximum number of results to return
            per_page: Results per API page
        
        Yields author records one by one, handling pagination automatically.
        """
        filter_str = self.build_filter(
            h_index_min=h_index_min,
            h_index_max=h_index_max,
            country_codes=country_codes,
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
            time.sleep(0.1)
    
    def _parse_author(self, author: dict) -> dict:
        """Parse author data into a cleaner format."""
        from disciplines import get_discipline_from_topics
        
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
        
        # Get discipline from topics
        discipline = get_discipline_from_topics(topics)
        
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
        country_codes: Optional[list[str]] = None,
        topic_ids: Optional[list[str]] = None,
        require_orcid: bool = True
    ) -> int:
        """Get total count of authors matching filters without fetching all data."""
        filter_str = self.build_filter(
            h_index_min=h_index_min,
            h_index_max=h_index_max,
            country_codes=country_codes,
            topic_ids=topic_ids,
            require_orcid=require_orcid
        )
        
        params = {
            "filter": filter_str,
            "per_page": 1
        }
        
        data = self._make_request("authors", params)
        return data.get("meta", {}).get("count", 0)
