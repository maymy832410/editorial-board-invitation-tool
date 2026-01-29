"""Email sender for sending invitations via SMTP with account pooling."""

import json
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr
from email.header import Header
from pathlib import Path
from typing import Optional, Tuple, Dict, List


class EmailSender:
    """Send emails via SMTP using publisher account pools with round-robin rotation."""
    
    CREDENTIALS_FILE = "email_credentials.json"
    
    def __init__(self):
        self.credentials = self._load_credentials()
        self._account_index: Dict[str, int] = {}  # Track rotation per publisher
    
    def _load_credentials(self) -> dict:
        """Load email credentials from JSON file or Streamlit secrets."""
        
        # First try Streamlit secrets (for cloud deployment)
        try:
            import streamlit as st
            if hasattr(st, 'secrets') and 'publishers' in st.secrets:
                # Load from Streamlit secrets
                credentials = {}
                for pub_id in st.secrets.publishers:
                    pub_data = st.secrets.publishers[pub_id]
                    credentials[pub_id] = {
                        "name": pub_data.get("name", ""),
                        "smtp_server": pub_data.get("smtp_server", "smtp.titan.email"),
                        "smtp_port": pub_data.get("smtp_port", 465),
                        "use_ssl": pub_data.get("use_ssl", True),
                        "accounts": []
                    }
                    # Load accounts array
                    if "accounts" in pub_data:
                        for acc in pub_data.accounts:
                            credentials[pub_id]["accounts"].append({
                                "email": acc.get("email", ""),
                                "password": acc.get("password", "")
                            })
                if credentials:
                    return credentials
        except Exception:
            pass
        
        # Fallback to local JSON file
        cred_path = Path(__file__).parent / self.CREDENTIALS_FILE
        
        if not cred_path.exists():
            raise FileNotFoundError(
                f"Credentials file not found: {cred_path}\n"
                "Please create email_credentials.json or configure Streamlit secrets."
            )
        
        with open(cred_path, 'r') as f:
            return json.load(f)
    
    def get_publishers(self) -> list:
        """Get list of available publishers."""
        result = []
        for key, val in self.credentials.items():
            # Get primary email (first account in pool)
            primary_email = ""
            if "accounts" in val and len(val["accounts"]) > 0:
                primary_email = val["accounts"][0]["email"]
            result.append({
                "id": key,
                "name": val["name"],
                "email": primary_email,
                "account_count": len(val.get("accounts", []))
            })
        return result
    
    def get_publisher_name(self, publisher_id: str) -> str:
        """Get publisher display name."""
        if publisher_id in self.credentials:
            return self.credentials[publisher_id]["name"]
        return ""
    
    def get_publisher_email(self, publisher_id: str) -> str:
        """Get publisher primary email address (first account in pool)."""
        if publisher_id in self.credentials:
            accounts = self.credentials[publisher_id].get("accounts", [])
            if accounts:
                return accounts[0]["email"]
        return ""
    
    def get_account_count(self, publisher_id: str) -> int:
        """Get number of accounts in the pool for a publisher."""
        if publisher_id in self.credentials:
            return len(self.credentials[publisher_id].get("accounts", []))
        return 0
    
    def get_next_account(self, publisher_id: str) -> Dict:
        """
        Get next account in round-robin rotation.
        
        Returns dict with 'email' and 'password' keys.
        """
        if publisher_id not in self.credentials:
            return {}
        
        accounts = self.credentials[publisher_id].get("accounts", [])
        if not accounts:
            return {}
        
        idx = self._account_index.get(publisher_id, 0)
        account = accounts[idx % len(accounts)]
        self._account_index[publisher_id] = idx + 1
        
        return account
    
    def peek_next_account(self, publisher_id: str) -> Dict:
        """
        Peek at the next account without advancing the rotation.
        
        Returns dict with 'email' and 'password' keys.
        """
        if publisher_id not in self.credentials:
            return {}
        
        accounts = self.credentials[publisher_id].get("accounts", [])
        if not accounts:
            return {}
        
        idx = self._account_index.get(publisher_id, 0)
        return accounts[idx % len(accounts)]
    
    def get_pool_status(self, publisher_id: str) -> Dict:
        """Get pool status for a publisher."""
        if publisher_id not in self.credentials:
            return {"total": 0, "current_index": 0, "daily_capacity": 0}
        
        accounts = self.credentials[publisher_id].get("accounts", [])
        idx = self._account_index.get(publisher_id, 0)
        
        return {
            "total": len(accounts),
            "current_index": idx % len(accounts) if accounts else 0,
            "daily_capacity": len(accounts) * 50,  # 50 emails per account per day
            "sends_today": idx  # Approximate, resets on app restart
        }
    
    def get_all_accounts(self, publisher_id: str) -> List[str]:
        """Get list of all account emails for a publisher."""
        if publisher_id not in self.credentials:
            return []
        return [acc["email"] for acc in self.credentials[publisher_id].get("accounts", [])]
    
    def get_account_by_email(self, publisher_id: str, email: str) -> Dict:
        """Get account by email address."""
        if publisher_id not in self.credentials:
            return {}
        for acc in self.credentials[publisher_id].get("accounts", []):
            if acc.get("email") == email:
                return acc
        return {}
    
    def send_email(
        self,
        publisher_id: str,
        to_email: str,
        subject: str,
        body: str,
        to_name: Optional[str] = None,
        pdf_attachment: Optional[bytes] = None,
        attachment_filename: str = "Invitation_Letter.pdf",
        force_account_email: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Send an email using the next account in the publisher's pool.
        
        Args:
            publisher_id: Publisher ID (e.g., 'peninsula', 'mesopotamian')
            to_email: Recipient email address
            subject: Email subject
            body: Email body (plain text)
            to_name: Optional recipient name for display
            pdf_attachment: Optional PDF file as bytes to attach
            attachment_filename: Filename for the PDF attachment
            force_account_email: Optional specific account email to use (bypasses rotation)
            
        Returns:
            Tuple of (success: bool, message: str)
        """
        if publisher_id not in self.credentials:
            return False, f"Unknown publisher: {publisher_id}"
        
        # Validate recipient email
        if not to_email or '@' not in to_email:
            return False, f"Invalid recipient email: '{to_email}'"
        
        pub_config = self.credentials[publisher_id]
        
        # Get account - either forced or next in rotation
        if force_account_email:
            account = self.get_account_by_email(publisher_id, force_account_email)
            if not account:
                return False, f"Account not found: {force_account_email}"
        else:
            account = self.get_next_account(publisher_id)
            if not account:
                return False, "No email accounts configured for this publisher"
        
        sender_email = account.get('email', '')
        sender_password = account.get('password', '')
        
        if not sender_email or '@' not in sender_email:
            return False, f"Invalid sender email in account pool"
        
        try:
            # Create message - use mixed for attachments
            if pdf_attachment:
                message = MIMEMultipart("mixed")
            else:
                message = MIMEMultipart("alternative")
            
            message["Subject"] = subject
            # Use formataddr to properly encode names with special characters
            message["From"] = formataddr((pub_config['name'], sender_email))
            
            if to_name:
                # Sanitize to_name - remove characters that break email headers
                safe_name = ''.join(c for c in to_name if c.isalnum() or c in ' .-')
                message["To"] = formataddr((safe_name, to_email))
            else:
                message["To"] = to_email
            
            # Create body part as alternative (plain + html)
            body_part = MIMEMultipart("alternative")
            
            # Add plain text body
            text_part = MIMEText(body, "plain", "utf-8")
            body_part.attach(text_part)
            
            # Also add HTML version for better formatting
            html_body = self._text_to_html(body)
            html_part = MIMEText(html_body, "html", "utf-8")
            body_part.attach(html_part)
            
            if pdf_attachment:
                message.attach(body_part)
                
                # Attach PDF
                pdf_part = MIMEBase("application", "pdf")
                pdf_part.set_payload(pdf_attachment)
                encoders.encode_base64(pdf_part)
                pdf_part.add_header(
                    "Content-Disposition",
                    f"attachment; filename={attachment_filename}"
                )
                message.attach(pdf_part)
            else:
                # No attachment, just attach body parts directly
                message.attach(text_part)
                message.attach(html_part)
            
            # Create SSL context
            context = ssl.create_default_context()
            
            # Connect and send using pool account
            if pub_config.get("use_ssl", True):
                # SSL connection (port 465)
                with smtplib.SMTP_SSL(
                    pub_config["smtp_server"],
                    pub_config["smtp_port"],
                    context=context
                ) as server:
                    server.login(sender_email, sender_password)
                    server.sendmail(
                        sender_email,
                        to_email,
                        message.as_string()
                    )
            else:
                # TLS connection (port 587)
                with smtplib.SMTP(pub_config["smtp_server"], pub_config["smtp_port"]) as server:
                    server.starttls(context=context)
                    server.login(sender_email, sender_password)
                    server.sendmail(
                        sender_email,
                        to_email,
                        message.as_string()
                    )
            
            attachment_info = " with PDF attachment" if pdf_attachment else ""
            return True, f"Email sent from {sender_email}{attachment_info}"
            
        except smtplib.SMTPAuthenticationError:
            return False, f"Authentication failed for {sender_email}. Check credentials."
        except smtplib.SMTPRecipientsRefused:
            return False, f"Recipient refused: {to_email}"
        except smtplib.SMTPException as e:
            return False, f"SMTP error: {str(e)}"
        except Exception as e:
            return False, f"Error sending email: {str(e)}"
    
    def _text_to_html(self, text: str) -> str:
        """Convert plain text email to simple HTML."""
        # Escape HTML characters
        html = text.replace("&", "&amp;")
        html = html.replace("<", "&lt;")
        html = html.replace(">", "&gt;")
        
        # Convert line breaks to <br>
        html = html.replace("\n\n", "</p><p>")
        html = html.replace("\n", "<br>")
        
        # Convert bullet points
        html = html.replace("- ", "&#8226; ")
        
        # Wrap in HTML structure
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{
                    font-family: 'Georgia', 'Times New Roman', serif;
                    font-size: 14px;
                    line-height: 1.6;
                    color: #333333;
                    max-width: 600px;
                    margin: 0 auto;
                    padding: 20px;
                }}
                p {{
                    margin-bottom: 16px;
                }}
            </style>
        </head>
        <body>
            <p>{html}</p>
        </body>
        </html>
        """
    
    def test_connection(self, publisher_id: str, force_account_email: Optional[str] = None) -> Tuple[bool, str]:
        """
        Test SMTP connection using the specified or first account in the pool.
        
        Args:
            publisher_id: Publisher ID to test
            force_account_email: Optional specific account email to test
            
        Returns:
            Tuple of (success: bool, message: str)
        """
        if publisher_id not in self.credentials:
            return False, f"Unknown publisher: {publisher_id}"
        
        pub_config = self.credentials[publisher_id]
        accounts = pub_config.get("accounts", [])
        
        if not accounts:
            return False, "No accounts configured for this publisher"
        
        # Get account - either forced or first in pool
        if force_account_email:
            account = self.get_account_by_email(publisher_id, force_account_email)
            if not account:
                return False, f"Account not found: {force_account_email}"
        else:
            account = accounts[0]
        
        test_email = account.get("email", "")
        test_password = account.get("password", "")
        
        try:
            context = ssl.create_default_context()
            
            if pub_config.get("use_ssl", True):
                with smtplib.SMTP_SSL(
                    pub_config["smtp_server"],
                    pub_config["smtp_port"],
                    context=context,
                    timeout=10
                ) as server:
                    server.login(test_email, test_password)
                    return True, f"Connection successful! ({len(accounts)} accounts in pool)"
            else:
                with smtplib.SMTP(
                    pub_config["smtp_server"],
                    pub_config["smtp_port"],
                    timeout=10
                ) as server:
                    server.starttls(context=context)
                    server.login(test_email, test_password)
                    return True, f"Connection successful! ({len(accounts)} accounts in pool)"
                    
        except smtplib.SMTPAuthenticationError:
            return False, f"Authentication failed for {test_email}. Check credentials."
        except smtplib.SMTPException as e:
            return False, f"SMTP error: {str(e)}"
        except Exception as e:
            return False, f"Connection error: {str(e)}"
