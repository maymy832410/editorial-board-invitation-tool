"""Discipline categorization based on OpenAlex field.display_name.

OpenAlex topics have a 4-level hierarchy: domain > field > subfield > topic.
We use the 'field' level (~25 categories) as the discipline, extracted directly
from the API response rather than maintaining a custom mapping.
"""

# OpenAlex field names (used for UI defaults when no results are loaded yet)
ALL_DISCIPLINES = sorted([
    "Agricultural and Biological Sciences",
    "Arts and Humanities",
    "Biochemistry, Genetics and Molecular Biology",
    "Business, Management and Accounting",
    "Chemical Engineering",
    "Chemistry",
    "Computer Science",
    "Decision Sciences",
    "Dentistry",
    "Earth and Planetary Sciences",
    "Economics, Econometrics and Finance",
    "Energy",
    "Engineering",
    "Environmental Science",
    "Health Professions",
    "Immunology and Microbiology",
    "Materials Science",
    "Mathematics",
    "Medicine",
    "Multidisciplinary",
    "Neuroscience",
    "Nursing",
    "Pharmacology, Toxicology and Pharmaceutics",
    "Physics and Astronomy",
    "Psychology",
    "Social Sciences",
    "Veterinary",
])


def get_discipline_from_topics(topics: list) -> str:
    """Determine the primary discipline from author's topics using field.display_name.

    Args:
        topics: List of topic dicts from OpenAlex author record

    Returns:
        Most frequent field name or "Other"
    """
    if not topics:
        return "Other"

    field_counts: dict[str, int] = {}
    for topic in topics:
        field_data = topic.get("field", {})
        field_name = field_data.get("display_name")
        if field_name:
            field_counts[field_name] = field_counts.get(field_name, 0) + 1

    if field_counts:
        return max(field_counts, key=field_counts.get)

    return "Other"


def categorize_authors(authors: list) -> list:
    """Add discipline field to each author based on their topics."""
    for author in authors:
        topics = author.get("_topics", [])
        author["discipline"] = get_discipline_from_topics(topics)
    return authors
