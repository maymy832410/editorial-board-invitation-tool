"""Supabase client for persistent storage of sent invitations."""

import os
from datetime import datetime
from typing import Optional, Set, Dict, List
from supabase import create_client, Client


def _get_credentials():
    """Get Supabase credentials - hardcoded for reliability."""
    # Hardcoded credentials for production use
    url = "https://csstwegijzwlkjvjkvhp.supabase.co"
    key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNzc3R3ZWdpanp3bGtqdmprdmhwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzA5NDc3NTUsImV4cCI6MjA4NjUyMzc1NX0.n7Z6e4a-vFxANGteGWsZgmBWIvISUKmkIUfSN6KsZuk"
    
    return url, key


class SupabaseStorage:
    """Persistent storage using Supabase for sent invitations."""
    
    TABLE_NAME = "sent_invitations"
    
    def __init__(self):
        self.client: Optional[Client] = None
        self.available = False
        self.error_message = ""
        self._init_client()
    
    def _init_client(self):
        """Initialize Supabase client."""
        url, key = _get_credentials()
        
        if not url or not key:
            self.error_message = "Supabase credentials not configured"
            print(self.error_message)
            return
        
        try:
            self.client = create_client(url, key)
            # Test connection by querying table
            self.client.table(self.TABLE_NAME).select("orcid_id").limit(1).execute()
            self.available = True
            self.error_message = ""
        except Exception as e:
            self.error_message = f"Supabase error: {str(e)}"
            print(self.error_message)
            self.available = False
    
    def get_status(self) -> Dict:
        """Get database status for UI display."""
        return {
            "available": self.available,
            "error": self.error_message,
            "table": self.TABLE_NAME
        }
    
    def mark_sent(self, orcid_id: str, author_name: str = "", email: str = "", publisher: str = "") -> bool:
        """Mark an author as sent invitation."""
        if not self.available or not self.client:
            return False

        data = {
            "orcid_id": orcid_id,
            "author_name": author_name,
            "email": email,
            "publisher": publisher,
            "sent_at": datetime.utcnow().isoformat()
        }

        try:
            # Upsert - requires UNIQUE(orcid_id) or PRIMARY KEY on orcid_id
            self.client.table(self.TABLE_NAME).upsert(data, on_conflict="orcid_id").execute()
            return True
        except Exception as e1:
            print(f"Supabase mark_sent upsert error: {e1}")
            try:
                # Fallback: insert (table may use default id PK and no unique on orcid_id)
                self.client.table(self.TABLE_NAME).insert(data).execute()
                return True
            except Exception as e2:
                try:
                    # Already exists: update by orcid_id
                    self.client.table(self.TABLE_NAME).update({
                        "author_name": author_name,
                        "email": email,
                        "publisher": publisher,
                        "sent_at": data["sent_at"]
                    }).eq("orcid_id", orcid_id).execute()
                    return True
                except Exception as e3:
                    print(f"Supabase mark_sent fallback error: {e3}")
                    return False
    
    def is_sent(self, orcid_id: str) -> bool:
        """Check if author has been sent invitation."""
        if not self.available or not self.client:
            return False
        
        try:
            result = self.client.table(self.TABLE_NAME).select("orcid_id").eq("orcid_id", orcid_id).execute()
            return len(result.data) > 0
        except Exception as e:
            print(f"Supabase is_sent error: {e}")
            return False
    
    def get_all_sent(self) -> Set[str]:
        """Get all sent ORCID IDs (paginated to fetch full table, not just first 1000)."""
        if not self.available or not self.client:
            return set()

        page_size = 1000
        all_orcids: Set[str] = set()
        offset = 0
        try:
            while True:
                result = (
                    self.client.table(self.TABLE_NAME)
                    .select("orcid_id")
                    .range(offset, offset + page_size - 1)
                    .execute()
                )
                rows = result.data or []
                for row in rows:
                    if row.get("orcid_id"):
                        all_orcids.add(row["orcid_id"])
                if len(rows) < page_size:
                    break
                offset += page_size
            return all_orcids
        except Exception as e:
            print(f"Supabase get_all_sent error: {e}")
            return set()
    
    def get_sent_details(self, orcid_id: str) -> Optional[Dict]:
        """Get details of a sent invitation."""
        if not self.available or not self.client:
            return None
        
        try:
            result = self.client.table(self.TABLE_NAME).select("*").eq("orcid_id", orcid_id).execute()
            if result.data:
                return result.data[0]
            return None
        except Exception as e:
            print(f"Supabase get_sent_details error: {e}")
            return None
    
    def get_sent_count(self) -> int:
        """Get total count of sent invitations."""
        if not self.available or not self.client:
            return 0
        
        try:
            result = self.client.table(self.TABLE_NAME).select("orcid_id", count="exact").execute()
            return result.count or 0
        except Exception as e:
            print(f"Supabase get_sent_count error: {e}")
            return 0
    
    def remove_sent(self, orcid_id: str) -> bool:
        """Remove sent status (useful for corrections)."""
        if not self.available or not self.client:
            return False
        
        try:
            self.client.table(self.TABLE_NAME).delete().eq("orcid_id", orcid_id).execute()
            return True
        except Exception as e:
            print(f"Supabase remove_sent error: {e}")
            return False


# Singleton instance
_storage: Optional[SupabaseStorage] = None

def get_storage() -> SupabaseStorage:
    """Get or create Supabase storage singleton."""
    global _storage
    if _storage is None:
        _storage = SupabaseStorage()
    return _storage
