"""Background email-collection worker.

Runs as a separate process (see Procfile `worker:`). Continuously seeds authors
from OpenAlex by discipline/specialty/keyword filters into the `harvested_authors`
queue, then fetches ORCID emails at an adaptive, rate-limit-safe pace. Every result
(full author metadata + email) is persisted to PostgreSQL so it can be reused later.

State machine (per the agreed 429-safe policy):
    ACTIVE      -> normal baseline concurrency/delay.
    COOLDOWN    -> on any ORCID 429: concurrency=1, delay 8-12s, hold 15-30 min.
    RECOVERY    -> ramp concurrency back toward baseline over clean batches.
    STOPPED_TODAY -> a 429 during RECOVERY: stop until next UTC midnight.
    PAUSED/IDLE -> user-controlled via the dashboard; worker waits.
"""

import asyncio
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from config import (
    COLLECT_BASELINE_CONCURRENCY,
    COLLECT_BASELINE_DELAY_SEC,
    COLLECT_COOLDOWN_DELAY_MAX,
    COLLECT_COOLDOWN_DELAY_MIN,
    COLLECT_COOLDOWN_MIN,
    COLLECT_CYCLE_PAUSE_SEC,
    COLLECT_ERROR_RETRY_HOURS,
    COLLECT_FETCH_BATCH,
    COLLECT_IDLE_POLL_SEC,
    COLLECT_NO_EMAIL_RETRY_DAYS,
    COLLECT_RECOVERY_STEP,
    COLLECT_SEED_BATCH_SIZE,
    COLLECT_SEED_MIN_QUEUE,
)
from db_client import (
    EMAIL_STATUS_ERROR,
    EMAIL_STATUS_FOUND,
    EMAIL_STATUS_NO_EMAIL,
    EMAIL_STATUS_PENDING,
    RUN_STATUS_ACTIVE,
    RUN_STATUS_COOLDOWN,
    RUN_STATUS_IDLE,
    RUN_STATUS_PAUSED,
    RUN_STATUS_RECOVERY,
    RUN_STATUS_STOPPED_TODAY,
    get_storage,
)
from openalex_client import (
    OpenAlexClient,
    OpenAlexRateLimitError,
    OpenAlexRequestError,
)
from orcid_async import fetch_emails_async
from bulk_email_worker import BulkEmailWorker
from openai_email_async import AsyncOpenAIEmailClient


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _next_utc_midnight(now: Optional[datetime] = None) -> datetime:
    now = now or _utcnow()
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_json_list(value: Any) -> List[str]:
    import json

    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value)
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


