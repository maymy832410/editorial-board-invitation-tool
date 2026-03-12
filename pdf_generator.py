"""Premium PDF generator for invitation letters with clean, professional design."""

import io
from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


LOGO_DIR = Path(__file__).parent / "pdf template"

PUBLISHER_LOGOS = {
    "peninsula": LOGO_DIR / "Peninsula publishing press.jpg",
    "brevo": LOGO_DIR / "Peninsula publishing press.jpg",
    "mesopotamian": LOGO_DIR / "mesopotamian academic press.jpg",
}

_PENINSULA_COLORS = {
    "primary": colors.HexColor('#1a365d'),
    "secondary": colors.HexColor('#2c5282'),
    "accent": colors.HexColor('#3182ce'),
    "gold": colors.HexColor('#c9a84c'),
    "text": colors.HexColor('#2d3748'),
    "light": colors.HexColor('#e2e8f0'),
    "bg_tint": colors.HexColor('#f7fafc'),
}
PUBLISHER_COLORS = {
    "peninsula": _PENINSULA_COLORS,
    "brevo": _PENINSULA_COLORS,
    "mesopotamian": {
        "primary": colors.HexColor('#5c1a0a'),
        "secondary": colors.HexColor('#7c2d12'),
        "accent": colors.HexColor('#c2410c'),
        "gold": colors.HexColor('#b8860b'),
        "text": colors.HexColor('#292524'),
        "light": colors.HexColor('#fef3c7'),
        "bg_tint": colors.HexColor('#fffbeb'),
    }
}

_PENINSULA_INFO = {
    "name": "Peninsula Publishing Press",
    "email": "info@peninsula-press.ae",
    "website": "www.peninsula-press.ae",
    "location": "Dubai, UAE"
}
PUBLISHER_INFO = {
    "peninsula": _PENINSULA_INFO,
    "brevo": _PENINSULA_INFO,
    "mesopotamian": {
        "name": "Mesopotamian Academic Press",
        "email": "info@mesopotamian.press",
        "website": "www.mesopotamian.press",
        "location": "Iraq"
    }
}


