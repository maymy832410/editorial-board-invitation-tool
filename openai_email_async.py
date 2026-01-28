"""
Async email finder with Tavily + GPT-4o-mini (primary) and OpenAI web_search (fallback).
Flow: ORCID -> Tavily+GPT -> OpenAI web_search -> null
"""

import asyncio
import re
import json
from typing import Optional, Callable, List, Dict
from openai import OpenAI, AsyncOpenAI

from config import OPENAI_API_KEY, TAVILY_API_KEY


# GPT-4o-mini extraction prompt
EXTRACTION_PROMPT = """Extract email addresses from these search results for {author_name}:

{search_results}

Rules:
1. Only extract REAL emails that appear in the text above
2. Do NOT guess or predict emails based on patterns
3. Prefer institutional emails (.edu, .ac.uk, university domains) over personal (gmail, yahoo)
4. Return JSON format only

Return: {{"emails": ["email1@domain.com", "email2@domain.com"], "primary": "best_institutional@email.com"}}
If no emails found: {{"emails": [], "primary": null}}"""


class AsyncEmailFinderClient:
    """
    Async client for finding academic emails.
    
    Priority order:
    1. Tavily search + GPT-4o-mini extraction (cheap, ~$0.001/search)
    2. OpenAI Responses API with web_search (expensive fallback)
    3. Return null if both fail
    """
    
    def __init__(
        self,
        openai_api_key: str = OPENAI_API_KEY,
        tavily_api_key: str = TAVILY_API_KEY,
        max_concurrent: int = 5,
        delay_between_requests: float = 0.5
    ):
        self.openai_api_key = openai_api_key
        self.tavily_api_key = tavily_api_key
        self.max_concurrent = max_concurrent
        self.delay_between_requests = delay_between_requests
        self.openai_client = None
        self.tavily_client = None
        self.semaphore = None
        self.tavily_available = False
    
    async def __aenter__(self):
        self.openai_client = OpenAI(api_key=self.openai_api_key)
        self.async_openai = AsyncOpenAI(api_key=self.openai_api_key)
        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        
        # Try to initialize Tavily
        try:
            from tavily import TavilyClient
            self.tavily_client = TavilyClient(self.tavily_api_key)  # API key as positional arg
            # Test with a simple query
            self.tavily_client.search("test", max_results=1)
            self.tavily_available = True
        except Exception as e:
            print(f"Tavily initialization failed: {e}")
            self.tavily_available = False
        
        return self
    
    async def __aexit__(self, *args):
        pass
    
    def _extract_emails_regex(self, text: str) -> List[str]:
        """Extract email addresses using regex."""
        pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        emails = re.findall(pattern, text)
        # Filter out false positives
        filtered = [e for e in emails if not e.endswith(('.png', '.jpg', '.gif', '.svg'))]
        return list(set(filtered))
    
    def _prioritize_emails(self, emails: List[str]) -> tuple:
        """Sort emails to prioritize institutional over personal."""
        personal_domains = ['gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'live.com']
        
        institutional = [e for e in emails if not any(d in e.lower() for d in personal_domains)]
        personal = [e for e in emails if any(d in e.lower() for d in personal_domains)]
        
        all_sorted = institutional + personal
        primary = institutional[0] if institutional else (personal[0] if personal else None)
        
        return primary, all_sorted
    
    def _search_tavily(self, author_name: str, institution: Optional[str] = None) -> Optional[str]:
        """
        Search Tavily for author email.
        Returns combined text from search results.
        """
        if not self.tavily_available or not self.tavily_client:
            return None
        
        query = f"{author_name} email"
        if institution:
            query += f" {institution}"
        
        try:
            results = self.tavily_client.search(query, search_depth="advanced", max_results=5)
            
            # Combine all result content
            combined_text = []
            for r in results.get("results", []):
                title = r.get("title", "")
                content = r.get("content", "")
                url = r.get("url", "")
                combined_text.append(f"Title: {title}\nURL: {url}\nContent: {content}")
            
            return "\n---\n".join(combined_text) if combined_text else None
            
        except Exception:
            return None
    
    async def _extract_emails_gpt(self, author_name: str, search_results: str) -> Dict:
        """
        Use GPT-4o-mini to extract emails from search results.
        Returns dict with emails list and primary email.
        """
        try:
            prompt = EXTRACTION_PROMPT.format(
                author_name=author_name,
                search_results=search_results[:4000]  # Limit tokens
            )
            
            response = await self.async_openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=200,
                response_format={"type": "json_object"}
            )
            
            content = response.choices[0].message.content
            result = json.loads(content)
            
            return {
                "emails": result.get("emails", []),
                "primary": result.get("primary"),
                "source": "tavily+gpt"
            }
            
        except Exception:
            # Fallback to regex extraction
            emails = self._extract_emails_regex(search_results)
            primary, _ = self._prioritize_emails(emails)
            return {
                "emails": emails,
                "primary": primary,
                "source": "tavily+regex"
            }
    
    def _search_openai_web(self, author_name: str, institution: Optional[str] = None) -> Dict:
        """
        Fallback: Use OpenAI Responses API with web_search tool.
        More expensive but more reliable.
        """
        query = f"Find the professional email address for {author_name}"
        if institution:
            query += f" at {institution}"
        query += ". Search their university faculty page, Google Scholar, ResearchGate, or ORCID profile."
        
        try:
            response = self.openai_client.responses.create(
                model="gpt-4o-mini",
                tools=[{"type": "web_search"}],
                input=query,
                tool_choice="required"
            )
            
            # Extract text from response
            response_text = ""
            for output in response.output:
                if hasattr(output, 'content'):
                    for content in output.content:
                        if hasattr(content, 'text'):
                            response_text = content.text
                            break
            
            emails = self._extract_emails_regex(response_text)
            primary, all_sorted = self._prioritize_emails(emails)
            
            return {
                "emails": all_sorted,
                "primary": primary,
                "text": response_text[:500],
                "source": "openai_web_search"
            }
            
        except Exception as e:
            return {
                "emails": [],
                "primary": None,
                "text": str(e)[:200],
                "source": "error"
            }
    
    async def find_email(
        self,
        author_name: str,
        institution: Optional[str] = None,
        country: Optional[str] = None,
        research_area: Optional[str] = None,
        use_tavily: bool = True,
        use_openai_web: bool = True
    ) -> Dict:
        """
        Find email for an author using multiple methods.
        
        Priority:
        1. Tavily search + GPT-4o-mini extraction (if use_tavily=True)
        2. OpenAI web_search fallback (if use_openai_web=True)
        3. Return null
        """
        async with self.semaphore:
            # Method 1: Tavily + GPT-4o-mini (cheap)
            if use_tavily and self.tavily_available:
                tavily_results = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._search_tavily(author_name, institution)
                )
                
                if tavily_results:
                    gpt_result = await self._extract_emails_gpt(author_name, tavily_results)
                    
                    if gpt_result.get("primary"):
                        return {
                            "email": gpt_result["primary"],
                            "all_emails": ", ".join(gpt_result["emails"]),
                            "confidence": "high",
                            "source": gpt_result["source"]
                        }
            
            # Method 2: OpenAI web_search (fallback)
            if use_openai_web:
                openai_result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._search_openai_web(author_name, institution)
                )
                
                if openai_result.get("primary"):
                    return {
                        "email": openai_result["primary"],
                        "all_emails": ", ".join(openai_result["emails"]),
                        "confidence": "high",
                        "source": openai_result["source"]
                    }
            
            # Method 3: Return null
            return {
                "email": None,
                "all_emails": "",
                "confidence": "none",
                "source": "not_found"
            }
    
    async def _fetch_single(
        self,
        author: Dict,
        use_tavily: bool = True,
        use_openai_web: bool = True
    ) -> Dict:
        """Fetch email for a single author."""
        result = await self.find_email(
            author_name=author.get("name", ""),
            institution=author.get("institution"),
            country=author.get("country"),
            research_area=author.get("research_area") or author.get("specialty"),
            use_tavily=use_tavily,
            use_openai_web=use_openai_web
        )
        
        # Add delay between requests
        await asyncio.sleep(self.delay_between_requests)
        
        return {
            **author,
            "email": result.get("email"),
            "all_emails": result.get("all_emails"),
            "email_confidence": result.get("confidence"),
            "email_source": result.get("source")
        }
    
    async def fetch_emails_batch(
        self,
        authors: List[Dict],
        on_result: Optional[Callable] = None,
        on_progress: Optional[Callable] = None,
        use_tavily: bool = True,
        use_openai_web: bool = True
    ) -> List[Dict]:
        """
        Find emails for a batch of authors with parallel processing.
        
        Args:
            use_tavily: Enable Tavily + GPT-4o-mini search
            use_openai_web: Enable OpenAI web_search fallback
        """
        results = []
        total = len(authors)
        
        # Process in parallel batches
        batch_size = self.max_concurrent
        
        for i in range(0, total, batch_size):
            batch = authors[i:i + batch_size]
            
            # Create tasks for parallel execution
            tasks = [self._fetch_single(author, use_tavily, use_openai_web) for author in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for j, result in enumerate(batch_results):
                if isinstance(result, Exception):
                    # Handle failed requests
                    result = {
                        **batch[j],
                        "email": None,
                        "all_emails": "",
                        "email_confidence": "error",
                        "email_source": "error"
                    }
                
                results.append(result)
                
                if on_result:
                    on_result(result)
            
            if on_progress:
                on_progress(len(results), total)
        
        return results


# Backward compatibility aliases
AsyncOpenAIEmailClient = AsyncEmailFinderClient
AsyncWebSearchEmailClient = AsyncEmailFinderClient


async def infer_missing_emails(
    authors: List[Dict],
    api_key: str = None,
    max_concurrent: int = 5,
    delay_between_requests: float = 0.5,
    on_result: Optional[Callable] = None,
    on_progress: Optional[Callable] = None
) -> List[Dict]:
    """
    Find emails for authors who don't have one from ORCID.
    Uses Tavily+GPT first, then OpenAI web_search, then returns null.
    """
    with_email = []
    without_email = []
    
    for author in authors:
        if author.get("email"):
            author["email_source"] = author.get("email_source", "orcid")
            with_email.append(author)
        else:
            without_email.append(author)
    
    if not without_email:
        return authors
    
    async with AsyncEmailFinderClient(
        max_concurrent=max_concurrent,
        delay_between_requests=delay_between_requests
    ) as client:
        inferred = await client.fetch_emails_batch(
            without_email,
            on_result=on_result,
            on_progress=on_progress
        )
    
    result_map = {a.get("orcid_id") or a.get("name"): a for a in inferred}
    
    final_results = []
    for author in authors:
        key = author.get("orcid_id") or author.get("name")
        if author.get("email"):
            final_results.append(author)
        elif key in result_map:
            final_results.append(result_map[key])
        else:
            final_results.append(author)
    
    return final_results


async def test_email_finder(authors: List[Dict]) -> List[Dict]:
    """Test email finding for a list of authors."""
    async with AsyncEmailFinderClient() as client:
        print(f"Tavily available: {client.tavily_available}")
        return await client.fetch_emails_batch(
            authors,
            on_result=lambda r: print(f"  -> {r.get('name')}: {r.get('email', 'N/A')} ({r.get('email_source', '?')})")
        )


if __name__ == "__main__":
    import time
    
    test_authors = [
        {"name": "Carmen Peñafiel Sáiz", "institution": "Universidad del País Vasco", "specialty": "Communication"},
        {"name": "Esther Martínez Pastor", "institution": "Universidad Rey Juan Carlos", "specialty": "Advertising"},
        {"name": "Francisco Paulo Jamil Marques", "institution": "Universidade Federal do Paraná", "specialty": "Political Communication"},
    ]
    
    print("Email Finder (Tavily+GPT -> OpenAI web_search -> null)")
    print("=" * 60)
    
    start = time.time()
    results = asyncio.run(test_email_finder(test_authors))
    elapsed = time.time() - start
    
    print()
    print(f"Completed in {elapsed:.2f}s")
    print()
    for r in results:
        print(f"  {r['name']}")
        print(f"    Email: {r.get('email', 'N/A')}")
        print(f"    All: {r.get('all_emails', 'N/A')}")
        print(f"    Source: {r.get('email_source', '?')}")
        print()
