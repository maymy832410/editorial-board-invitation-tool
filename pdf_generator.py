"""Premium PDF generator for invitation letters with decorative borders and shapes."""

import io
from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch, cm, mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, 
    Table, TableStyle, HRFlowable, BaseDocTemplate,
    PageTemplate, Frame
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.pdfgen import canvas


# Logo file paths
LOGO_DIR = Path(__file__).parent / "pdf template"

PUBLISHER_LOGOS = {
    "peninsula": LOGO_DIR / "Peninsula publishing press.jpg",
    "mesopotamian": LOGO_DIR / "mesopotamian academic press.jpg",
}

# Publisher branding colors
PUBLISHER_COLORS = {
    "peninsula": {
        "primary": colors.HexColor('#1a365d'),      # Navy blue
        "secondary": colors.HexColor('#2c5282'),    # Medium blue
        "accent": colors.HexColor('#3182ce'),       # Light blue
        "gold": colors.HexColor('#d69e2e'),         # Gold accent
        "text": colors.HexColor('#2d3748'),         # Dark gray
        "light": colors.HexColor('#e2e8f0'),        # Light gray
        "bg_tint": colors.HexColor('#f7fafc'),      # Very light blue
    },
    "mesopotamian": {
        "primary": colors.HexColor('#7c2d12'),      # Burnt orange/brown
        "secondary": colors.HexColor('#9a3412'),    # Orange
        "accent": colors.HexColor('#c2410c'),       # Bright orange
        "gold": colors.HexColor('#b45309'),         # Amber
        "text": colors.HexColor('#292524'),         # Stone dark
        "light": colors.HexColor('#fef3c7'),        # Cream
        "bg_tint": colors.HexColor('#fffbeb'),      # Very light cream
    }
}

# Publisher contact info
PUBLISHER_INFO = {
    "peninsula": {
        "name": "Peninsula Publishing Press",
        "email": "info@peninsula-press.ae",
        "website": "www.peninsula-press.ae",
        "location": "Dubai, UAE"
    },
    "mesopotamian": {
        "name": "Mesopotamian Academic Press", 
        "email": "info@mesopotamian.press",
        "website": "www.mesopotamian.press",
        "location": "Iraq"
    }
}


