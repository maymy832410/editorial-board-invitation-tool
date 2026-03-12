"""Professional email invitation templates for editorial positions."""

from typing import Dict


TEMPLATE_BOARD_MEMBER = "board_member"
TEMPLATE_MANAGING_EDITOR = "managing_editor"
TEMPLATE_EDITOR_IN_CHIEF = "editor_in_chief"

# Base template IDs that appear in the UI dropdown
_BASE_TEMPLATES = [TEMPLATE_BOARD_MEMBER, TEMPLATE_MANAGING_EDITOR, TEMPLATE_EDITOR_IN_CHIEF]

TEMPLATES = {
    # ── Editorial Board Member (non-Scopus) ──
    TEMPLATE_BOARD_MEMBER: {
        "name": "Editorial Board Member",
        "subject": "Invitation to Join {journal_name} Editorial Board",
        "body": """Dear Prof. {author_name},

On behalf of the editorial leadership of {journal_name}, we are pleased to invite you to join the journal's Editorial Board.

{journal_name} is currently advancing its editorial standards in line with COPE principles and international publishing best practices. We are forming a renewed Editorial Board of distinguished scholars whose expertise will support the journal's academic quality, peer-review process, and international visibility.

The journal is actively working toward indexing in major international databases, including Scopus, and your participation would significantly strengthen this effort.

Given your respected academic profile, we would be honored by your acceptance of this invitation.

We look forward to the possibility of working with you.

Warm regards,

Editorial Office
{journal_name}
{publisher_location}
{journal_link}"""
    },

    # ── Editorial Board Member (Scopus) ──
    "board_member_scopus": {
        "name": "Editorial Board Member",
        "subject": "Invitation to Join {journal_name} Editorial Board (Scopus-Indexed)",
        "body": """Dear Prof. {author_name},

On behalf of the editorial leadership of {journal_name}, we are pleased to invite you to join the journal's Editorial Board.

{journal_name} is a Scopus-indexed journal committed to advancing its editorial standards in line with COPE principles and international publishing best practices. We are forming a renewed Editorial Board of distinguished scholars whose expertise will support the journal's academic quality, peer-review process, and international visibility.

Given your respected academic profile, we would be honored by your acceptance of this invitation.

We look forward to the possibility of working with you.

Warm regards,

Editorial Office
{journal_name}
{publisher_location}
{journal_link}"""
    },

    # ── Managing Editor (non-Scopus) ──
    TEMPLATE_MANAGING_EDITOR: {
        "name": "Managing Editor",
        "subject": "Invitation to Serve as Managing Editor - {journal_name}",
        "body": """Dear Professor {author_name},

I hope this letter finds you in excellent health and spirits.

On behalf of {journal_name} (ISSN: {journal_issn}), published by {publisher_name}, I am pleased to extend an invitation for you to serve as Managing Editor of our journal.

Given your distinguished academic career, extensive publication record, and recognized expertise in the field, we believe you would be an exceptional addition to our editorial leadership team.

As Managing Editor, your responsibilities would include:

- Overseeing the day-to-day editorial operations
- Managing manuscript workflow and peer review process
- Coordinating with Editorial Board members and reviewers
- Ensuring timely publication of accepted manuscripts
- Contributing to strategic planning and journal development
- Maintaining publication quality and ethical standards

This position offers:
- A prominent leadership role in a growing academic journal
- Certificate of appointment as Managing Editor
- Opportunity to shape the direction of scholarly discourse in your field
- Networking with leading researchers worldwide
- Recognition on all journal publications and communications

{journal_name} is dedicated to publishing innovative, high-impact research and is actively pursuing indexing in Scopus and other major international databases. Your leadership would be instrumental in achieving this milestone and contributing to our continued growth and excellence.

Please visit {journal_link} to familiarize yourself with our journal's scope, recent issues, and editorial policies.

To express your interest or discuss this opportunity further, please reply to this email with your CV and a brief statement of interest.

We eagerly await your favorable response.

With highest regards,

{editor_in_chief_name}
Editor-in-Chief
{journal_name}
{publisher_name}
Email: {sender_email}
Website: {journal_link}"""
    },

    # ── Managing Editor (Scopus) ──
    "managing_editor_scopus": {
        "name": "Managing Editor",
        "subject": "Invitation to Serve as Managing Editor - {journal_name} (Scopus-Indexed)",
        "body": """Dear Professor {author_name},

I hope this letter finds you in excellent health and spirits.

On behalf of {journal_name} (ISSN: {journal_issn}), published by {publisher_name}, I am pleased to extend an invitation for you to serve as Managing Editor of our journal.

{journal_name} is a Scopus-indexed journal recognized for its commitment to rigorous peer review and high-quality scholarship. Given your distinguished academic career, extensive publication record, and recognized expertise in the field, we believe you would be an exceptional addition to our editorial leadership team.

As Managing Editor, your responsibilities would include:

- Overseeing the day-to-day editorial operations
- Managing manuscript workflow and peer review process
- Coordinating with Editorial Board members and reviewers
- Ensuring timely publication of accepted manuscripts
- Contributing to strategic planning and journal development
- Maintaining publication quality and ethical standards

This position offers:
- A prominent leadership role in a Scopus-indexed academic journal
- Certificate of appointment as Managing Editor
- Opportunity to shape the direction of scholarly discourse in your field
- Networking with leading researchers worldwide
- Recognition on all journal publications and communications

{journal_name} is dedicated to publishing innovative, high-impact research. We are confident that your leadership would significantly contribute to our continued growth and excellence.

Please visit {journal_link} to familiarize yourself with our journal's scope, recent issues, and editorial policies.

To express your interest or discuss this opportunity further, please reply to this email with your CV and a brief statement of interest.

We eagerly await your favorable response.

With highest regards,

{editor_in_chief_name}
Editor-in-Chief
{journal_name}
{publisher_name}
Email: {sender_email}
Website: {journal_link}"""
    },

    # ── Editor-in-Chief (non-Scopus) ──
    TEMPLATE_EDITOR_IN_CHIEF: {
        "name": "Editor-in-Chief",
        "subject": "Distinguished Invitation: Editor-in-Chief Position - {journal_name}",
        "body": """Dear Professor {author_name},

I hope this message finds you well.

It is with great pleasure that I write to you on behalf of {publisher_name} to extend a distinguished invitation to serve as Editor-in-Chief of {journal_name} (ISSN: {journal_issn}).

Your outstanding scholarly achievements, extensive publication record, and recognized leadership in the academic community make you our ideal candidate for this prestigious position.

As Editor-in-Chief, you would:

- Provide visionary leadership and strategic direction for the journal
- Chair the Editorial Board and guide editorial policies
- Make final decisions on manuscript acceptance
- Represent the journal at conferences and academic forums
- Foster relationships with the global research community
- Ensure the highest standards of publication ethics and quality
- Guide the journal's growth and international recognition
- Lead the journal's efforts toward Scopus indexing and broader international database inclusion

We offer:
- Complete editorial autonomy within established ethical guidelines
- Full administrative and technical support from our publishing team
- Competitive honorarium commensurate with the position
- Platform to advance your field through curated special issues
- Prominent recognition across all journal communications
- Opportunity to build a distinguished editorial team

{journal_name}, published by {publisher_name}, is committed to excellence in academic publishing. Under your leadership, we envision the journal achieving Scopus indexing and new heights of scholarly impact and recognition.

Please visit {journal_link} to review our current publications, scope, and editorial framework.

I would be delighted to arrange a call at your convenience to discuss this opportunity in detail, answer any questions, and explore how we can support your vision for the journal.

To express your interest, please reply to this email with your updated CV and any initial thoughts or questions.

We are honored to extend this invitation and sincerely hope you will consider joining us in this exciting endeavor.

With deepest respect and warm regards,

{editor_in_chief_name}
{publisher_name}
Email: {sender_email}
Website: {journal_link}"""
    },

    # ── Editor-in-Chief (Scopus) ──
    "editor_in_chief_scopus": {
        "name": "Editor-in-Chief",
        "subject": "Distinguished Invitation: Editor-in-Chief Position - {journal_name} (Scopus-Indexed)",
        "body": """Dear Professor {author_name},

I hope this message finds you well.

It is with great pleasure that I write to you on behalf of {publisher_name} to extend a distinguished invitation to serve as Editor-in-Chief of {journal_name} (ISSN: {journal_issn}).

{journal_name} is a Scopus-indexed journal with an established reputation for publishing rigorous, high-impact research. Your outstanding scholarly achievements, extensive publication record, and recognized leadership in the academic community make you our ideal candidate for this prestigious position.

As Editor-in-Chief, you would:

- Provide visionary leadership and strategic direction for the journal
- Chair the Editorial Board and guide editorial policies
- Make final decisions on manuscript acceptance
- Represent the journal at conferences and academic forums
- Foster relationships with the global research community
- Ensure the highest standards of publication ethics and quality
- Guide the journal's growth and international recognition

We offer:
- Complete editorial autonomy within established ethical guidelines
- Full administrative and technical support from our publishing team
- Competitive honorarium commensurate with the position
- Platform to advance your field through curated special issues
- Prominent recognition across all journal communications
- Opportunity to build a distinguished editorial team

{journal_name}, published by {publisher_name}, is committed to excellence in academic publishing. Under your leadership, we envision the journal achieving new heights of scholarly impact and recognition.

Please visit {journal_link} to review our current publications, scope, and editorial framework.

I would be delighted to arrange a call at your convenience to discuss this opportunity in detail, answer any questions, and explore how we can support your vision for the journal.

To express your interest, please reply to this email with your updated CV and any initial thoughts or questions.

We are honored to extend this invitation and sincerely hope you will consider joining us in this exciting endeavor.

With deepest respect and warm regards,

{editor_in_chief_name}
{publisher_name}
Email: {sender_email}
Website: {journal_link}"""
    },
}


