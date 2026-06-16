"""Email sender for sending invitations via SMTP (Brevo or other)."""

import base64
import html
import io
import json
import mimetypes
import os
import re
import requests
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr
from pathlib import Path
from typing import Optional, Tuple


LOGO_DIR = Path(__file__).parent / "pdf template"
TRANSPARENT_LOGO_DIR = Path(__file__).parent / "logo"
SCOPUS_LOGO_FILE = TRANSPARENT_LOGO_DIR / "Elsevier-scopus.jpg"
DEFAULT_PUBLIC_ASSET_BASE_URL = "https://editorial-board-app-production.up.railway.app"
DEFAULT_PUBLISHER_LOGO_PATHS = {
    "peninsula": "/app/static/logos/peninsula-publishing-press.jpg",
    "brevo": "/app/static/logos/peninsula-publishing-press.jpg",
    "mesopotamian": "/app/static/logos/map-logo-mesopotamian.png",
}
DEFAULT_SCOPUS_LOGO_PATH = "/app/static/logos/elsevier-scopus.jpg"
PUBLISHER_LOGOS = {
    "peninsula": LOGO_DIR / "Peninsula publishing press.jpg",
    "brevo": LOGO_DIR / "Peninsula publishing press.jpg",
    "mesopotamian": TRANSPARENT_LOGO_DIR / "MAP logo Mesopotamian.png",
}


