"""Async ORCID client for fast parallel email fetching with rate limiting."""

import asyncio
from typing import Optional, Callable
import aiohttp

from config import ORCID_API_BASE_URL


class AsyncOrcidClient:
    """Async client for fetching emails from ORCID in parallel."""
    
    def __init__(
        self,
        max_concurrent: int = 10,
        delay_between_batches: float = 1.0
    ):
        """
        Initialize async ORCID client.
        
        Args:
            max_concurrent: Max number of concurrent requests
            delay_between_batches: Delay in seconds between batches
        """
        self.base_url = ORCID_API_BASE_URL
        self.max_concurrent = max_concurrent
        self.delay_between_batches = delay_between_batches
        self.semaphore = None
        self.session = None
    
    async def __aenter__(self):
        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        self.session = aiohttp.ClientSession(
            headers={
                "Accept": "application/json",
                "User-Agent": "AuthorEmailFinder/1.0"
            },
            timeout=aiohttp.ClientTimeout(total=30)
        )
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    async def get_email(self, orcid_id: str) -> dict:
        """
        Fetch email for a single ORCID ID.
        
        Args:
            orcid_id: ORCID identifier
            
        Returns:
            Dict with keys 'email' (str or None), 'rate_limited' (bool) and
            'status' (one of 'found', 'no_email', 'rate_limited', 'error').
        """
        async with self.semaphore:
            rate_limited = False
            errored = False

            # Try /email endpoint first
            url = f"{self.base_url}/{orcid_id}/email"
            
            try:
                async with self.session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        email = self._parse_email(data)
                        if email:
                            return {"email": email, "rate_limited": False, "status": "found"}
                    elif response.status == 429:
                        await asyncio.sleep(2)
                        return {"email": None, "rate_limited": True, "status": "rate_limited"}
            except Exception:
                errored = True
            
            # Fallback: try /person endpoint (emails + researcher-urls)
            try:
                person_url = f"{self.base_url}/{orcid_id}/person"
                async with self.session.get(person_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        email = self._parse_person_email(data)
                        if email:
                            return {"email": email, "rate_limited": False, "status": "found"}
                    elif response.status == 429:
                        await asyncio.sleep(2)
                        return {"email": None, "rate_limited": True, "status": "rate_limited"}
            except Exception:
                errored = True
            
            status = "error" if (errored and not rate_limited) else "no_email"
            return {"email": None, "rate_limited": rate_limited, "status": status}
    
    def _parse_email(self, data: dict) -> Optional[str]:
        """Parse email from ORCID API /email response."""
        emails = data.get("email", [])
        for email_entry in emails:
            email = email_entry.get("email")
            if email:
                return email
        return None
    
    def _parse_person_email(self, data: dict) -> Optional[str]:
        """Parse email from ORCID API /person response (emails section)."""
        emails_section = data.get("emails", {})
        emails = emails_section.get("email", [])
        for email_entry in emails:
            email = email_entry.get("email")
            if email:
                return email
        return None
    
    async def fetch_emails_batch(
        self,
        authors: list,
        on_result: Optional[Callable] = None,
        on_progress: Optional[Callable] = None
    ) -> list:
        """
        Fetch emails for a batch of authors in parallel.
        
        Args:
            authors: List of author dicts with 'orcid_id' and 'name' keys
            on_result: Callback called with each result dict
            on_progress: Callback called with (current, total) progress
            
        Returns:
            List of result dicts with 'orcid_id', 'name', 'email' keys
        """
        results = []
        total = len(authors)
        
        # Process in smaller batches to respect rate limits
        batch_size = self.max_concurrent
        
        for i in range(0, total, batch_size):
            batch = authors[i:i + batch_size]
            
            # Create tasks for this batch
            tasks = []
            for author in batch:
                orcid_id = author.get("orcid_id")
                if orcid_id:
                    tasks.append(self._fetch_single(author))
                else:
                    # No ORCID, add empty result
                    result = {
                        "orcid_id": None,
                        "name": author.get("name", ""),
                        "email": None,
                        "rate_limited": False,
                        "email_status": "no_orcid",
                    }
                    results.append(result)
                    if on_result:
                        on_result(result)
            
            # Execute batch in parallel
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for result in batch_results:
                if isinstance(result, dict):
                    results.append(result)
                    if on_result:
                        on_result(result)
            
            # Progress callback
            if on_progress:
                on_progress(len(results), total)
            
            # Delay between batches to avoid rate limiting
            if i + batch_size < total:
                await asyncio.sleep(self.delay_between_batches)
        
        return results
    
    async def _fetch_single(self, author: dict) -> dict:
        """Fetch email for a single author."""
        orcid_id = author.get("orcid_id")
        name = author.get("name", "")
        
        lookup = await self.get_email(orcid_id)
        
        result = dict(author)
        result.update({
            "orcid_id": orcid_id,
            "name": name,
            "email": lookup.get("email"),
            "rate_limited": bool(lookup.get("rate_limited")),
            "email_status": lookup.get("status", "no_email"),
            "orcid_url": f"https://orcid.org/{orcid_id}" if orcid_id else None,
        })
        return result


async def fetch_emails_async(
    authors: list,
    max_concurrent: int = 10,
    delay_between_batches: float = 1.0,
    on_result: Optional[Callable] = None,
    on_progress: Optional[Callable] = None
) -> list:
    """
    Convenience function to fetch emails for a list of authors.
    
    Args:
        authors: List of author dicts
        max_concurrent: Max concurrent requests
        delay_between_batches: Delay between batches
        on_result: Callback for each result
        on_progress: Callback for progress updates
        
    Returns:
        List of results with emails
    """
    async with AsyncOrcidClient(
        max_concurrent=max_concurrent,
        delay_between_batches=delay_between_batches
    ) as client:
        return await client.fetch_emails_batch(
            authors,
            on_result=on_result,
            on_progress=on_progress
        )
