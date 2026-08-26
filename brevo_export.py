"""Helpers for filtering and formatting Brevo contact-list exports."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


BREVO_CSV_FIELDS = ["EMAIL", "FIRSTNAME", "ORCID", "COUNTRY", "DISCIPLINE"]
VALID_EXPORT_SOURCES = {"all", "profiles", "harvested"}
EMPTY_BREVO_EXPORT_COUNTS = {
    "eligible": 0,
    "excluded_suppressed": 0,
    "excluded_retracted": 0,
    "excluded_country": 0,
    "excluded_discipline": 0,
    "total_with_email": 0,
}


@dataclass(frozen=True)
class BrevoExportFilters:
    """Normalized filters for a database-wide Brevo CSV export."""

    source: str = "all"
    query: str = ""
    include_countries: tuple[str, ...] = field(default_factory=tuple)
    exclude_countries: tuple[str, ...] = field(default_factory=tuple)
    disciplines: tuple[str, ...] = field(default_factory=tuple)
    include_suppressed: bool = False
    include_retracted: bool = False
    exclude_invited: bool = False


def _unique_upper(values: Iterable[str] | None) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values or []:
        text = str(value or "").strip().upper()
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


def _unique_text(values: Iterable[str] | None) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values or []:
        text = " ".join(str(value or "").strip().split())
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


def normalize_brevo_export_filters(
    source: str = "all",
    query: str = "",
    include_countries: Iterable[str] | None = None,
    exclude_countries: Iterable[str] | None = None,
    disciplines: Iterable[str] | None = None,
    include_suppressed: bool = False,
    include_retracted: bool = False,
    exclude_invited: bool = False,
) -> BrevoExportFilters:
    """Normalize UI/API filter values for SQL and in-memory tests."""
    normalized_source = (source or "all").strip().lower()
    if normalized_source not in VALID_EXPORT_SOURCES:
        normalized_source = "all"
    return BrevoExportFilters(
        source=normalized_source,
        query=" ".join((query or "").strip().split()),
        include_countries=_unique_upper(include_countries),
        exclude_countries=_unique_upper(exclude_countries),
        disciplines=_unique_text(disciplines),
        include_suppressed=bool(include_suppressed),
        include_retracted=bool(include_retracted),
        exclude_invited=bool(exclude_invited),
    )


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def country_matches(country: str, filters: BrevoExportFilters) -> bool:
    """Return whether a country code passes include/exclude filters."""
    code = (country or "").strip().upper()
    if filters.include_countries and code not in filters.include_countries:
        return False
    if filters.exclude_countries and code in filters.exclude_countries:
        return False
    return True


def discipline_matches(discipline: str, filters: BrevoExportFilters) -> bool:
    """Return whether a discipline label passes the include filter."""
    if not filters.disciplines:
        return True
    return (discipline or "").strip() in filters.disciplines


def row_is_eligible(row: dict[str, Any], filters: BrevoExportFilters) -> bool:
    """Apply the same export rules used by the SQL preview/download."""
    if not _normalize_email(row.get("email") or ""):
        return False
    if not filters.include_suppressed and row.get("is_suppressed"):
        return False
    if not filters.include_retracted and row.get("is_retracted"):
        return False
    if filters.exclude_invited and row.get("is_invited"):
        return False
    if not country_matches(str(row.get("country") or ""), filters):
        return False
    if not discipline_matches(str(row.get("discipline") or ""), filters):
        return False
    return True


def summarize_brevo_export_rows(
    rows: Iterable[dict[str, Any]],
    filters: BrevoExportFilters,
) -> dict[str, int]:
    """Count eligible and excluded rows after email dedupe."""
    counts = dict(EMPTY_BREVO_EXPORT_COUNTS)
    for row in dedupe_brevo_export_rows(rows):
        counts["total_with_email"] += 1
        is_suppressed = bool(row.get("is_suppressed"))
        is_retracted = bool(row.get("is_retracted"))
        country_ok = country_matches(str(row.get("country") or ""), filters)
        discipline_ok = discipline_matches(str(row.get("discipline") or ""), filters)
        if is_suppressed and not filters.include_suppressed:
            counts["excluded_suppressed"] += 1
        if is_retracted and not filters.include_retracted:
            counts["excluded_retracted"] += 1
        if not country_ok:
            counts["excluded_country"] += 1
        if not discipline_ok:
            counts["excluded_discipline"] += 1
        if row_is_eligible(row, filters):
            counts["eligible"] += 1
    return counts


def dedupe_brevo_export_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one row per email, preferring profiles over harvested authors."""
    ranked = sorted(
        (
            row
            for row in rows
            if _normalize_email(row.get("email") or "")
        ),
        key=lambda row: (
            _normalize_email(row.get("email") or ""),
            int(row.get("source_priority") or 99),
            str(row.get("author_name") or ""),
        ),
    )
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in ranked:
        email = _normalize_email(row.get("email") or "")
        if email in seen:
            continue
        seen.add(email)
        deduped.append(row)
    return deduped


