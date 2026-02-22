"""Email sender for sending invitations via SMTP (Brevo or other)."""

import json
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr
from pathlib import Path
from typing import Optional, Tuple


class EmailSender:
    """Send emails via SMTP using the first configured account per publisher."""

    CREDENTIALS_FILE = "email_credentials.json"

    def __init__(self):
        self.credentials = self._load_credentials()

    def _load_credentials(self) -> dict:
        """Load email credentials from JSON file or Streamlit secrets."""
        try:
            import streamlit as st
            if hasattr(st, 'secrets') and 'publishers' in st.secrets:
                credentials = {}
                for pub_id in st.secrets.publishers:
                    pub_data = st.secrets.publishers[pub_id]
                    credentials[pub_id] = {
                        "name": pub_data["name"] if "name" in pub_data else "",
                        "smtp_server": pub_data["smtp_server"] if "smtp_server" in pub_data else "smtp.titan.email",
                        "smtp_port": pub_data["smtp_port"] if "smtp_port" in pub_data else 465,
                        "use_ssl": pub_data["use_ssl"] if "use_ssl" in pub_data else True,
                        "accounts": []
                    }
                    if "accounts" in pub_data:
                        for acc in pub_data.accounts:
                            acct = {
                                "email": acc["email"] if "email" in acc else "",
                                "password": acc["password"] if "password" in acc else ""
                            }
                            if "smtp_login" in acc:
                                acct["smtp_login"] = acc["smtp_login"]
                            credentials[pub_id]["accounts"].append(acct)
                if credentials:
                    return credentials
        except Exception:
            pass

        cred_path = Path(__file__).parent / self.CREDENTIALS_FILE
        if not cred_path.exists():
            raise FileNotFoundError(
                f"Credentials file not found: {cred_path}\n"
                "Please create email_credentials.json or configure Streamlit secrets."
            )
        with open(cred_path, 'r') as f:
            return json.load(f)

    def get_publishers(self) -> list:
        """Get list of available publishers (Brevo only; Titan/external SMTP are hidden)."""
        result = []
        for key, val in self.credentials.items():
            smtp = (val.get("smtp_server") or "").lower()
            if "brevo" not in smtp:
                continue
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
        """Get publisher primary email address."""
        if publisher_id in self.credentials:
            accounts = self.credentials[publisher_id].get("accounts", [])
            if accounts:
                return accounts[0]["email"]
        return ""

    def get_account_count(self, publisher_id: str) -> int:
        """Get number of accounts for a publisher."""
        if publisher_id in self.credentials:
            return len(self.credentials[publisher_id].get("accounts", []))
        return 0

    def send_email(
        self,
        publisher_id: str,
        to_email: str,
        subject: str,
        body: str,
        to_name: Optional[str] = None,
        pdf_attachment: Optional[bytes] = None,
        attachment_filename: str = "Invitation_Letter.pdf",
    ) -> Tuple[bool, str]:
        """
        Send an email using the publisher's first account.
        """
        if publisher_id not in self.credentials:
            return False, f"Unknown publisher: {publisher_id}"

        if not to_email or '@' not in to_email:
            return False, f"Invalid recipient email: '{to_email}'"

        pub_config = self.credentials[publisher_id]
        accounts = pub_config.get("accounts", [])
        if not accounts:
            return False, "No email accounts configured for this publisher"

        account = accounts[0]
        sender_email = account.get('email', '')
        sender_password = account.get('password', '')
        login_user = account.get('smtp_login') or sender_email

        if not sender_email or '@' not in sender_email:
            return False, "Invalid sender email in account"

        try:
            if pdf_attachment:
                message = MIMEMultipart("mixed")
            else:
                message = MIMEMultipart("alternative")

            message["Subject"] = subject
            message["From"] = formataddr((pub_config['name'], sender_email))

            if to_name:
                safe_name = ''.join(c for c in to_name if c.isalnum() or c in ' .-')
                message["To"] = formataddr((safe_name, to_email))
            else:
                message["To"] = to_email

            body_part = MIMEMultipart("alternative")
            text_part = MIMEText(body, "plain", "utf-8")
            body_part.attach(text_part)
            html_body = self._text_to_html(body)
            html_part = MIMEText(html_body, "html", "utf-8")
            body_part.attach(html_part)

            if pdf_attachment:
                message.attach(body_part)
                pdf_part = MIMEBase("application", "pdf")
                pdf_part.set_payload(pdf_attachment)
                encoders.encode_base64(pdf_part)
                pdf_part.add_header(
                    "Content-Disposition",
                    f"attachment; filename={attachment_filename}"
                )
                message.attach(pdf_part)
            else:
                message.attach(text_part)
                message.attach(html_part)

            context = ssl.create_default_context()

            if pub_config.get("use_ssl", True):
                with smtplib.SMTP_SSL(
                    pub_config["smtp_server"],
                    pub_config["smtp_port"],
                    context=context
                ) as server:
                    server.login(login_user, sender_password)
                    server.sendmail(sender_email, to_email, message.as_string())
            else:
                with smtplib.SMTP(pub_config["smtp_server"], pub_config["smtp_port"]) as server:
                    server.starttls(context=context)
                    server.login(login_user, sender_password)
                    server.sendmail(sender_email, to_email, message.as_string())

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
        html = text.replace("&", "&amp;")
        html = html.replace("<", "&lt;")
        html = html.replace(">", "&gt;")
        html = html.replace("\n\n", "</p><p>")
        html = html.replace("\n", "<br>")
        html = html.replace("- ", "&#8226; ")
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: 'Georgia', 'Times New Roman', serif; font-size: 14px; line-height: 1.6; color: #333333; max-width: 600px; margin: 0 auto; padding: 20px; }}
                p {{ margin-bottom: 16px; }}
            </style>
        </head>
        <body><p>{html}</p></body>
        </html>
        """

    def test_connection(self, publisher_id: str) -> Tuple[bool, str]:
        """Test SMTP connection using the publisher's first account."""
        if publisher_id not in self.credentials:
            return False, f"Unknown publisher: {publisher_id}"

        pub_config = self.credentials[publisher_id]
        accounts = pub_config.get("accounts", [])
        if not accounts:
            return False, "No accounts configured for this publisher"

        account = accounts[0]
        test_password = account.get("password", "")
        login_user = account.get("smtp_login") or account.get("email", "")

        try:
            context = ssl.create_default_context()
            if pub_config.get("use_ssl", True):
                with smtplib.SMTP_SSL(
                    pub_config["smtp_server"],
                    pub_config["smtp_port"],
                    context=context,
                    timeout=10
                ) as server:
                    server.login(login_user, test_password)
                    return True, "Connection successful!"
            else:
                with smtplib.SMTP(
                    pub_config["smtp_server"],
                    pub_config["smtp_port"],
                    timeout=10
                ) as server:
                    server.starttls(context=context)
                    server.login(login_user, test_password)
                    return True, "Connection successful!"
        except smtplib.SMTPAuthenticationError:
            return False, f"Authentication failed for {login_user}. Check credentials."
        except smtplib.SMTPException as e:
            return False, f"SMTP error: {str(e)}"
        except Exception as e:
            return False, f"Connection error: {str(e)}"
