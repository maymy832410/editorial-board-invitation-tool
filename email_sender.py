"""Email sender for sending invitations via SMTP."""

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
from typing import Optional, Tuple


class EmailSender:
    """Send emails via SMTP using publisher credentials."""
    
    CREDENTIALS_FILE = "email_credentials.json"
    
    def __init__(self):
        self.credentials = self._load_credentials()
    
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
                        "email": pub_data.get("email", ""),
                        "password": pub_data.get("password", ""),
                        "smtp_server": pub_data.get("smtp_server", ""),
                        "smtp_port": pub_data.get("smtp_port", 465),
                        "use_ssl": pub_data.get("use_ssl", True)
                    }
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
        return [
            {"id": key, "name": val["name"], "email": val["email"]}
            for key, val in self.credentials.items()
        ]
    
    def get_publisher_name(self, publisher_id: str) -> str:
        """Get publisher display name."""
        if publisher_id in self.credentials:
            return self.credentials[publisher_id]["name"]
        return ""
    
    def get_publisher_email(self, publisher_id: str) -> str:
        """Get publisher email address."""
        if publisher_id in self.credentials:
            return self.credentials[publisher_id]["email"]
        return ""
    
    def send_email(
        self,
        publisher_id: str,
        to_email: str,
        subject: str,
        body: str,
        to_name: Optional[str] = None,
        pdf_attachment: Optional[bytes] = None,
        attachment_filename: str = "Invitation_Letter.pdf"
    ) -> Tuple[bool, str]:
        """
        Send an email using the specified publisher's SMTP settings.
        
        Args:
            publisher_id: Publisher ID (e.g., 'peninsula', 'mesopotamian')
            to_email: Recipient email address
            subject: Email subject
            body: Email body (plain text)
            to_name: Optional recipient name for display
            pdf_attachment: Optional PDF file as bytes to attach
            attachment_filename: Filename for the PDF attachment
            
        Returns:
            Tuple of (success: bool, message: str)
        """
        if publisher_id not in self.credentials:
            return False, f"Unknown publisher: {publisher_id}"
        
        # Validate recipient email
        if not to_email or '@' not in to_email:
            return False, f"Invalid recipient email: '{to_email}'"
        
        creds = self.credentials[publisher_id]
        
        # Validate sender email
        sender_email = creds.get('email', '')
        if not sender_email or '@' not in sender_email:
            return False, f"Invalid sender email in credentials"
        
        try:
            # Create message - use mixed for attachments
            if pdf_attachment:
                message = MIMEMultipart("mixed")
            else:
                message = MIMEMultipart("alternative")
            
            message["Subject"] = subject
            # Use formataddr to properly encode names with special characters
            message["From"] = formataddr((creds['name'], creds['email']))
            
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
            
            # Connect and send
            # Use smtp_username if provided (e.g., for AWS SES), otherwise use email
            smtp_user = creds.get("smtp_username", creds["email"])
            
            if creds.get("use_ssl", True):
                # SSL connection (port 465)
                with smtplib.SMTP_SSL(
                    creds["smtp_server"],
                    creds["smtp_port"],
                    context=context
                ) as server:
                    server.login(smtp_user, creds["password"])
                    server.sendmail(
                        creds["email"],
                        to_email,
                        message.as_string()
                    )
            else:
                # TLS connection (port 587)
                with smtplib.SMTP(creds["smtp_server"], creds["smtp_port"]) as server:
                    server.starttls(context=context)
                    server.login(smtp_user, creds["password"])
                    server.sendmail(
                        creds["email"],
                        to_email,
                        message.as_string()
                    )
            
            attachment_info = " with PDF attachment" if pdf_attachment else ""
            return True, f"Email sent successfully{attachment_info}"
            
        except smtplib.SMTPAuthenticationError:
            return False, "Authentication failed. Please check your email credentials."
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
    
    def test_connection(self, publisher_id: str) -> Tuple[bool, str]:
        """
        Test SMTP connection without sending an email.
        
        Args:
            publisher_id: Publisher ID to test
            
        Returns:
            Tuple of (success: bool, message: str)
        """
        if publisher_id not in self.credentials:
            return False, f"Unknown publisher: {publisher_id}"
        
        creds = self.credentials[publisher_id]
        
        try:
            context = ssl.create_default_context()
            
            # Use smtp_username if provided (e.g., for AWS SES), otherwise use email
            smtp_user = creds.get("smtp_username", creds["email"])
            
            if creds.get("use_ssl", True):
                with smtplib.SMTP_SSL(
                    creds["smtp_server"],
                    creds["smtp_port"],
                    context=context,
                    timeout=10
                ) as server:
                    server.login(smtp_user, creds["password"])
                    return True, "Connection successful!"
            else:
                with smtplib.SMTP(
                    creds["smtp_server"],
                    creds["smtp_port"],
                    timeout=10
                ) as server:
                    server.starttls(context=context)
                    server.login(smtp_user, creds["password"])
                    return True, "Connection successful!"
                    
        except smtplib.SMTPAuthenticationError:
            return False, "Authentication failed. Check credentials."
        except smtplib.SMTPException as e:
            return False, f"SMTP error: {str(e)}"
        except Exception as e:
            return False, f"Connection error: {str(e)}"