def format_brevo_csv_row(row: dict[str, Any]) -> dict[str, str]:
    """Map a database row to Brevo CSV columns."""
    return {
        "EMAIL": (row.get("email") or row.get("EMAIL") or "").strip(),
        "FIRSTNAME": (row.get("author_name") or row.get("FIRSTNAME") or "").strip(),
        "ORCID": (row.get("orcid_id") or row.get("ORCID") or "").strip(),
        "COUNTRY": (row.get("country") or row.get("COUNTRY") or "").strip(),
        "DISCIPLINE": (row.get("discipline") or row.get("DISCIPLINE") or "").strip(),
    }


def write_brevo_csv(rows: Iterable[dict[str, Any]]) -> str:
    """Render a Brevo-ready CSV string."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=BREVO_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(format_brevo_csv_row(row))
    return output.getvalue()


def _table_names(names: dict[str, str] | None) -> dict[str, str]:
    return {
        "profiles": "author_profiles",
        "harvested": "harvested_authors",
        "suppressions": "email_suppressions",
        "retracted": "retracted_authors",
        "invitations": "author_invitations",
        **(names or {}),
    }


def _source_union_sql(
    filters: BrevoExportFilters,
    tables: dict[str, str],
) -> tuple[str, list[Any]]:
    """Build the profile/harvested union that feeds export counts and rows."""
    parts: list[str] = []
    params: list[Any] = []
    if filters.source in {"all", "profiles"}:
        parts.append(
            f"""
            SELECT
                p.email,
                LOWER(p.email) AS email_lower,
                COALESCE(p.author_name, '') AS author_name,
                COALESCE(p.orcid_id, '') AS orcid_id,
                COALESCE(hc.country, '') AS country,
                COALESCE(p.scientific_domain, '') AS discipline,
                1 AS source_priority
            FROM {tables['profiles']} p
            LEFT JOIN (
                SELECT DISTINCT ON (orcid_id) orcid_id, country
                FROM {tables['harvested']}
                WHERE orcid_id <> '' AND COALESCE(country, '') <> ''
                ORDER BY orcid_id, updated_at DESC NULLS LAST
            ) hc ON hc.orcid_id = p.orcid_id
            WHERE p.email IS NOT NULL
              AND p.email LIKE %s
            """
        )
        params.append("%@%")
    if filters.source in {"all", "harvested"}:
        parts.append(
            f"""
            SELECT
                h.email,
                LOWER(h.email) AS email_lower,
                COALESCE(h.author_name, '') AS author_name,
                COALESCE(h.orcid_id, '') AS orcid_id,
                COALESCE(h.country, '') AS country,
                COALESCE(h.discipline, '') AS discipline,
                2 AS source_priority
            FROM {tables['harvested']} h
            WHERE h.email IS NOT NULL
              AND h.email LIKE %s
              AND COALESCE(h.email_status, '') IN ('', 'found')
            """
        )
        params.append("%@%")
    return " UNION ALL ".join(parts), params


def _annotated_cte_sql(
    filters: BrevoExportFilters,
    tables: dict[str, str],
) -> tuple[str, list[Any]]:
    union_sql, params = _source_union_sql(filters, tables)
    query_sql = "TRUE"
    if filters.query:
        like = f"%{filters.query.lower()}%"
        query_sql = """(
            LOWER(COALESCE(author_name, '')) LIKE %s
            OR email_lower LIKE %s
            OR LOWER(COALESCE(orcid_id, '')) LIKE %s
        )"""
        params.extend([like, like, like])

    sql = f"""
        WITH raw_contacts AS (
            {union_sql}
        ),
        searched AS (
            SELECT *
            FROM raw_contacts
            WHERE {query_sql}
        ),
        deduped AS (
            SELECT DISTINCT ON (email_lower)
                email,
                email_lower,
                author_name,
                orcid_id,
                country,
                discipline
            FROM searched
            ORDER BY email_lower, source_priority ASC, author_name DESC
        ),
        annotated AS (
            SELECT
                d.*,
                EXISTS (
                    SELECT 1
                    FROM {tables['suppressions']} s
                    WHERE s.is_suppressed = TRUE
                      AND (
                          s.email_lower = d.email_lower
                          OR (d.orcid_id <> '' AND s.orcid_id = d.orcid_id)
                      )
                ) AS is_suppressed,
                EXISTS (
                    SELECT 1
                    FROM {tables['retracted']} r
                    WHERE r.author_name_lower = LOWER(d.author_name)
                ) AS is_retracted,
                EXISTS (
                    SELECT 1
                    FROM {tables['invitations']} i
                    WHERE (d.orcid_id <> '' AND i.orcid_id = d.orcid_id)
                       OR LOWER(COALESCE(i.email, '')) = d.email_lower
                ) AS is_invited
            FROM deduped d
        )
    """
    return sql, params


def _eligibility_params(filters: BrevoExportFilters) -> tuple[str, list[Any]]:
    params: list[Any] = [
        filters.include_suppressed,
        filters.include_retracted,
        filters.exclude_invited,
        bool(filters.include_countries),
        list(filters.include_countries) or [""],
        bool(filters.exclude_countries),
        list(filters.exclude_countries) or [""],
        bool(filters.disciplines),
        list(filters.disciplines) or [""],
    ]
    clause = """
        (%s OR NOT is_suppressed)
        AND (%s OR NOT is_retracted)
        AND (NOT %s OR NOT is_invited)
        AND (NOT %s OR UPPER(COALESCE(country, '')) = ANY(%s))
        AND (NOT %s OR UPPER(COALESCE(country, '')) <> ALL(%s))
        AND (NOT %s OR discipline = ANY(%s))
    """
    return clause, params


def build_brevo_export_count_sql(
    filters: BrevoExportFilters,
    table_names: dict[str, str] | None = None,
) -> tuple[str, list[Any]]:
    """Return SQL that counts eligible and excluded unique emails."""
    tables = _table_names(table_names)
    cte_sql, params = _annotated_cte_sql(filters, tables)
    eligible_clause, eligible_params = _eligibility_params(filters)
    sql = f"""
        {cte_sql}
        SELECT
            COUNT(*) AS total_with_email,
            COUNT(*) FILTER (WHERE {eligible_clause}) AS eligible,
            COUNT(*) FILTER (
                WHERE is_suppressed AND NOT %s
            ) AS excluded_suppressed,
            COUNT(*) FILTER (
                WHERE is_retracted AND NOT %s
            ) AS excluded_retracted,
            COUNT(*) FILTER (
                WHERE ( %s AND UPPER(COALESCE(country, '')) <> ALL(%s) )
                   OR ( %s AND UPPER(COALESCE(country, '')) = ANY(%s) )
            ) AS excluded_country,
            COUNT(*) FILTER (
                WHERE %s AND discipline <> ALL(%s)
            ) AS excluded_discipline
        FROM annotated;
    """
    params.extend(eligible_params)
    params.extend(
        [
            filters.include_suppressed,
            filters.include_retracted,
            bool(filters.include_countries),
            list(filters.include_countries) or [""],
            bool(filters.exclude_countries),
            list(filters.exclude_countries) or [""],
            bool(filters.disciplines),
            list(filters.disciplines) or [""],
        ]
    )
    return sql, params


def build_brevo_export_rows_sql(
    filters: BrevoExportFilters,
    table_names: dict[str, str] | None = None,
) -> tuple[str, list[Any]]:
    """Return SQL that streams eligible contacts for the Brevo CSV."""
    tables = _table_names(table_names)
    cte_sql, params = _annotated_cte_sql(filters, tables)
    eligible_clause, eligible_params = _eligibility_params(filters)
    sql = f"""
        {cte_sql}
        SELECT email, author_name, orcid_id, country, discipline
        FROM annotated
        WHERE {eligible_clause}
        ORDER BY author_name ASC, email ASC;
    """
    params.extend(eligible_params)
    return sql, params


def parse_export_counts(row: dict[str, Any] | Sequence[Any] | None) -> dict[str, int]:
    """Normalize a COUNT query result into the preview payload."""
    counts = dict(EMPTY_BREVO_EXPORT_COUNTS)
    if row is None:
        return counts
    if not isinstance(row, dict):
        keys = list(EMPTY_BREVO_EXPORT_COUNTS)
        row = {keys[index]: value for index, value in enumerate(row)}
    for key in counts:
        counts[key] = int(row.get(key) or 0)
    return counts