class EmailSender:
    """Send emails via SMTP using the first configured account per publisher."""

    CREDENTIALS_FILE = "email_credentials.json"

    def __init__(self):
        self.credentials = self._load_credentials()
        self._logo_data_cache: dict[str, str] = {}
        self._asset_data_cache: dict[str, str] = {}

    def _public_asset_base_url(self) -> str:
        """Return the base URL for hosted public email assets."""
        return (
            os.environ.get("PUBLIC_ASSET_BASE_URL", DEFAULT_PUBLIC_ASSET_BASE_URL)
            .strip()
            .rstrip("/")
        )

    def _build_public_asset_url(self, relative_path: str) -> str:
        """Build an absolute URL for a static asset path."""
        base_url = self._public_asset_base_url()
        if not base_url or not relative_path:
            return ""
        normalized_path = relative_path if relative_path.startswith("/") else f"/{relative_path}"
        return f"{base_url}{normalized_path}"

    def _is_http_url(self, value: str) -> bool:
        """Check whether a value is an HTTP(S) URL."""
        lowered = (value or "").strip().lower()
        return lowered.startswith("https://") or lowered.startswith("http://")

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

        # Try EMAIL_CREDENTIALS environment variable (JSON string)
        env_creds = os.environ.get("EMAIL_CREDENTIALS", "")
        if env_creds:
            try:
                return json.loads(env_creds)
            except json.JSONDecodeError:
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
        """Get list of available publisher sender identities."""
        result = []
        for key, val in self.credentials.items():
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
        journal_name: str = "",
        journal_link: str = "",
        submission_link: str = "",
        invitation_type: str = "editorial",
        scopus_indexed: bool = False,
        journal_cite_score: str = "",
        journal_quartile: str = "",
        html_body: Optional[str] = None,
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
            if not html_body:
                html_body = self._build_premium_html_email(
                    body=body,
                    publisher_id=publisher_id,
                    publisher_name=pub_config.get("name", ""),
                    sender_email=sender_email,
                    journal_name=journal_name,
                    journal_link=journal_link,
                    submission_link=submission_link,
                    invitation_type=invitation_type,
                    scopus_indexed=scopus_indexed,
                    journal_cite_score=journal_cite_score,
                    journal_quartile=journal_quartile,
                )
            brevo_api_key = os.environ.get("BREVO_API_KEY", "").strip()
            if brevo_api_key and "brevo" in (pub_config.get("smtp_server") or "").lower():
                return self._send_email_brevo_api(
                    api_key=brevo_api_key,
                    publisher_name=pub_config.get("name", ""),
                    sender_email=sender_email,
                    to_email=to_email,
                    to_name=to_name,
                    subject=subject,
                    body=body,
                    html_body=html_body,
                    pdf_attachment=pdf_attachment,
                    attachment_filename=attachment_filename,
                )

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

    def _send_email_brevo_api(
        self,
        api_key: str,
        publisher_name: str,
        sender_email: str,
        to_email: str,
        to_name: Optional[str],
        subject: str,
        body: str,
        html_body: str,
        pdf_attachment: Optional[bytes],
        attachment_filename: str,
    ) -> Tuple[bool, str]:
        """Send a transactional email through Brevo API and return its message id."""
        payload = {
            "sender": {
                "name": publisher_name or sender_email,
                "email": sender_email,
            },
            "to": [
                {
                    "email": to_email,
                    **({"name": to_name} if to_name else {}),
                }
            ],
            "subject": subject,
            "htmlContent": html_body,
            "textContent": body,
        }
        if pdf_attachment:
            payload["attachment"] = [
                {
                    "name": attachment_filename,
                    "content": base64.b64encode(pdf_attachment).decode("ascii"),
                }
            ]

        try:
            response = requests.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={
                    "accept": "application/json",
                    "api-key": api_key,
                    "content-type": "application/json",
                },
                json=payload,
                timeout=45,
            )
        except requests.RequestException as exc:
            return False, f"Brevo API request failed: {exc}"

        response_text = response.text.strip()
        if response.status_code not in {200, 201, 202}:
            return False, f"Brevo API error {response.status_code}: {response_text[:500]}"

        message_id = ""
        try:
            message_id = response.json().get("messageId", "")
        except Exception:
            message_id = ""
        if message_id:
            return True, f"Brevo API accepted messageId={message_id}"
        return True, f"Brevo API accepted: {response_text[:500]}"

    def _text_to_html(self, text: str) -> str:
        """Backwards-compatible wrapper for premium HTML rendering."""
        return self._build_premium_html_email(
            body=text,
            publisher_id="brevo",
            publisher_name=self.get_publisher_name("brevo") or "Publisher",
            sender_email=self.get_publisher_email("brevo"),
            journal_name="",
            journal_link="",
            submission_link="",
            invitation_type="editorial",
            scopus_indexed=False,
            journal_cite_score="",
            journal_quartile="",
        )

    def render_html_preview(
        self,
        publisher_id: str,
        subject: str,
        body: str,
        journal_name: str = "",
        journal_link: str = "",
        submission_link: str = "",
        invitation_type: str = "publication",
        scopus_indexed: bool = True,
        journal_cite_score: str = "",
        journal_quartile: str = "",
    ) -> str:
        """Public helper for generating local HTML previews without sending."""
        return self._build_premium_html_email(
            body=body,
            publisher_id=publisher_id,
            publisher_name=self.get_publisher_name(publisher_id) or "Publisher",
            sender_email=self.get_publisher_email(publisher_id),
            journal_name=journal_name,
            journal_link=journal_link,
            submission_link=submission_link,
            subject=subject,
            invitation_type=invitation_type,
            scopus_indexed=scopus_indexed,
            journal_cite_score=journal_cite_score,
            journal_quartile=journal_quartile,
        )

    def _build_premium_html_email(
        self,
        body: str,
        publisher_id: str,
        publisher_name: str,
        sender_email: str,
        journal_name: str,
        journal_link: str,
        submission_link: str,
        invitation_type: str,
        scopus_indexed: bool,
        journal_cite_score: str,
        journal_quartile: str,
        subject: str = "",
    ) -> str:
        """Render a premium navy-themed HTML email with branding and CTA buttons."""
        logo_data_uri = self._get_logo_data_uri(publisher_id)
        invitation_label = self._invitation_label(invitation_type)
        cta_html = self._build_cta_buttons_html(
            submission_link=submission_link,
            journal_link=journal_link,
            sender_email=sender_email,
        )
        scopus_badge_html = self._build_scopus_badge_html(
            scopus_indexed=scopus_indexed,
            journal_quartile=journal_quartile,
            journal_cite_score=journal_cite_score,
        )
        body_html = self._format_body_html(body)
        safe_publisher_name = html.escape(publisher_name or "Publisher")
        safe_journal_name = html.escape(journal_name or "Academic Journal")
        safe_subject = html.escape(subject or "Invitation")
        safe_invitation_label = html.escape(invitation_label)

        logo_block = ""
        if logo_data_uri:
            logo_block = (
                '<img src="{src}" alt="{name} logo" '
            'style="display:block; max-width:220px; width:100%; height:auto; margin:0 auto 12px auto;">'
            ).format(src=logo_data_uri, name=safe_publisher_name)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{safe_subject}</title>