def get_template_names() -> Dict[str, str]:
    """Get dictionary of base template IDs to display names (excludes Scopus variants)."""
    return {key: TEMPLATES[key]["name"] for key in _BASE_TEMPLATES}


def get_template(template_id: str) -> Dict:
    """Get a template by ID."""
    return TEMPLATES.get(template_id, TEMPLATES[TEMPLATE_BOARD_MEMBER])


def format_template(
    template_id: str,
    author_name: str,
    journal_name: str,
    journal_issn: str,
    journal_link: str,
    editor_in_chief_name: str,
    publisher_name: str,
    sender_email: str,
    publisher_location: str = "",
    scopus_indexed: bool = False
) -> Dict[str, str]:
    """
    Format a template with the provided values.

    When scopus_indexed is True, the Scopus variant of the template is used.

    Returns:
        Dict with 'subject' and 'body' keys
    """
    effective_id = f"{template_id}_scopus" if scopus_indexed else template_id
    template = get_template(effective_id)

    replacements = {
        "{author_name}": author_name,
        "{journal_name}": journal_name,
        "{journal_issn}": journal_issn,
        "{journal_link}": journal_link,
        "{editor_in_chief_name}": editor_in_chief_name,
        "{publisher_name}": publisher_name,
        "{sender_email}": sender_email,
        "{publisher_location}": publisher_location,
    }

    subject = template["subject"]
    body = template["body"]

    for placeholder, value in replacements.items():
        subject = subject.replace(placeholder, value or "")
        body = body.replace(placeholder, value or "")

    return {
        "subject": subject,
        "body": body
    }


def get_all_placeholders() -> list:
    """Get list of all placeholders used in templates."""
    return [
        "{author_name}",
        "{journal_name}",
        "{journal_issn}",
        "{journal_link}",
        "{editor_in_chief_name}",
        "{publisher_name}",
        "{sender_email}",
        "{publisher_location}",
    ]