class PremiumPDFGenerator:
    """Generate premium PDF invitation letters with decorative borders and shapes."""
    
    def __init__(self, publisher_id: str = "peninsula"):
        self.publisher_id = publisher_id
        self.colors = PUBLISHER_COLORS.get(publisher_id, PUBLISHER_COLORS["peninsula"])
        self.info = PUBLISHER_INFO.get(publisher_id, PUBLISHER_INFO["peninsula"])
        self.page_width, self.page_height = A4
        self._setup_styles()
    
    def _setup_styles(self):
        """Setup custom paragraph styles with premium typography."""
        
        # Title style
        self.title_style = ParagraphStyle(
            name='LetterTitle',
            fontSize=15,
            leading=20,
            alignment=TA_CENTER,
            spaceAfter=20,
            spaceBefore=5,
            textColor=self.colors['primary'],
            fontName='Helvetica-Bold'
        )
        
        # Body text style
        self.body_style = ParagraphStyle(
            name='LetterBody',
            fontSize=11,
            leading=17,
            alignment=TA_JUSTIFY,
            spaceAfter=12,
            firstLineIndent=0,
            textColor=self.colors['text'],
            fontName='Helvetica'
        )
        
        # Greeting style
        self.greeting_style = ParagraphStyle(
            name='Greeting',
            fontSize=11,
            leading=17,
            spaceAfter=18,
            spaceBefore=15,
            textColor=self.colors['text'],
            fontName='Helvetica'
        )
        
        # Signature style
        self.signature_style = ParagraphStyle(
            name='Signature',
            fontSize=11,
            leading=15,
            spaceBefore=8,
            spaceAfter=3,
            textColor=self.colors['text'],
            fontName='Helvetica'
        )
        
        # Footer style
        self.footer_style = ParagraphStyle(
            name='Footer',
            fontSize=9,
            alignment=TA_CENTER,
            textColor=self.colors['secondary'],
            fontName='Helvetica'
        )
    
    def _draw_premium_border(self, c: canvas.Canvas):
        """Draw premium decorative border with corner ornaments."""
        
        width = self.page_width
        height = self.page_height
        margin = 15 * mm
        
        primary = self.colors['primary']
        gold = self.colors['gold']
        light = self.colors['light']
        
        # Outer border - thick line
        c.setStrokeColor(primary)
        c.setLineWidth(2.5)
        c.rect(margin, margin, width - 2*margin, height - 2*margin, stroke=1, fill=0)
        
        # Inner border - thin line with slight offset
        inner_offset = 4 * mm
        c.setStrokeColor(gold)
        c.setLineWidth(0.8)
        c.rect(
            margin + inner_offset, 
            margin + inner_offset, 
            width - 2*margin - 2*inner_offset, 
            height - 2*margin - 2*inner_offset, 
            stroke=1, fill=0
        )
        
        # Corner ornaments
        corner_size = 12 * mm
        ornament_offset = margin + 1 * mm
        
        # Draw corner L-shapes with gold
        c.setStrokeColor(gold)
        c.setLineWidth(2)
        
        # Top-left corner
        c.line(ornament_offset, height - ornament_offset, 
               ornament_offset + corner_size, height - ornament_offset)
        c.line(ornament_offset, height - ornament_offset, 
               ornament_offset, height - ornament_offset - corner_size)
        
        # Top-right corner
        c.line(width - ornament_offset, height - ornament_offset, 
               width - ornament_offset - corner_size, height - ornament_offset)
        c.line(width - ornament_offset, height - ornament_offset, 
               width - ornament_offset, height - ornament_offset - corner_size)
        
        # Bottom-left corner
        c.line(ornament_offset, ornament_offset, 
               ornament_offset + corner_size, ornament_offset)
        c.line(ornament_offset, ornament_offset, 
               ornament_offset, ornament_offset + corner_size)
        
        # Bottom-right corner
        c.line(width - ornament_offset, ornament_offset, 
               width - ornament_offset - corner_size, ornament_offset)
        c.line(width - ornament_offset, ornament_offset, 
               width - ornament_offset, ornament_offset + corner_size)
        
        # Corner diamonds/dots
        c.setFillColor(gold)
        diamond_size = 3 * mm
        
        corners = [
            (ornament_offset + 1*mm, height - ornament_offset - 1*mm),
            (width - ornament_offset - 1*mm, height - ornament_offset - 1*mm),
            (ornament_offset + 1*mm, ornament_offset + 1*mm),
            (width - ornament_offset - 1*mm, ornament_offset + 1*mm),
        ]
        
        for cx, cy in corners:
            # Draw diamond shape
            c.saveState()
            c.translate(cx, cy)
            c.rotate(45)
            c.rect(-diamond_size/2, -diamond_size/2, diamond_size, diamond_size, stroke=0, fill=1)
            c.restoreState()
    
    def _draw_header_decoration(self, c: canvas.Canvas):
        """Draw decorative header line under logo area."""
        
        width = self.page_width
        y_pos = self.page_height - 85 * mm  # Position below logo
        margin = 25 * mm
        
        # Center decorative element
        center_x = width / 2
        line_width = width - 2 * margin
        
        # Main horizontal line
        c.setStrokeColor(self.colors['primary'])
        c.setLineWidth(1.5)
        c.line(margin, y_pos, center_x - 20*mm, y_pos)
        c.line(center_x + 20*mm, y_pos, width - margin, y_pos)
        
        # Center diamond ornament
        c.setFillColor(self.colors['gold'])
        c.setStrokeColor(self.colors['primary'])
        c.setLineWidth(1)
        
        # Diamond
        size = 6 * mm
        c.saveState()
        c.translate(center_x, y_pos)
        c.rotate(45)
        c.rect(-size/2, -size/2, size, size, stroke=1, fill=1)
        c.restoreState()
        
        # Small circles on either side
        c.setFillColor(self.colors['primary'])
        c.circle(center_x - 25*mm, y_pos, 2*mm, stroke=0, fill=1)
        c.circle(center_x + 25*mm, y_pos, 2*mm, stroke=0, fill=1)
    
    def _draw_footer_decoration(self, c: canvas.Canvas):
        """Draw decorative footer."""
        
        width = self.page_width
        y_pos = 35 * mm
        margin = 25 * mm
        center_x = width / 2
        
        # Horizontal lines
        c.setStrokeColor(self.colors['light'])
        c.setLineWidth(0.5)
        c.line(margin, y_pos, width - margin, y_pos)
        
        # Footer text
        c.setFillColor(self.colors['secondary'])
        c.setFont('Helvetica', 8)
        
        footer_text = f"{self.info['name']}  |  {self.info['location']}  |  {self.info['email']}  |  {self.info['website']}"
        c.drawCentredString(center_x, y_pos - 12*mm, footer_text)
    
    def _draw_watermark_pattern(self, c: canvas.Canvas):
        """Draw subtle watermark/pattern in background."""
        
        # Very subtle diagonal lines in corners
        c.setStrokeColor(self.colors['light'])
        c.setLineWidth(0.3)
        c.setStrokeAlpha(0.3)
        
        # Top right corner subtle pattern
        for i in range(5):
            offset = i * 8 * mm
            c.line(
                self.page_width - 50*mm + offset, 
                self.page_height - 25*mm,
                self.page_width - 25*mm,
                self.page_height - 50*mm + offset
            )
        
        # Bottom left corner subtle pattern
        for i in range(5):
            offset = i * 8 * mm
            c.line(
                25*mm, 
                50*mm - offset,
                50*mm - offset,
                25*mm
            )
        
        c.setStrokeAlpha(1)  # Reset alpha
    
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
        
        # Create canvas for custom drawing
        c = canvas.Canvas(buffer, pagesize=A4)
        
        # Draw background elements
        self._draw_premium_border(c)
        self._draw_watermark_pattern(c)
        self._draw_header_decoration(c)
        self._draw_footer_decoration(c)
        
        # Draw logo
        self._draw_logo(c)
        
        # Draw content
        self._draw_content(c, subject, email_body)
        
        c.save()
        
        buffer.seek(0)
        return buffer.getvalue()
    
    def _draw_logo(self, c: canvas.Canvas):
        """Draw logo at top center."""
        
        logo_path = PUBLISHER_LOGOS.get(self.publisher_id)
        
        if logo_path and logo_path.exists():
            try:
                from reportlab.lib.utils import ImageReader
                
                img = ImageReader(str(logo_path))
                img_width, img_height = img.getSize()
                
                # Scale logo
                max_width = 100 * mm
                max_height = 35 * mm
                
                scale = min(max_width / img_width, max_height / img_height)
                draw_width = img_width * scale
                draw_height = img_height * scale
                
                # Center position
                x = (self.page_width - draw_width) / 2
                y = self.page_height - 25*mm - draw_height
                
                c.drawImage(str(logo_path), x, y, draw_width, draw_height, mask='auto')
                
            except Exception:
                self._draw_text_logo(c)
        else:
            self._draw_text_logo(c)
    
    def _draw_text_logo(self, c: canvas.Canvas):
        """Fallback text logo."""
        c.setFillColor(self.colors['primary'])
        c.setFont('Helvetica-Bold', 18)
        c.drawCentredString(self.page_width/2, self.page_height - 50*mm, self.info['name'])
    
    def _draw_content(self, c: canvas.Canvas, subject: str, email_body: str):
        """Draw the main letter content."""
        
        # Content area
        left_margin = 30 * mm
        right_margin = 30 * mm
        top_start = self.page_height - 95 * mm  # Below header decoration
        line_height = 5 * mm
        
        current_y = top_start
        content_width = self.page_width - left_margin - right_margin
        
        # Subject/Title
        if subject:
            c.setFillColor(self.colors['primary'])
            c.setFont('Helvetica-Bold', 14)
            
            # Center the subject
            c.drawCentredString(self.page_width/2, current_y, subject)
            current_y -= 20 * mm
        
        # Body content
        c.setFillColor(self.colors['text'])
        c.setFont('Helvetica', 11)
        
        paragraphs = email_body.split('\n\n')
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            
            # Handle line breaks within paragraph
            lines = para.split('\n')
            
            for line in lines:
                line = line.strip()
                if not line:
                    current_y -= 3 * mm
                    continue
                
                # Word wrap
                words = line.split()
                current_line = ""
                
                for word in words:
                    test_line = current_line + " " + word if current_line else word
                    text_width = c.stringWidth(test_line, 'Helvetica', 11)
                    
                    if text_width < content_width:
                        current_line = test_line
                    else:
                        # Draw current line
                        c.drawString(left_margin, current_y, current_line)
                        current_y -= line_height
                        current_line = word
                
                # Draw remaining text
                if current_line:
                    c.drawString(left_margin, current_y, current_line)
                    current_y -= line_height
            
            # Paragraph spacing
            current_y -= 3 * mm


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
        publisher_id: Publisher ID ('peninsula' or 'mesopotamian')
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
