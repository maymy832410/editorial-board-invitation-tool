"""Professional email invitation templates for editorial and publication invitations."""

from typing import Dict, Iterable


INVITATION_TYPE_EDITORIAL = "editorial"
INVITATION_TYPE_PUBLICATION = "publication"
TEMPLATE_BOARD_MEMBER = "board_member"
TEMPLATE_MANAGING_EDITOR = "managing_editor"
TEMPLATE_EDITOR_IN_CHIEF = "editor_in_chief"
TEMPLATE_PUBLICATION_RECENT_WORK = "publication_recent_work"
TEMPLATE_PUBLICATION_TOPIC_FIT = "publication_topic_fit"
TEMPLATE_PUBLICATION_METRICS = "publication_metrics"

# Base template IDs that appear in the UI dropdown
_EDITORIAL_BASE_TEMPLATES = [TEMPLATE_BOARD_MEMBER, TEMPLATE_MANAGING_EDITOR, TEMPLATE_EDITOR_IN_CHIEF]
_PUBLICATION_TEMPLATES = [
    TEMPLATE_PUBLICATION_RECENT_WORK,
    TEMPLATE_PUBLICATION_TOPIC_FIT,
    TEMPLATE_PUBLICATION_METRICS,
]
_BASE_TEMPLATES = _EDITORIAL_BASE_TEMPLATES

_COMMON_PLACEHOLDERS = [
    "{author_name}",
    "{journal_name}",
    "{journal_issn}",
    "{journal_link}",
    "{editor_in_chief_name}",
    "{publisher_name}",
    "{sender_email}",
    "{publisher_location}",
]
_EDITORIAL_PLACEHOLDERS = list(_COMMON_PLACEHOLDERS)
_PUBLICATION_PLACEHOLDERS = list(_COMMON_PLACEHOLDERS) + [
    "{journal_submission_link}",
    "{journal_cite_score}",
    "{journal_quartile}",
    "{journal_indexing_status}",
    "{author_specialty}",
    "{author_recent_publications}",
    "{journal_scope}",
    "{invitation_goal}",
    "{journal_metrics}",
    "{invitation_goal_note}",
    "{journal_scope_note}",
]

