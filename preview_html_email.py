#!/usr/bin/env python3
"""Generate local premium HTML email previews without sending any email."""

from pathlib import Path

from email_sender import EmailSender
from templates import TEMPLATE_PUBLICATION_RECENT_WORK, format_template


SAMPLE_AUTHOR_NAME = "Jane Example"
SAMPLE_SPECIALTY = "Medical Informatics"
SAMPLE_JOURNAL = {
    "name": "International Journal of Applied Health Analytics",
    "issn": "1234-5678",
    "link": "https://examplejournal.org",
    "submission_link": "https://examplejournal.org/submit",
    "editor_in_chief": "Prof. Alex Editor",
    "location": "Dubai, UAE",
}


def _build_sample_text(sender: EmailSender, publisher_id: str) -> dict[str, str]:
    publisher_name = sender.get_publisher_name(publisher_id) or "Publisher"
    sender_email = sender.get_publisher_email(publisher_id) or "editorial.office@example.com"

    return format_template(
        template_id=TEMPLATE_PUBLICATION_RECENT_WORK,
        author_name=SAMPLE_AUTHOR_NAME,
        journal_name=SAMPLE_JOURNAL["name"],
        journal_issn=SAMPLE_JOURNAL["issn"],
        journal_link=SAMPLE_JOURNAL["link"],
        editor_in_chief_name=SAMPLE_JOURNAL["editor_in_chief"],
        publisher_name=publisher_name,
        sender_email=sender_email,
        publisher_location=SAMPLE_JOURNAL["location"],
        scopus_indexed=False,
        journal_submission_link=SAMPLE_JOURNAL["submission_link"],
        journal_cite_score="4.2",
        journal_quartile="Q2",
        journal_indexing_status="Indexed in major scholarly databases",
        author_specialty=SAMPLE_SPECIALTY,
        author_recent_publications="\n\nWe particularly noted recent publications such as:\n- Adaptive Clinical AI for Triage (2025, Health Data)\n- Early Risk Detection Pipelines (2024, Digital Medicine)",
        journal_scope="Interdisciplinary research at the intersection of healthcare, analytics, and AI.",
        invitation_goal="inviting high-quality original research submissions for upcoming issues",
    )


def generate_preview(publisher_id: str, output_dir: Path) -> Path:
    sender = EmailSender()
    formatted = _build_sample_text(sender, publisher_id)
    html_content = sender.render_html_preview(
        publisher_id=publisher_id,
        subject=formatted["subject"],
        body=formatted["body"],
        journal_name=SAMPLE_JOURNAL["name"],
        journal_link=SAMPLE_JOURNAL["link"],
        submission_link=SAMPLE_JOURNAL["submission_link"],
        invitation_type="publication",
        scopus_indexed=True,
        journal_cite_score="4.2",
        journal_quartile="Q2",
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"premium_email_preview_{publisher_id}.html"
    output_path.write_text(html_content, encoding="utf-8")
    return output_path


def main():
    workspace = Path(__file__).parent
    output_dir = workspace / "data"

    preview_publishers = ["brevo", "mesopotamian"]
    generated_files = []

    for publisher_id in preview_publishers:
        try:
            generated_files.append(generate_preview(publisher_id, output_dir))
        except Exception as exc:
            print(f"Failed to generate preview for '{publisher_id}': {exc}")

    if not generated_files:
        raise SystemExit("No preview files were generated.")

    print("Generated HTML previews:")
    for file_path in generated_files:
        print(f"- {file_path}")


if __name__ == "__main__":
    main()