</head>
<body style="margin:0; padding:0; background:#08172b;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#08172b; background-image:linear-gradient(155deg, #08172b 0%, #123459 46%, #0b213b 100%); padding:30px 10px;">
    <tr>
      <td align="center">
                <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:760px; background:#fdfefe; border-radius:22px; overflow:hidden; border:1px solid #21456d; box-shadow:0 18px 40px rgba(4, 15, 30, 0.38);">
          <tr>
                        <td style="padding:0; background:linear-gradient(145deg, #091a32 0%, #0d2e55 55%, #165081 100%);">
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                                <tr>
                                    <td style="padding:12px 28px; border-bottom:1px solid #2e537e; font-family:Arial, Helvetica, sans-serif; font-size:11px; letter-spacing:1.2px; text-transform:uppercase; color:#d4e0ef;">
                                        {safe_invitation_label}
                                    </td>
                                </tr>
                                <tr>
                                    <td align="center" style="padding:24px 24px 26px 24px;">
                                        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="min-width:280px; max-width:620px; width:100%; border-radius:16px; border:1px solid #6f89ab; background:#18385b; background:rgba(255,255,255,0.08);">
                                            <tr>
                                                <td align="center" style="padding:16px 22px 18px 22px;">
                                                    {logo_block}
                                                    <div style="font-family:Georgia, 'Times New Roman', serif; font-size:30px; line-height:1.22; color:#ffffff; font-weight:bold; margin:0;">{safe_publisher_name}</div>
                                                    <div style="width:110px; height:2px; background:#d4af37; margin:12px auto 10px auto;"></div>
                                                    <div style="font-family:Arial, Helvetica, sans-serif; font-size:13px; line-height:1.4; color:#d8e6f5; margin-top:4px;">
                                                        Official invitation from {safe_journal_name}
                                                    </div>
                                                    {scopus_badge_html}
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                            </table>
            </td>
          </tr>
          <tr>
                        <td style="padding:30px 32px 18px 32px; font-family:Arial, Helvetica, sans-serif; color:#1f2a3d; font-size:15px; line-height:1.72; text-align:justify; text-justify:inter-word; background:#fdfefe;">
              {body_html}
            </td>
          </tr>
          <tr>
                        <td style="padding:0 32px 24px 32px;">
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border:1px solid #d6e3f2; border-radius:12px; background:#edf3fb;">
                                <tr>
                                    <td style="padding:14px 16px 6px 16px; font-family:Arial, Helvetica, sans-serif; font-size:13px; color:#254a72; letter-spacing:0.2px;">
                                        Continue with the next step
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding:0 16px 12px 16px;">
                                        {cta_html}
                                    </td>
                                </tr>
                            </table>
            </td>
          </tr>
          <tr>
                        <td style="padding:0 32px 30px 32px; font-family:Arial, Helvetica, sans-serif; color:#5f6f87; font-size:12px; line-height:1.62; border-top:1px solid #e2eaf5;">
                            <div style="padding-top:16px;">This invitation was sent by {safe_publisher_name}.</div>
              <div>If a button does not open, copy and paste the links from the message body.</div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    def _get_logo_data_uri(self, publisher_id: str) -> str:
        """Load and cache publisher logo as data URI for HTML email embedding."""
        cache_key = publisher_id or "brevo"
        if cache_key in self._logo_data_cache:
            return self._logo_data_cache[cache_key]

        public_logo_urls = {
            "peninsula": os.environ.get("PENINSULA_LOGO_URL", "").strip(),
            "brevo": os.environ.get("BREVO_LOGO_URL", "").strip(),
            "mesopotamian": os.environ.get("MESOPOTAMIAN_LOGO_URL", "").strip(),
        }
        public_logo_url = public_logo_urls.get(cache_key, "")
        if not public_logo_url:
            default_path = DEFAULT_PUBLISHER_LOGO_PATHS.get(cache_key, "")
            public_logo_url = self._build_public_asset_url(default_path)
        if self._is_http_url(public_logo_url):
            self._logo_data_cache[cache_key] = public_logo_url
            return public_logo_url

        logo_path = PUBLISHER_LOGOS.get(cache_key)
        if not logo_path:
            self._logo_data_cache[cache_key] = ""
            return ""
        if not logo_path.exists():
            self._logo_data_cache[cache_key] = ""
            return ""

        data_uri = self._build_transparent_logo_data_uri(logo_path)
        if data_uri:
            self._logo_data_cache[cache_key] = data_uri
            return data_uri

        fallback_mime = mimetypes.guess_type(logo_path.name)[0] or "image/jpeg"
        try:
            logo_raw = logo_path.read_bytes()
            encoded = base64.b64encode(logo_raw).decode("ascii")
            data_uri = f"data:{fallback_mime};base64,{encoded}"
        except Exception:
            self._logo_data_cache[cache_key] = ""
            return ""

        self._logo_data_cache[cache_key] = data_uri
        return data_uri

    def _get_scopus_logo_data_uri(self) -> str:
        """Load Scopus logo as data URI for metrics badge."""
        cache_key = "scopus_logo"
        if cache_key in self._asset_data_cache:
            return self._asset_data_cache[cache_key]

        public_scopus_logo_url = os.environ.get("SCOPUS_LOGO_URL", "").strip()
        if not public_scopus_logo_url:
            public_scopus_logo_url = self._build_public_asset_url(DEFAULT_SCOPUS_LOGO_PATH)
        if self._is_http_url(public_scopus_logo_url):
            self._asset_data_cache[cache_key] = public_scopus_logo_url
            return public_scopus_logo_url

        if not SCOPUS_LOGO_FILE.exists():
            self._asset_data_cache[cache_key] = ""
            return ""

        mime_type = mimetypes.guess_type(SCOPUS_LOGO_FILE.name)[0] or "image/jpeg"
        try:
            raw = SCOPUS_LOGO_FILE.read_bytes()
            encoded = base64.b64encode(raw).decode("ascii")
            data_uri = f"data:{mime_type};base64,{encoded}"
        except Exception:
            data_uri = ""

        self._asset_data_cache[cache_key] = data_uri
        return data_uri

    def _invitation_label(self, invitation_type: str) -> str:
        """Map internal invitation type to human-friendly header label."""
        normalized = (invitation_type or "").strip().lower()
        if normalized in {"publication", "author", "author_invitation"}:
            return "Author Invitation"
        return "Editorial Invitation"

    def _build_scopus_badge_html(self, scopus_indexed: bool, journal_quartile: str, journal_cite_score: str) -> str:
        """Render a Scimago-style Scopus badge with quartile and citescore."""
        if not scopus_indexed and not journal_quartile and not journal_cite_score:
            return ""

        quartile = html.escape((journal_quartile or "--").strip())
        cite_score = html.escape((journal_cite_score or "--").strip())
        scopus_logo = self._get_scopus_logo_data_uri()

        scopus_logo_html = ""
        if scopus_logo:
            scopus_logo_html = (
                '<img src="{src}" alt="Scopus" '
                'style="display:block; max-width:140px; width:100%; height:auto;">'
            ).format(src=scopus_logo)
        else:
            scopus_logo_html = (
                '<div style="font-family:Arial, Helvetica, sans-serif; font-size:20px; '
                'font-weight:700; letter-spacing:0.3px; color:#f38b00;">Scopus</div>'
            )

        return f"""
                          <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:14px auto 0 auto; border:1px solid #cad7e6; border-radius:11px; background:#ffffff; min-width:320px;">
                            <tr>
                              <td style="padding:8px 10px 8px 10px; border-right:1px solid #e1eaf4; min-width:150px;">{scopus_logo_html}</td>
                              <td style="padding:7px 10px; border-right:1px solid #e1eaf4; text-align:center; min-width:74px;">
                                <div style="font-family:Arial, Helvetica, sans-serif; font-size:19px; font-weight:700; color:#1d385d; line-height:1;">{quartile}</div>
                                <div style="font-family:Arial, Helvetica, sans-serif; font-size:10px; text-transform:uppercase; letter-spacing:0.8px; color:#5f7696; margin-top:4px;">Quartile</div>
                              </td>
                              <td style="padding:7px 10px; text-align:center; min-width:86px;">
                                <div style="font-family:Arial, Helvetica, sans-serif; font-size:19px; font-weight:700; color:#1d385d; line-height:1;">{cite_score}</div>
                                <div style="font-family:Arial, Helvetica, sans-serif; font-size:10px; text-transform:uppercase; letter-spacing:0.8px; color:#5f7696; margin-top:4px;">CiteScore</div>
                              </td>
                            </tr>
                          </table>
        """

    def _build_transparent_logo_data_uri(self, logo_path: Path) -> str:
        """Build a transparent PNG data URI by removing bright backgrounds."""
        try:
            from PIL import Image
        except Exception:
            return ""

        try:
            image = Image.open(logo_path).convert("RGBA")
        except Exception:
            return ""

        alpha_extrema = image.getchannel("A").getextrema()
        # If the source already has transparency, preserve it as-is.
        if alpha_extrema and alpha_extrema[0] < 255:
            output = io.BytesIO()
            image.save(output, format="PNG")
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
            return f"data:image/png;base64,{encoded}"

        pixels = image.getdata()
        converted = []
        for r, g, b, a in pixels:
            if r >= 242 and g >= 242 and b >= 242:
                converted.append((255, 255, 255, 0))
            elif r >= 228 and g >= 228 and b >= 228:
                converted.append((r, g, b, int(a * 0.35)))
            else:
                converted.append((r, g, b, a))
        image.putdata(converted)

        output = io.BytesIO()
        image.save(output, format="PNG")
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def _format_body_html(self, text: str) -> str:
        """Convert plain text to polished HTML paragraphs and lists."""
        lines = text.splitlines()
        blocks: list[list[str]] = []
        current = []
        for raw_line in lines:
            line = raw_line.rstrip()
            if line.strip():
                current.append(line)
            else:
                if current:
                    blocks.append(current)
                    current = []
        if current:
            blocks.append(current)

        normalized_blocks: list[dict] = []
        for block in blocks:
            stripped = [line.strip() for line in block if line.strip()]
            if not stripped:
                continue

            list_items = [line[2:].strip() for line in stripped if line.startswith("- ") and line[2:].strip()]
            non_list_lines = [line for line in stripped if not line.startswith("- ")]

            # Keep heading text and publication bullets as separate semantic blocks.
            if list_items and non_list_lines:
                heading_text = re.sub(r"\s+", " ", " ".join(non_list_lines)).strip()
                if heading_text:
                    normalized_blocks.append({"type": "text", "value": heading_text})
                normalized_blocks.append({"type": "list", "value": list_items})
                continue

            is_list = len(list_items) == len(stripped)
            if is_list:
                normalized_blocks.append({
                    "type": "list",
                    "value": list_items
                })
            else:
                joined = " ".join(stripped)
                joined = re.sub(r"\s+", " ", joined).strip()
                if joined:
                    normalized_blocks.append({"type": "text", "value": joined})

        compacted: list[dict] = []
        prose_buffer: list[str] = []

        def _flush_prose_buffer():
            if prose_buffer:
                compacted.append({"type": "text", "value": " ".join(prose_buffer).strip()})
                prose_buffer.clear()

        for block in normalized_blocks:
            if block["type"] == "list":
                _flush_prose_buffer()
                compacted.append(block)
                continue

            text_block = block["value"]
            lower = text_block.lower()
            has_url = "http://" in lower or "https://" in lower or "mailto:" in lower
            signature_or_heading = (
                lower.startswith("dear ")
                or lower.startswith("warm regards")
                or lower.startswith("with best regards")
                or lower.startswith("with highest regards")
                or lower.startswith("sincerely")
                or lower.startswith("editorial office")
                or lower.startswith("submission link")
                or lower.startswith("submission portal")
                or lower.startswith("journal website")
                or lower.startswith("email:")
                or "recent publications such as" in lower
            )

            if signature_or_heading or has_url:
                _flush_prose_buffer()
                compacted.append({"type": "text", "value": text_block})
                continue

            prose_buffer.append(text_block)
            if len(prose_buffer) >= 3 or len(" ".join(prose_buffer)) >= 520:
                _flush_prose_buffer()

        _flush_prose_buffer()

        html_blocks = []
        for block in compacted:
            if block["type"] == "list":
                items = "".join(
                    f'<li style="margin:0 0 8px 0; text-align:justify; text-justify:inter-word;">{html.escape(item)}</li>'
                    for item in block["value"]
                )
                html_blocks.append('<ul style="padding-left:20px; margin:0 0 16px 0;">' + items + "</ul>")
                continue

            paragraph = html.escape(block["value"])
            paragraph = re.sub(r"(https?://[^\s<]+)", r'<a href="\1" target="_blank" style="color:#0f3a6d; text-decoration:underline;">\1</a>', paragraph)
            paragraph = paragraph.replace("Submission portal: ", "Submission portal:<br>")
            paragraph = paragraph.replace("Journal website: ", "Journal website:<br>")
            paragraph = paragraph.replace(" Email:", "<br>Email:")
            paragraph = paragraph.replace(" Website:", "<br>Website:")
            html_blocks.append(
                '<p style="margin:0 0 14px 0; text-align:justify; text-justify:inter-word;">'
                + paragraph
                + "</p>"
            )

        return "\n".join(html_blocks)

    def _build_cta_buttons_html(self, submission_link: str, journal_link: str, sender_email: str) -> str:
        """Build CTA buttons for submission, journal view, and direct reply."""
        buttons = []
        if submission_link:
            buttons.append(("Submit Manuscript", submission_link.strip(), True))
        if journal_link:
            buttons.append(("View Journal Website", journal_link.strip(), False))
        if sender_email:
            buttons.append(("Reply to Editorial Office", f"mailto:{sender_email.strip()}", False))

        if not buttons:
            return ""

        links_html = []
        for label, link, primary in buttons:
            bg = "#0d335f" if primary else "#ffffff"
            border = "#0b2c50" if primary else "#2a4f7d"
            text_color = "#ffffff" if primary else "#1a3f67"
            links_html.append(
                '<a href="{href}" target="_blank" '
                'style="display:inline-block; margin:0 10px 10px 0; padding:12px 19px; '
                'font-family:Arial, Helvetica, sans-serif; font-size:14px; font-weight:600; '
                'line-height:1; color:{text}; text-decoration:none; background:{bg}; '
                'border:1px solid {border}; border-radius:999px;">{label}</a>'.format(
                    href=html.escape(link, quote=True),
                    text=text_color,
                    bg=bg,
                    border=border,
                    label=html.escape(label),
                )
            )

        return (
            '<div style="font-family:Arial, Helvetica, sans-serif; font-size:12px; color:#4e6483; margin-bottom:10px;">'
            "Quick links"
            "</div>"
            '<div style="margin:0;">' + "".join(links_html) + "</div>"
        )

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