class PremiumPDFGenerator:
    """Generate premium PDF invitation letters with a clean, Canva-style design."""

    def __init__(self, publisher_id: str = "brevo"):
        self.publisher_id = publisher_id
        self.colors = PUBLISHER_COLORS.get(publisher_id, PUBLISHER_COLORS["brevo"])
        self.info = PUBLISHER_INFO.get(publisher_id, PUBLISHER_INFO["brevo"])
        self.page_width, self.page_height = A4

    def _draw_header(self, c: canvas.Canvas):
        """Draw logo centered at top, with a single accent line beneath."""
        logo_path = PUBLISHER_LOGOS.get(self.publisher_id)
        logo_bottom = self.page_height - 55 * mm

        if logo_path and logo_path.exists():
            try:
                from reportlab.lib.utils import ImageReader
                img = ImageReader(str(logo_path))
                img_w, img_h = img.getSize()
                max_w, max_h = 90 * mm, 30 * mm
                scale = min(max_w / img_w, max_h / img_h)
                dw, dh = img_w * scale, img_h * scale
                x = (self.page_width - dw) / 2
                y = self.page_height - 20 * mm - dh
                c.drawImage(str(logo_path), x, y, dw, dh, mask='auto')
                logo_bottom = y - 6 * mm
            except Exception:
                self._draw_text_logo(c)
                logo_bottom = self.page_height - 55 * mm
        else:
            self._draw_text_logo(c)

        margin = 28 * mm
        c.setStrokeColor(self.colors['gold'])
        c.setLineWidth(2.5)
        c.line(margin, logo_bottom, self.page_width - margin, logo_bottom)

        c.setStrokeColor(self.colors['primary'])
        c.setLineWidth(0.5)
        c.line(margin, logo_bottom - 3 * mm, self.page_width - margin, logo_bottom - 3 * mm)

        return logo_bottom - 12 * mm

    def _draw_text_logo(self, c: canvas.Canvas):
        """Fallback text logo when image is missing."""
        c.setFillColor(self.colors['primary'])
        c.setFont('Helvetica-Bold', 20)
        c.drawCentredString(self.page_width / 2, self.page_height - 42 * mm, self.info['name'])

    def _draw_footer(self, c: canvas.Canvas):
        """Draw a minimal footer strip with publisher details."""
        y = 22 * mm
        margin = 28 * mm

        c.setStrokeColor(self.colors['light'])
        c.setLineWidth(0.5)
        c.line(margin, y, self.page_width - margin, y)

        c.setFillColor(self.colors['secondary'])
        c.setFont('Helvetica', 7.5)
        parts = [self.info['name'], self.info['location'], self.info['email'], self.info['website']]
        footer_text = "   |   ".join(parts)
        c.drawCentredString(self.page_width / 2, y - 10 * mm, footer_text)

    def _draw_side_accent(self, c: canvas.Canvas):
        """Draw a thin vertical accent stripe on the left edge."""
        stripe_w = 4 * mm
        c.setFillColor(self.colors['primary'])
        c.rect(0, 0, stripe_w, self.page_height, stroke=0, fill=1)

        c.setFillColor(self.colors['gold'])
        c.rect(stripe_w, 0, 1.2 * mm, self.page_height, stroke=0, fill=1)

    def _draw_content(self, c: canvas.Canvas, subject: str, email_body: str, content_top: float):
        """Draw subject and body text with proper word-wrapping and pagination."""
        left_margin = 30 * mm
        content_width = self.page_width - left_margin - 28 * mm
        line_height = 5.2 * mm
        footer_limit = 35 * mm
        current_y = content_top

        def new_page():
            nonlocal current_y
            c.showPage()
            self._draw_side_accent(c)
            self._draw_footer(c)
            current_y = self.page_height - 25 * mm

        def check_space(needed=line_height):
            nonlocal current_y
            if current_y - needed < footer_limit:
                new_page()

        if subject:
            check_space(12 * mm)
            c.setFillColor(self.colors['primary'])
            c.setFont('Helvetica-Bold', 13)
            # Word-wrap the subject across multiple lines if needed
            subject_words = subject.split()
            subj_line = ""
            for w in subject_words:
                test = subj_line + " " + w if subj_line else w
                if c.stringWidth(test, 'Helvetica-Bold', 13) < content_width:
                    subj_line = test
                else:
                    c.drawCentredString(self.page_width / 2, current_y, subj_line)
                    current_y -= 6 * mm
                    check_space()
                    subj_line = w
            if subj_line:
                c.drawCentredString(self.page_width / 2, current_y, subj_line)
                current_y -= 14 * mm

        c.setFillColor(self.colors['text'])
        c.setFont('Helvetica', 10.5)
        font_name = 'Helvetica'
        font_size = 10.5

        paragraphs = email_body.split('\n\n')
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            lines = para.split('\n')
            for line in lines:
                line = line.strip()
                if not line:
                    current_y -= 2.5 * mm
                    continue

                is_bullet = line.startswith('- ') or line.startswith('* ')
                indent = 5 * mm if is_bullet else 0
                effective_width = content_width - indent
                if is_bullet:
                    line = line[2:]

                words = line.split()
                current_line = ""
                first_of_bullet = True
                for word in words:
                    test = current_line + " " + word if current_line else word
                    if c.stringWidth(test, font_name, font_size) < effective_width:
                        current_line = test
                    else:
                        check_space()
                        prefix = "\u2022  " if (is_bullet and first_of_bullet) else ("   " if is_bullet else "")
                        c.drawString(left_margin + indent - (5 * mm if is_bullet and first_of_bullet else 0),
                                     current_y, prefix + current_line)
                        current_y -= line_height
                        current_line = word
                        first_of_bullet = False

                if current_line:
                    check_space()
                    prefix = "\u2022  " if (is_bullet and first_of_bullet) else ("   " if is_bullet else "")
                    c.drawString(left_margin + indent - (5 * mm if is_bullet and first_of_bullet else 0),
                                 current_y, prefix + current_line)
                    current_y -= line_height

            current_y -= 3 * mm

    def generate_invitation_pdf(
        self,
        recipient_name: str,
        email_body: str,
        subject: str,
        journal_name: str = "",
        journal_link: str = ""
    ) -> bytes:
        """Generate a premium PDF invitation letter."""
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)

        self._draw_side_accent(c)
        content_top = self._draw_header(c)
        self._draw_footer(c)
        self._draw_content(c, subject, email_body, content_top)

        c.save()
        buffer.seek(0)
        return buffer.getvalue()


def generate_invitation_pdf(
    publisher_id: str,
    recipient_name: str,
    email_body: str,
    subject: str,
    journal_name: str = "",
    journal_link: str = ""
) -> bytes:
    """
    Generate a premium PDF invitation letter.

    Args:
        publisher_id: Publisher ID ('brevo', 'peninsula', or 'mesopotamian')
        recipient_name: Name of the recipient
        email_body: The email body text
        subject: Email subject (used as title)
        journal_name: Journal name
        journal_link: Journal link

    Returns:
        PDF file as bytes
    """
    generator = PremiumPDFGenerator(publisher_id=publisher_id)
    return generator.generate_invitation_pdf(
        recipient_name=recipient_name,
        email_body=email_body,
        subject=subject,
        journal_name=journal_name,
        journal_link=journal_link
    )
