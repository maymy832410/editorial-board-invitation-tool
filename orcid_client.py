"""ORCID Public API client for fetching author emails."""

import time
from typing import Optional
import requests

from config import ORCID_API_BASE_URL


class OrcidClient:
    """Client for querying ORCID Public API for email addresses."""
    
    def __init__(self):
        self.base_url = ORCID_API_BASE_URL
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "AuthorEmailFinder/1.0"
        })
    
    def get_email(self, orcid_id: str, max_retries: int = 3) -> Optional[str]:
        """
        Fetch email for a given ORCID ID using the public API.
        
        Args:
            orcid_id: ORCID identifier (e.g., "0000-0001-9557-8195")
            max_retries: Number of retries on failure
            
        Returns:
            Email address if found and public, None otherwise
        """
        url = f"{self.base_url}/{orcid_id}/email"
        
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, timeout=30)
                
                if response.status_code == 200:
                    return self._parse_email_response(response.json())
                elif response.status_code == 404:
                    # ORCID not found
                    return None
                elif response.status_code == 429:
                    # Rate limited
                    wait_time = (2 ** attempt) * 2
                    time.sleep(wait_time)
                    continue
                elif response.status_code >= 500:
                    # Server error
                    wait_time = (2 ** attempt) * 2
                    time.sleep(wait_time)
                    continue
                else:
                    # Other client error
                    return None
                    
            except requests.exceptions.RequestException:
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) * 2
                    time.sleep(wait_time)
                    continue
                return None
        
        return None
    
    def _parse_email_response(self, data: dict) -> Optional[str]:
        """Parse email from ORCID API response."""
        emails = data.get("email", [])
        
        if not emails:
            return None
        
        # Return the first email that has a value
        for email_entry in emails:
            email = email_entry.get("email")
            if email:
                return email
        
        return None
    
    def get_full_record(self, orcid_id: str, max_retries: int = 3) -> Optional[dict]:
        """
        Fetch full public record for a given ORCID ID.
        
        This can be used to get additional info like employment, education, etc.
        
        Args:
            orcid_id: ORCID identifier
            max_retries: Number of retries on failure
            
        Returns:
            Full record dict if found, None otherwise
        """
        url = f"{self.base_url}/{orcid_id}/record"
        
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, timeout=30)
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 404:
                    return None
                elif response.status_code == 429:
                    wait_time = (2 ** attempt) * 2
                    time.sleep(wait_time)
                    continue
                elif response.status_code >= 500:
                    wait_time = (2 ** attempt) * 2
                    time.sleep(wait_time)
                    continue
                else:
                    return None
                    
            except requests.exceptions.RequestException:
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) * 2
                    time.sleep(wait_time)
                    continue
                return None
        
        return None
    
    def extract_emails_from_record(self, record: dict) -> list[str]:
        """
        Extract all emails from a full ORCID record.
        
        Args:
            record: Full ORCID record from get_full_record()
            
        Returns:
            List of email addresses found
        """
        emails = []
        
        # Check person -> emails
        person = record.get("person", {})
        emails_section = person.get("emails", {})
        email_list = emails_section.get("email", [])
        
        for email_entry in email_list:
            email = email_entry.get("email")
            if email:
                emails.append(email)
        
        return emails
