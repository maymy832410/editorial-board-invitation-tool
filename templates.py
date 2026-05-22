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

    # -- Publication invitation: recent-work admiration --
    TEMPLATE_PUBLICATION_RECENT_WORK: {
        "name": "Publish Invitation - Recent Work",
        "subject": "{author_name}, invitation to submit your work to {journal_name}",
        "body": """Dear Professor {author_name},

I hope this message finds you well.

I am writing on behalf of {journal_name} (ISSN: {journal_issn}) to invite you to consider submitting a manuscript to the journal.

We noticed your recent scholarly work and believe your research profile is highly relevant to our readership.{author_recent_publications}

{journal_name} welcomes rigorous contributions in {author_specialty}. {journal_metrics}{invitation_goal_note}{journal_scope_note}

If you have a manuscript in preparation, we would be pleased to receive it through our submission portal:
{journal_submission_link}

You may also review the journal scope and published articles here:
{journal_link}

We would be honored to consider a contribution from you and would be happy to answer any questions about fit, scope, or the submission process.

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
        "subject": "{author_name}, your research aligns with {journal_name}",
        "body": """Dear Professor {author_name},

Greetings from the Editorial Office of {journal_name}.

Your research background in {author_specialty} appears to align well with the journal's current publishing interests. We would therefore like to invite you to submit a manuscript for consideration in {journal_name} (ISSN: {journal_issn}).

{author_recent_publications}

The journal is seeking high-quality original articles, reviews, and scholarly contributions that can support meaningful discussion in the field. {journal_metrics}{invitation_goal_note}{journal_scope_note}

For submission, please use the journal portal:
{journal_submission_link}

Journal website:
{journal_link}

If you are considering a suitable manuscript, we would be glad to hear from you or receive your submission through the portal.

Sincerely,

Editorial Office
{journal_name}
{publisher_name}
Email: {sender_email}"""
    },

    # -- Publication invitation: journal value and metrics --
    TEMPLATE_PUBLICATION_METRICS: {
        "name": "Publish Invitation - Journal Metrics",
        "subject": "{author_name}, publication invitation from {journal_name}",
        "body": """Dear Professor {author_name},

I hope you are doing well.

On behalf of {journal_name}, I am pleased to invite you to submit a manuscript for publication consideration.

{journal_name} is committed to rigorous peer review, ethical publication practices, and international scholarly visibility. {journal_metrics}{invitation_goal_note}{journal_scope_note}

Your work in {author_specialty} would be a valuable fit for the journal's audience.{author_recent_publications}

Submission link:
{journal_submission_link}

Journal website:
{journal_link}

We would be pleased to consider an original article, review article, or other suitable scholarly contribution from your research group.

With best regards,

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

    replacements = {
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