TEMPLATES = {
    # ── Editorial Board Member (non-Scopus) ──
    TEMPLATE_BOARD_MEMBER: {
        "name": "Editorial Board Member",
        "subject": "Invitation to Join the Editorial Board of {journal_name}",
        "body": """Dear Professor {author_name},

On behalf of the editorial leadership of {journal_name}, published by {publisher_name}, we are pleased to invite you to join the journal's Editorial Board.

We are expanding the board with experienced scholars who can support rigorous peer review, strong publication ethics, and long-term journal development aligned with COPE and international publishing standards.

Your academic profile and subject-matter expertise would be highly valuable to this effort, and we would be honored to welcome your participation.

If you are open to this appointment, please reply to this message and we will share the formal onboarding details.

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
        "subject": "Invitation to Join the Editorial Board of {journal_name} (Scopus-Indexed)",
        "body": """Dear Professor {author_name},

On behalf of the editorial leadership of {journal_name}, published by {publisher_name}, we are pleased to invite you to join the journal's Editorial Board.

{journal_name} is a Scopus-indexed journal committed to high editorial quality, ethical publishing practice, and international scholarly visibility.

Your academic profile and subject-matter expertise would be highly valuable to the continued growth of the journal, and we would be honored to welcome your participation.

If you are open to this appointment, please reply to this message and we will share the formal onboarding details.

Warm regards,

Editorial Office
{journal_name}
{publisher_location}
{journal_link}"""
    },

    # ── Managing Editor (non-Scopus) ──
    TEMPLATE_MANAGING_EDITOR: {
        "name": "Managing Editor",
        "subject": "Invitation to Serve as Managing Editor of {journal_name}",
        "body": """Dear Professor {author_name},

On behalf of the editorial leadership of {journal_name} (ISSN: {journal_issn}), published by {publisher_name}, we are pleased to invite you to serve as Managing Editor.

This role includes coordinating manuscript workflow, supporting timely peer review, and helping implement editorial policy in line with COPE and international publishing standards.

The journal is actively strengthening its editorial operations and international profile. Your leadership, editorial judgment, and academic reputation would be highly valuable to this mission.

If you are open to this appointment, please reply to this message and we will share the formal scope, timeline, and onboarding details.

We would be honored to work with you in this capacity.

Warm regards,

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
        "subject": "Invitation to Serve as Managing Editor of {journal_name} (Scopus-Indexed)",
        "body": """Dear Professor {author_name},

On behalf of the editorial leadership of {journal_name} (ISSN: {journal_issn}), published by {publisher_name}, we are pleased to invite you to serve as Managing Editor.

{journal_name} is a Scopus-indexed journal committed to rigorous peer review, ethical publishing practice, and international visibility.

This role includes coordinating manuscript workflow, supporting timely peer review, and helping implement editorial policy in line with COPE and international publishing standards.

Your leadership, editorial judgment, and academic reputation would be highly valuable to the continued development of the journal.

If you are open to this appointment, please reply to this message and we will share the formal scope, timeline, and onboarding details.

We would be honored to work with you in this capacity.

Warm regards,

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
        "subject": "Invitation to Serve as Editor-in-Chief of {journal_name}",
        "body": """Dear Professor {author_name},

On behalf of {publisher_name}, we are honored to invite you to serve as Editor-in-Chief of {journal_name} (ISSN: {journal_issn}).

This appointment carries strategic responsibility for editorial direction, quality oversight, and long-term journal development in line with COPE and international publishing standards.

Your scholarly leadership and academic reputation make you an exceptional candidate for this role.

If you are open to this appointment, please reply to this message and we will share the formal terms, editorial mandate, and onboarding process.

We would be honored to work under your guidance.

With highest respect and warm regards,

{editor_in_chief_name}
{publisher_name}
Email: {sender_email}
Website: {journal_link}"""
    },

    # ── Editor-in-Chief (Scopus) ──
    "editor_in_chief_scopus": {
        "name": "Editor-in-Chief",
        "subject": "Invitation to Serve as Editor-in-Chief of {journal_name} (Scopus-Indexed)",
        "body": """Dear Professor {author_name},

On behalf of {publisher_name}, we are honored to invite you to serve as Editor-in-Chief of {journal_name} (ISSN: {journal_issn}).

{journal_name} is a Scopus-indexed journal committed to rigorous peer review, publication ethics, and international scholarly visibility.

This appointment carries strategic responsibility for editorial direction, quality oversight, and sustained global growth.

Your scholarly leadership and academic reputation make you an exceptional candidate for this role.

If you are open to this appointment, please reply to this message and we will share the formal terms, editorial mandate, and onboarding process.

We would be honored to work under your guidance.

With highest respect and warm regards,

{editor_in_chief_name}
{publisher_name}
Email: {sender_email}
Website: {journal_link}"""
    },

    # -- Publication invitation: recent-work admiration --
    TEMPLATE_PUBLICATION_RECENT_WORK: {
        "name": "Publish Invitation - Recent Work",
        "subject": "Invitation to Submit Your Research to {journal_name}",
        "body": """Dear Professor {author_name},

On behalf of {journal_name} (ISSN: {journal_issn}), we are pleased to invite you to submit a manuscript for consideration.

Your research profile in {author_specialty} is highly relevant to the journal's readership.{author_recent_publications}

{journal_name} welcomes original research, review papers, and other high-quality scholarly contributions. {journal_metrics}{invitation_goal_note}{journal_scope_note}

Submission portal:
{journal_submission_link}

Journal website:
{journal_link}

If you would like to confirm topic fit before submission, please feel free to reply to this email.

Warm regards,

Editorial Office
{journal_name}
{publisher_name}
Email: {sender_email}
Website: {journal_link}"""
    },

    # -- Publication invitation: topic fit --
    TEMPLATE_PUBLICATION_TOPIC_FIT: {
        "name": "Publish Invitation - Topic Fit",
        "subject": "Your Research Topic Fits {journal_name}",
        "body": """Dear Professor {author_name},

Greetings from the Editorial Office of {journal_name}.

Your research background in {author_specialty} appears to align closely with the journal's current publishing priorities. We would therefore like to invite you to submit a manuscript for consideration in {journal_name} (ISSN: {journal_issn}).

{author_recent_publications}

The journal is currently seeking high-quality original articles, review papers, and other scholarly contributions that can support meaningful discussion in the field. {journal_metrics}{invitation_goal_note}{journal_scope_note}

Submission portal:
{journal_submission_link}

Journal website:
{journal_link}

If you are considering a suitable manuscript, we would be delighted to receive your submission through the portal.

Warm regards,

Editorial Office
{journal_name}
{publisher_name}
Email: {sender_email}
Website: {journal_link}"""
    },

    # -- Publication invitation: journal value and metrics --
    TEMPLATE_PUBLICATION_METRICS: {
        "name": "Publish Invitation - Journal Metrics",
        "subject": "Publication Invitation from {journal_name}",
        "body": """Dear Professor {author_name},

On behalf of {journal_name} (ISSN: {journal_issn}), we are pleased to invite you to submit a manuscript for publication consideration.

{journal_name} is committed to rigorous peer review, ethical publication practices, and international scholarly visibility. {journal_metrics}{invitation_goal_note}{journal_scope_note}

Your work in {author_specialty} would be a strong fit for the journal's audience.{author_recent_publications}

Submission portal:
{journal_submission_link}

Journal website:
{journal_link}

We would be pleased to consider an original article, review article, or other suitable scholarly contribution from your research group.

Warm regards,

Editorial Office
{journal_name}
{publisher_name}
Email: {sender_email}
Website: {journal_link}"""
    },
}


def get_template_names(invitation_type: str = INVITATION_TYPE_EDITORIAL) -> Dict[str, str]:
    """Get dictionary of template IDs to display names for the selected invitation type."""
    template_ids = _PUBLICATION_TEMPLATES if invitation_type == INVITATION_TYPE_PUBLICATION else _EDITORIAL_BASE_TEMPLATES
    return {key: TEMPLATES[key]["name"] for key in template_ids}


def get_publication_template_ids() -> list[str]:
    """Return publication template IDs in their rotation order."""
    return list(_PUBLICATION_TEMPLATES)


def _template_invitation_type(template_id: str) -> str:
    """Infer invitation type from template id (handles Scopus template variants)."""
    base_id = template_id.removesuffix("_scopus")
    if base_id in _PUBLICATION_TEMPLATES:
        return INVITATION_TYPE_PUBLICATION
    return INVITATION_TYPE_EDITORIAL


def get_placeholders_for_invitation_type(invitation_type: str) -> list[str]:
    """Return placeholders relevant to a specific invitation workflow type."""
    if invitation_type == INVITATION_TYPE_PUBLICATION:
        return list(_PUBLICATION_PLACEHOLDERS)
    return list(_EDITORIAL_PLACEHOLDERS)


def get_template(template_id: str) -> Dict:
    """Get a template by ID."""
    return TEMPLATES.get(template_id, TEMPLATES[TEMPLATE_BOARD_MEMBER])


def build_journal_metrics(
    journal_cite_score: str = "",
    journal_quartile: str = "",
    journal_indexing_status: str = "",
) -> str:
    """Build a compact sentence with optional journal metrics."""
    details = []
    if journal_cite_score:
        details.append(f"CiteScore: {journal_cite_score}")
    if journal_quartile:
        details.append(f"Quartile: {journal_quartile}")
    if journal_indexing_status:
        details.append(f"Indexing status: {journal_indexing_status}")
    if not details:
        return ""
    return "Journal details: " + "; ".join(details) + "."


def format_recent_publications(publications: Iterable[dict]) -> str:
    """Format recent OpenAlex publications for inclusion in an email template."""
    lines = []
    for publication in publications or []:
        title = (publication.get("title") or "").strip()
        if not title:
            continue
        year = (publication.get("year") or "").strip()
        source = (publication.get("source") or "").strip()
        suffix_parts = [part for part in [year, source] if part]
        suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
        lines.append(f"- {title}{suffix}")
    if not lines:
        return ""
    return "\n\nWe particularly noted recent publications such as:\n" + "\n".join(lines)


def choose_rotating_template(template_ids: list[str], position: int) -> str:
    """Choose a template ID by deterministic rotation."""
    if not template_ids:
        return TEMPLATE_PUBLICATION_RECENT_WORK
    return template_ids[position % len(template_ids)]


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
    scopus_indexed: bool = False,
    journal_submission_link: str = "",
    journal_cite_score: str = "",
    journal_quartile: str = "",
    journal_indexing_status: str = "",
    author_specialty: str = "",
    author_recent_publications: str = "",
    journal_scope: str = "",
    invitation_goal: str = ""
) -> Dict[str, str]:
    """
    Format a template with the provided values.

    When scopus_indexed is True, the Scopus variant of the template is used.

    Returns:
        Dict with 'subject' and 'body' keys
    """
    effective_id = f"{template_id}_scopus" if scopus_indexed else template_id
    template = get_template(effective_id)

    journal_metrics = build_journal_metrics(
        journal_cite_score=journal_cite_score,
        journal_quartile=journal_quartile,
        journal_indexing_status=journal_indexing_status,
    )
    invitation_goal_note = f" Current invitation focus: {invitation_goal}." if invitation_goal else ""
    journal_scope_note = f"\n\nScope note: {journal_scope}" if journal_scope else ""

    all_replacements = {
        "{author_name}": author_name,
        "{journal_name}": journal_name,
        "{journal_issn}": journal_issn,
        "{journal_link}": journal_link,
        "{editor_in_chief_name}": editor_in_chief_name,
        "{publisher_name}": publisher_name,
        "{sender_email}": sender_email,
        "{publisher_location}": publisher_location,
        "{journal_submission_link}": journal_submission_link,
        "{journal_cite_score}": journal_cite_score,
        "{journal_quartile}": journal_quartile,
        "{journal_indexing_status}": journal_indexing_status,
        "{author_specialty}": author_specialty or "your field",
        "{author_recent_publications}": author_recent_publications,
        "{journal_scope}": journal_scope,
        "{invitation_goal}": invitation_goal,
        "{journal_metrics}": journal_metrics,
        "{invitation_goal_note}": invitation_goal_note,
        "{journal_scope_note}": journal_scope_note,
    }

    invitation_type = _template_invitation_type(template_id)
    allowed_placeholders = set(get_placeholders_for_invitation_type(invitation_type))

    subject = template["subject"]
    body = template["body"]

    for placeholder in get_all_placeholders():
        value = all_replacements.get(placeholder, "") if placeholder in allowed_placeholders else ""
        subject = subject.replace(placeholder, value or "")
        body = body.replace(placeholder, value or "")

    return {
        "subject": subject,
        "body": body
    }


def get_all_placeholders() -> list:
    """Get list of all placeholders used in templates."""
    # Preserve order while deduplicating shared placeholders.
    return list(dict.fromkeys(_EDITORIAL_PLACEHOLDERS + _PUBLICATION_PLACEHOLDERS))