def _log(message: str) -> None:
    print(f"[collector] {datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


class CollectorWorker:
    """Long-running collection worker driven by the `collection_runs` row."""

    def __init__(self) -> None:
        self.storage = get_storage()
        self.openalex = OpenAlexClient()
        self._topic_cache: Dict[int, List[str]] = {}
        self.bulk_email_worker: Optional[BulkEmailWorker] = None
        self.worker_owner = str(uuid.uuid4())

    # -- seeding ---------------------------------------------------------
    def _resolve_topic_ids(self, run: Dict[str, Any]) -> List[str]:
        """Resolve OpenAlex topic IDs from stored topic_ids or keyword tags."""
        search_run_id = int(run.get("search_run_id") or 0)
        if search_run_id in self._topic_cache:
            return self._topic_cache[search_run_id]
        cached = _parse_json_list(run.get("topic_ids_json"))
        keyword_tags = (run.get("keyword_tags") or "").strip()
        if not keyword_tags:
            self._topic_cache[search_run_id] = cached
            return cached

        keywords = [k.strip() for k in keyword_tags.replace("\n", ",").split(",") if k.strip()]
        if not keywords:
            return cached
        try:
            topic_ids, _details = self.openalex.search_topics(keywords)
        except Exception as exc:
            _log(f"topic resolution failed: {exc}")
            return cached
        combined = sorted(set(cached) | set(topic_ids))
        if combined:
            self.storage.update_current_search_topics(combined, _details)
        self._topic_cache[search_run_id] = combined
        return combined

    def _seed_filter_authors(
        self, authors: List[Dict[str, Any]], disciplines: List[str], specialties: List[str]
    ) -> List[Dict[str, Any]]:
        """Keep only authors matching configured disciplines/specialties (if any)."""
        if not disciplines and not specialties:
            return authors

        discipline_set = {d.strip().lower() for d in disciplines if d.strip()}
        specialty_terms = [s.strip().lower() for s in specialties if s.strip()]

        filtered = []
        for author in authors:
            if discipline_set:
                discipline = (author.get("discipline") or "").strip().lower()
                if discipline not in discipline_set:
                    continue
            if specialty_terms:
                haystack = " ".join(
                    str(author.get(field) or "").lower()
                    for field in ("specialty", "subfield", "research_areas")
                )
                if not any(term in haystack for term in specialty_terms):
                    continue
            filtered.append(author)
        return filtered

    def _seed_if_needed(self, run: Dict[str, Any]) -> None:
        """Fetch one OpenAlex batch into the harvest queue when it runs low."""
        if run.get("seed_exhausted"):
            return

        search_run_id = int(run.get("search_run_id") or 0)
        if not search_run_id:
            return
        counts = self.storage.count_search_harvest_by_status(search_run_id)
        pending = counts.get(EMAIL_STATUS_PENDING, 0)
        if pending >= COLLECT_SEED_MIN_QUEUE:
            return

        topic_ids = self._resolve_topic_ids(run) or None
        disciplines = _parse_json_list(run.get("disciplines_json"))
        specialties = _parse_json_list(run.get("specialties_json"))
        exclude_countries = _parse_json_list(run.get("exclude_countries_json")) or None

        try:
            batch = self.openalex.fetch_author_batch(
                h_index_min=run.get("h_index_min"),
                h_index_max=run.get("h_index_max"),
                exclude_country_codes=exclude_countries,
                topic_ids=topic_ids,
                require_orcid=True,
                cursor=run.get("seed_cursor") or "*",
                batch_size=COLLECT_SEED_BATCH_SIZE,
            )
        except OpenAlexRateLimitError as exc:
            self.storage.bump_daily_stat("openalex_429", 1)
            self.storage.bump_search_stat(search_run_id, "openalex_429", 1)
            wait = exc.retry_after_seconds or 300
            _log(f"OpenAlex 429 during seed; backing off {wait}s")
            time.sleep(min(wait, 900))
            return
        except OpenAlexRequestError as exc:
            _log(f"OpenAlex seed request failed: {exc}")
            time.sleep(30)
            return

        results = batch.get("results", [])
        filtered = self._seed_filter_authors(results, disciplines, specialties)
        next_cursor = batch.get("next_cursor")
        written = self.storage.persist_seed_batch(
            search_run_id,
            filtered,
            next_cursor=next_cursor,
            has_more=bool(batch.get("has_more")),
        )
        if written:
            self.storage.bump_daily_stat("seeded", written)
            _log(f"seeded {written} authors (fetched {len(results)}, pending was {pending})")

        if not batch.get("has_more") or not next_cursor:
            _log("OpenAlex seed cursor exhausted for current filters")

    # -- email fetching --------------------------------------------------
    def _fetch_cycle(self, run: Dict[str, Any]) -> bool:
        """Run one ORCID email-fetch batch. Returns True if a 429 was observed."""
        concurrency = int(run.get("effective_concurrency") or COLLECT_BASELINE_CONCURRENCY)
        delay = float(run.get("effective_delay") or COLLECT_BASELINE_DELAY_SEC)
        limit = max(concurrency, COLLECT_FETCH_BATCH)

        search_run_id = int(run.get("search_run_id") or 0)
        pending = self.storage.get_pending_harvest(
            limit=limit,
            require_orcid=True,
            search_run_id=search_run_id,
        )
        if not pending:
            return False

        authors = [
            {
                "orcid_id": row.get("orcid_id"),
                "name": row.get("author_name"),
                "openalex_id": row.get("openalex_id"),
                "discipline": row.get("discipline"),
            }
            for row in pending
        ]

        results = asyncio.run(
            fetch_emails_async(
                authors,
                max_concurrent=concurrency,
                delay_between_batches=delay,
            )
        )

        saw_429 = False
        now = _utcnow()
        for result in results:
            openalex_id = result.get("openalex_id")
            if not openalex_id:
                continue
            self.storage.bump_daily_stat("attempts", 1)
            self.storage.bump_search_stat(search_run_id, "attempts", 1)

            if result.get("rate_limited"):
                saw_429 = True

            email = result.get("email")
            status = result.get("email_status") or EMAIL_STATUS_NO_EMAIL

            if email:
                self.storage.update_harvest_email(
                    openalex_id=openalex_id,
                    email=email,
                    status=EMAIL_STATUS_FOUND,
                    email_source="orcid",
                )
                self.storage.upsert_author_profile(
                    orcid_id=result.get("orcid_id") or "",
                    author_name=result.get("name") or "",
                    email=email,
                    openalex_id=openalex_id,
                    scientific_domain=result.get("discipline") or "",
                    source="collector",
                )
                self.storage.bump_daily_stat("emails_found", 1)
                self.storage.bump_search_stat(search_run_id, "emails_found", 1)
            elif status == EMAIL_STATUS_ERROR:
                self.storage.update_harvest_email(
                    openalex_id=openalex_id,
                    status=EMAIL_STATUS_ERROR,
                    next_retry_at=now + timedelta(hours=COLLECT_ERROR_RETRY_HOURS),
                )
            elif result.get("rate_limited"):
                # Leave pending so it is retried after cooldown; only bump attempt.
                pass
            else:
                self.storage.update_harvest_email(
                    openalex_id=openalex_id,
                    status=EMAIL_STATUS_NO_EMAIL,
                    next_retry_at=now + timedelta(days=COLLECT_NO_EMAIL_RETRY_DAYS),
                )

        if saw_429:
            self.storage.bump_daily_stat("orcid_429", 1)
            self.storage.bump_search_stat(search_run_id, "orcid_429", 1)
        return saw_429

    # -- state machine ---------------------------------------------------
    def _enter_cooldown(self, run: Dict[str, Any]) -> None:
        now = _utcnow()
        self.storage.update_run_state(
            status=RUN_STATUS_COOLDOWN,
            effective_concurrency=1,
            effective_delay=round(random.uniform(COLLECT_COOLDOWN_DELAY_MIN, COLLECT_COOLDOWN_DELAY_MAX), 2),
            cooldown_until=now + timedelta(minutes=COLLECT_COOLDOWN_MIN),
            last_429_at=now,
            run_429_count=int(run.get("run_429_count") or 0) + 1,
            clean_batches=0,
        )
        _log(f"429 detected -> COOLDOWN for {COLLECT_COOLDOWN_MIN} min")

    def _stop_for_today(self, run: Dict[str, Any]) -> None:
        now = _utcnow()
        self.storage.update_run_state(
            status=RUN_STATUS_STOPPED_TODAY,
            stop_until=_next_utc_midnight(now),
            last_429_at=now,
            run_429_count=int(run.get("run_429_count") or 0) + 1,
        )
        _log("429 during RECOVERY -> STOPPED_TODAY until next UTC midnight")

    def _advance_recovery(self, run: Dict[str, Any]) -> None:
        """On a clean batch in RECOVERY, ramp concurrency/delay back to baseline."""
        baseline_concurrency = int(run.get("baseline_concurrency") or COLLECT_BASELINE_CONCURRENCY)
        baseline_delay = float(run.get("baseline_delay") or COLLECT_BASELINE_DELAY_SEC)
        clean = int(run.get("clean_batches") or 0) + 1
        eff_concurrency = int(run.get("effective_concurrency") or 1)
        eff_delay = float(run.get("effective_delay") or baseline_delay)

        if clean >= COLLECT_RECOVERY_STEP:
            clean = 0
            eff_concurrency = min(eff_concurrency + 1, baseline_concurrency)
            eff_delay = max(baseline_delay, eff_delay - 1.0)

        if eff_concurrency >= baseline_concurrency and eff_delay <= baseline_delay:
            self.storage.update_run_state(
                status=RUN_STATUS_ACTIVE,
                effective_concurrency=baseline_concurrency,
                effective_delay=baseline_delay,
                clean_batches=0,
            )
            _log("RECOVERY complete -> ACTIVE")
        else:
            self.storage.update_run_state(
                effective_concurrency=eff_concurrency,
                effective_delay=eff_delay,
                clean_batches=clean,
            )

    # -- main loop -------------------------------------------------------
    def run_forever(self) -> None:
        _log("worker starting")
        try:
            while True:
                try:
                    self._tick()
                except KeyboardInterrupt:
                    _log("worker stopping (KeyboardInterrupt)")
                    return
                except Exception as exc:  # keep the worker alive on unexpected errors
                    _log(f"unexpected error: {exc}")
                    time.sleep(COLLECT_IDLE_POLL_SEC)
        finally:
            self.storage.release_worker_lease(self.worker_owner)

    def _tick(self) -> None:
        if not self.storage.available:
            _log("database unavailable; retrying")
            time.sleep(COLLECT_IDLE_POLL_SEC)
            return

        # Keep deploy failover short; every tick renews this singleton lease.
        if not self.storage.acquire_worker_lease(self.worker_owner, lease_seconds=120):
            time.sleep(COLLECT_IDLE_POLL_SEC)
            return

        if self._process_bulk_email_once():
            time.sleep(COLLECT_CYCLE_PAUSE_SEC)
            return

        if self._process_email_lookup_once():
            time.sleep(COLLECT_CYCLE_PAUSE_SEC)
            return

        run = self.storage.get_active_run()
        if not run:
            time.sleep(COLLECT_IDLE_POLL_SEC)
            return

        status = run.get("status") or RUN_STATUS_IDLE
        now = _utcnow()

        if status in (RUN_STATUS_IDLE, RUN_STATUS_PAUSED):
            time.sleep(COLLECT_IDLE_POLL_SEC)
            return

        if status == RUN_STATUS_STOPPED_TODAY:
            stop_until = run.get("stop_until")
            if stop_until and now >= stop_until:
                self.storage.update_run_state(
                    status=RUN_STATUS_ACTIVE,
                    effective_concurrency=int(run.get("baseline_concurrency") or COLLECT_BASELINE_CONCURRENCY),
                    effective_delay=float(run.get("baseline_delay") or COLLECT_BASELINE_DELAY_SEC),
                    run_429_count=0,
                    clean_batches=0,
                )
                _log("new UTC day -> resuming ACTIVE")
            else:
                time.sleep(COLLECT_IDLE_POLL_SEC)
            return

        if status == RUN_STATUS_COOLDOWN:
            cooldown_until = run.get("cooldown_until")
            if cooldown_until and now < cooldown_until:
                time.sleep(COLLECT_IDLE_POLL_SEC)
                return
            self.storage.update_run_state(status=RUN_STATUS_RECOVERY, clean_batches=0)
            _log("cooldown elapsed -> RECOVERY")
            return

        # ACTIVE or RECOVERY: do real work.
        self._seed_if_needed(run)
        run = self.storage.get_active_run() or run
        saw_429 = self._fetch_cycle(run)

        if saw_429:
            if run.get("status") == RUN_STATUS_RECOVERY:
                self._stop_for_today(run)
            else:
                self._enter_cooldown(run)
        elif run.get("status") == RUN_STATUS_RECOVERY:
            self._advance_recovery(run)

        time.sleep(COLLECT_CYCLE_PAUSE_SEC)

    def _process_bulk_email_once(self) -> bool:
        """Let durable bulk email jobs make progress even when collection is idle."""
        try:
            if self.bulk_email_worker is None:
                self.bulk_email_worker = BulkEmailWorker()
            return self.bulk_email_worker.process_next()
        except Exception as exc:
            _log(f"bulk email worker unavailable: {exc}")
            self.bulk_email_worker = None
            return False

    def _process_email_lookup_once(self) -> bool:
        rows = self.storage.claim_email_lookup_batch(limit=COLLECT_FETCH_BATCH)
        if not rows:
            return False
        authors = [{
            "orcid_id": row.get("orcid_id"), "openalex_id": row.get("openalex_id"),
            "name": row.get("author_name"), "institution": row.get("institution"),
            "country": row.get("country"), "discipline": row.get("discipline"),
        } for row in rows]
        try:
            results = asyncio.run(fetch_emails_async(
                authors, max_concurrent=COLLECT_BASELINE_CONCURRENCY,
                delay_between_batches=COLLECT_BASELINE_DELAY_SEC,
            ))
            by_orcid = {str(item.get("orcid_id") or "").replace("https://orcid.org/", ""): item for item in results}
            missing = []
            for row, author in zip(rows, authors):
                result = by_orcid.get(row.get("orcid_id")) or {}
                if not result.get("email") and (row.get("use_tavily") or row.get("use_openai_web")):
                    missing.append(author)
            web_by_orcid = {}
            if missing:
                async def fetch_web():
                    async with AsyncOpenAIEmailClient(max_concurrent=3, delay_between_requests=0.5) as client:
                        return await client.fetch_emails_batch(
                            missing,
                            use_tavily=bool(rows[0].get("use_tavily")),
                            use_openai_web=bool(rows[0].get("use_openai_web")),
                        )
                web_results = asyncio.run(fetch_web())
                web_by_orcid = {str(item.get("orcid_id") or "").replace("https://orcid.org/", ""): item for item in web_results}
            for row in rows:
                result = by_orcid.get(row.get("orcid_id")) or {}
                web = web_by_orcid.get(row.get("orcid_id")) or {}
                email = result.get("email") or web.get("email") or ""
                source = "orcid" if result.get("email") else (web.get("email_source") or web.get("source") or "")
                if email:
                    self.storage.upsert_author_profile(
                        orcid_id=row.get("orcid_id") or "", author_name=row.get("author_name") or "",
                        email=email, openalex_id=row.get("openalex_id") or "",
                        scientific_domain=row.get("discipline") or "", source=source or "lookup_job",
                    )
                self.storage.finish_email_lookup_recipient(row["id"], email=email, source=source)
        except Exception as exc:
            _log(f"email lookup batch failed: {exc}")
            for row in rows:
                self.storage.finish_email_lookup_recipient(row["id"], error=str(exc))
        return True


def main() -> None:
    worker = CollectorWorker()
    worker.run_forever()


if __name__ == "__main__":
    main()
