"""Background processor for durable bulk invitation email jobs."""

import json
import time
from typing import Any, Dict, List

from config import BULK_EMAIL_MAX_ATTEMPTS, BULK_EMAIL_SEND_DELAY_SEC
from db_client import (
    BULK_RECIPIENT_STATUS_FAILED,
    BULK_RECIPIENT_STATUS_PENDING,
    BULK_RECIPIENT_STATUS_SENT,
    BULK_RECIPIENT_STATUS_SKIPPED,
    INVITATION_TYPE_PUBLICATION,
    get_storage,
)
from email_sender import EmailSender
from pdf_generator import PUBLISHER_INFO, generate_invitation_pdf
from templates import (
    TEMPLATE_BOARD_MEMBER,
    format_recent_publications,
    format_template,
)


def _parse_json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


class BulkEmailWorker:
    """Processes one queued bulk recipient at a time."""

    def __init__(self) -> None:
        self.storage = get_storage()
        self.email_sender = EmailSender()
        self._idle_ticks = 0

    def process_next(self) -> bool:
        """Process one queued recipient. Returns True when work was attempted."""
        claimed = self.storage.claim_next_bulk_email_recipient()
        if not claimed:
            self._idle_ticks += 1
            if self._idle_ticks == 1 or self._idle_ticks % 12 == 0:
                summary = self.storage.get_bulk_email_queue_summary()
                print(
                    "[bulk-email] idle "
                    f"active_jobs={summary.get('active_jobs')} "
                    f"pending={summary.get('pending_recipients')} "
                    f"sending={summary.get('sending_recipients')}",
                    flush=True,
                )
            return False
        self._idle_ticks = 0

        job = claimed.get("job") or {}
        recipient = claimed.get("recipient") or {}
        recipient_id = int(recipient.get("id") or 0)
        if not recipient_id:
            return False

        try:
            print(
                f"[bulk-email] sending recipient {recipient_id} "
                f"job={recipient.get('job_id')} to={recipient.get('email')}",
                flush=True,
            )
            provider_response = self._send_recipient(job, recipient)
            self.storage.mark_bulk_email_recipient(
                recipient_id,
                BULK_RECIPIENT_STATUS_SENT,
                provider_response=provider_response,
            )
            print(
                f"[bulk-email] sent recipient {recipient_id} "
                f"job={recipient.get('job_id')} provider_response={provider_response}",
                flush=True,
            )
            time.sleep(max(0.0, float(BULK_EMAIL_SEND_DELAY_SEC or 0)))
            return True
        except DuplicateInvitationError as exc:
            print(
                f"[bulk-email] skipped recipient {recipient_id}: {exc}",
                flush=True,
            )
            self.storage.mark_bulk_email_recipient(
                recipient_id,
                BULK_RECIPIENT_STATUS_SKIPPED,
                str(exc),
            )
            return True
        except Exception as exc:
            attempts = int(recipient.get("attempts") or 1)
            message = str(exc)
            print(
                f"[bulk-email] failed recipient {recipient_id} "
                f"attempt={attempts}: {message}",
                flush=True,
            )
            if attempts < max(1, int(BULK_EMAIL_MAX_ATTEMPTS or 1)):
                self.storage.retry_bulk_email_recipient(recipient_id, message)
            else:
                self.storage.mark_bulk_email_recipient(
                    recipient_id,
                    BULK_RECIPIENT_STATUS_FAILED,
                    message,
                )
            time.sleep(max(0.0, float(BULK_EMAIL_SEND_DELAY_SEC or 0)))
            return True

    def _send_recipient(self, job: Dict[str, Any], recipient: Dict[str, Any]) -> str:
        invitation_type = job.get("invitation_type") or "editorial"
        journal_name = job.get("journal_name") or ""
        orcid_id = recipient.get("orcid_id") or ""
        author_name = recipient.get("author_name") or "Author"
        to_email = recipient.get("email") or ""

        if not to_email or "@" not in to_email:
            raise ValueError("Recipient has no valid email")
        if orcid_id and self.storage.is_sent(
            orcid_id,
            invitation_type,
            journal_name if invitation_type == INVITATION_TYPE_PUBLICATION else None,
        ):
            raise DuplicateInvitationError("Already invited; skipped before sending")

        publisher_id = (job.get("publisher_id") or "").strip()
        if not publisher_id:
            raise ValueError("Bulk job has no selected publisher_id")
        if publisher_id not in self.email_sender.credentials:
            raise ValueError(f"Unknown selected publisher_id: {publisher_id}")
        journal_config = _parse_json(job.get("journal_config_json"), {})
        pub_info = PUBLISHER_INFO.get(publisher_id, {})
        publisher_name = pub_info.get("name") or self.email_sender.get_publisher_name(publisher_id)
        publisher_location = pub_info.get("location") or journal_config.get("location", "")
        sender_email = self.email_sender.get_publisher_email(publisher_id)
        template_id = self._resolve_template_id(job, recipient)

        recent_publications_text = ""
        if invitation_type == INVITATION_TYPE_PUBLICATION and job.get("include_publications"):
            recent_publications: List[Dict[str, Any]] = _parse_json(
                recipient.get("recent_publications_json"),
                [],
            )
            recent_publications_text = format_recent_publications(recent_publications)

        formatted = format_template(
            template_id=template_id,
            author_name=author_name,
            journal_name=journal_config.get("name", ""),
            journal_issn=journal_config.get("issn", ""),
            journal_link=journal_config.get("link", ""),
            editor_in_chief_name=journal_config.get("editor_in_chief", ""),
            publisher_name=publisher_name,
            sender_email=sender_email,
            publisher_location=publisher_location,
            scopus_indexed=bool(job.get("scopus_indexed")),
            journal_submission_link=journal_config.get("submission_link", ""),
            journal_cite_score=journal_config.get("cite_score", ""),
            journal_quartile=journal_config.get("quartile", ""),
            journal_indexing_status=journal_config.get("indexing_status", ""),
            author_specialty=recipient.get("specialty") or recipient.get("research_areas") or "",
            author_recent_publications=recent_publications_text,
            journal_scope=journal_config.get("scope", ""),
            invitation_goal=journal_config.get("invitation_goal", ""),
        )

        pdf_bytes = None
        if job.get("attach_pdf"):
            try:
                pdf_bytes = generate_invitation_pdf(
                    publisher_id=publisher_id,
                    recipient_name=author_name,
                    email_body=formatted["body"],
                    subject=formatted["subject"],
                    journal_name=journal_config.get("name", ""),
                    journal_link=journal_config.get("link", ""),
                )
            except Exception:
                pdf_bytes = None

        success, message = self.email_sender.send_email(
            publisher_id=publisher_id,
            to_email=to_email,
            subject=formatted["subject"],
            body=formatted["body"],
            to_name=author_name,
            pdf_attachment=pdf_bytes,
            attachment_filename=(
                "Publication_Invitation_Letter.pdf"
                if invitation_type == INVITATION_TYPE_PUBLICATION
                else "Invitation_Letter.pdf"
            ),
            journal_name=journal_config.get("name", ""),
            journal_link=journal_config.get("link", ""),
            submission_link=journal_config.get("submission_link", ""),
            invitation_type=invitation_type,
            scopus_indexed=bool(job.get("scopus_indexed")),
            journal_cite_score=journal_config.get("cite_score", ""),
            journal_quartile=journal_config.get("quartile", ""),
        )
        if not success:
            raise RuntimeError(message)

        if orcid_id:
            saved = self.storage.mark_sent(
                orcid_id,
                author_name=author_name,
                email=to_email,
                publisher=publisher_id,
                invitation_type=invitation_type,
                journal_name=journal_name,
                template_id=template_id,
                cite_score=journal_config.get("cite_score", ""),
                quartile=journal_config.get("quartile", ""),
            )
            if not saved:
                print("Bulk email sent, but sent status could not be saved", flush=True)
        return message

    def _resolve_template_id(self, job: Dict[str, Any], recipient: Dict[str, Any]) -> str:
        return job.get("template_id") or TEMPLATE_BOARD_MEMBER


class DuplicateInvitationError(RuntimeError):
    """Raised when a recipient has already been invited before send time."""
