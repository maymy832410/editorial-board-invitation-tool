"""Progress and state manager for the Editorial Board Invitation Tool."""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from config import DATA_DIR


class StateManager:
    """Manages complete app state including journal config, search results, and sent invitations."""
    
    STATE_FILE = "app_state.json"
    
    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = Path(data_dir)
        self.state_file = self.data_dir / self.STATE_FILE
        self._ensure_data_dir()
    
    def _ensure_data_dir(self):
        """Create data directory if it doesn't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_default_state(self) -> Dict:
        """Return default empty state."""
        return {
            "publisher": "brevo",
            "journal_config": {
                "name": "",
                "issn": "",
                "link": "",
                "editor_in_chief": ""
            },
            "search_params": {
                "h_index_min": 10,
                "h_index_max": 50,
                "countries": [],
                "disciplines": [],
                "max_results": 500,
                "jump_size": 250
            },
            "search_results": [],
            "search_pagination": {},
            "processed_orcids": [],
            "sent_invitations": [],
            "last_updated": None
        }
    
    def load_state(self) -> Dict:
        """
        Load app state from file.
        
        Returns:
            Complete state dict, or default state if file doesn't exist
        """
        if not self.state_file.exists():
            return self._get_default_state()
        
        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
            
            # Ensure all keys exist (for backward compatibility)
            default = self._get_default_state()
            for key in default:
                if key not in state:
                    state[key] = default[key]
            
            # Convert lists to sets where needed for lookup
            state['processed_orcids'] = set(state.get('processed_orcids', []))
            state['sent_invitations'] = set(state.get('sent_invitations', []))
            
            return state
            
        except (json.JSONDecodeError, KeyError):
            return self._get_default_state()
    
    def save_state(self, state: Dict):
        """
        Save complete app state to file.
        
        Args:
            state: Complete state dictionary
        """
        # Convert sets to lists for JSON serialization
        state_to_save = state.copy()
        
        if isinstance(state_to_save.get('processed_orcids'), set):
            state_to_save['processed_orcids'] = list(state_to_save['processed_orcids'])
        
        if isinstance(state_to_save.get('sent_invitations'), set):
            state_to_save['sent_invitations'] = list(state_to_save['sent_invitations'])
        
        state_to_save['last_updated'] = datetime.now().isoformat()
        
        with open(self.state_file, 'w') as f:
            json.dump(state_to_save, f, indent=2, default=str)
    
    def update_publisher(self, publisher_id: str):
        """Update selected publisher."""
        state = self.load_state()
        state['publisher'] = publisher_id
        self.save_state(state)
    
    def update_journal_config(
        self,
        name: str = None,
        issn: str = None,
        link: str = None,
        editor_in_chief: str = None
    ):
        """Update journal configuration."""
        state = self.load_state()
        
        if name is not None:
            state['journal_config']['name'] = name
        if issn is not None:
            state['journal_config']['issn'] = issn
        if link is not None:
            state['journal_config']['link'] = link
        if editor_in_chief is not None:
            state['journal_config']['editor_in_chief'] = editor_in_chief
        
        self.save_state(state)
    
    def update_search_params(self, params: Dict):
        """Update search parameters."""
        state = self.load_state()
        state['search_params'].update(params)
        self.save_state(state)
    
    def save_search_results(self, results: List[Dict]):
        """Save search results."""
        state = self.load_state()
        state['search_results'] = results
        state['processed_orcids'] = set()  # Reset email fetching progress
        self.save_state(state)
    
    def update_author_email(self, orcid_id: str, email: Optional[str]):
        """Update email for a specific author in results."""
        state = self.load_state()
        
        for author in state['search_results']:
            if author.get('orcid_id') == orcid_id:
                author['email'] = email
                break
        
        if isinstance(state['processed_orcids'], set):
            state['processed_orcids'].add(orcid_id)
        else:
            state['processed_orcids'] = set(state['processed_orcids'])
            state['processed_orcids'].add(orcid_id)
        
        self.save_state(state)
    
    def mark_invitation_sent(self, orcid_id: str):
        """Mark an invitation as sent for an author."""
        state = self.load_state()
        
        if isinstance(state['sent_invitations'], set):
            state['sent_invitations'].add(orcid_id)
        else:
            state['sent_invitations'] = set(state['sent_invitations'])
            state['sent_invitations'].add(orcid_id)
        
        self.save_state(state)
    
    def is_invitation_sent(self, orcid_id: str) -> bool:
        """Check if invitation was sent to an author."""
        state = self.load_state()
        sent = state.get('sent_invitations', set())
        if isinstance(sent, list):
            sent = set(sent)
        return orcid_id in sent
    
    def get_search_results(self) -> List[Dict]:
        """Get saved search results."""
        state = self.load_state()
        return state.get('search_results', [])
    
    def get_processed_orcids(self) -> set:
        """Get set of processed ORCID IDs."""
        state = self.load_state()
        processed = state.get('processed_orcids', [])
        if isinstance(processed, list):
            return set(processed)
        return processed
    
    def get_sent_invitations(self) -> set:
        """Get set of ORCID IDs that received invitations."""
        state = self.load_state()
        sent = state.get('sent_invitations', [])
        if isinstance(sent, list):
            return set(sent)
        return sent
    
    def reset_all(self):
        """Reset all state to defaults."""
        default_state = self._get_default_state()
        self.save_state(default_state)
    
    def reset_search(self):
        """Reset only search results and email progress."""
        state = self.load_state()
        state['search_results'] = []
        state['processed_orcids'] = []
        state['sent_invitations'] = []
        self.save_state(state)
    
    def export_results_csv(self, filename: str = "results.csv") -> str:
        """
        Export search results to CSV.
        
        Returns:
            Path to saved CSV file
        """
        import pandas as pd
        
        state = self.load_state()
        results = state.get('search_results', [])
        
        if not results:
            return None
        
        # Add sent status to results
        sent = self.get_sent_invitations()
        for r in results:
            r['invitation_sent'] = r.get('orcid_id') in sent
        
        df = pd.DataFrame(results)
        filepath = self.data_dir / filename
        df.to_csv(filepath, index=False)
        
        return str(filepath)


# Keep backward compatibility with old ProgressManager
class ProgressManager(StateManager):
    """Alias for backward compatibility."""
    pass
