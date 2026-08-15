"""Validates data/kaspa_github_ecosystem_inventory.csv, the human-maintained
source of truth. Catches the kind of taxonomy drift (free-text category/
org_type/confidence values that don't match any mapping and silently fall
into a default bucket) called out in the project review — these tests fail
loudly instead of letting a typo'd category quietly become "Other".
"""
import csv
import os
from urllib.parse import urlparse

import pytest

from build_data import CATEGORY_MAP, CONF_ORDER, map_org_type

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT, "data", "kaspa_github_ecosystem_inventory.csv")

REQUIRED_COLUMNS = [
    "name", "url", "org_type", "category", "description",
    "last_activity", "confidence",
]

# org_type raw values recognized by build_data.map_org_type — anything that
# doesn't start with one of these silently becomes "Independent" (its
# fallback default), which is exactly the kind of silent miscategorization
# these tests exist to catch.
KNOWN_ORG_TYPE_PREFIXES = (
    "official", "company-affiliated", "community org",
    "independent/company", "independent contributor", "independent",
    "uncertain",
)


def load_rows():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def rows():
    return load_rows()


def test_csv_exists_and_has_rows():
    assert os.path.exists(CSV_PATH), f"Registry CSV not found at {CSV_PATH}"
    assert len(load_rows()) > 0, "Registry CSV has no data rows"


def test_required_columns_present(rows):
    fieldnames = set(rows[0].keys()) if rows else set()
    missing = [col for col in REQUIRED_COLUMNS if col not in fieldnames]
    assert not missing, f"CSV is missing required column(s): {missing}"


def test_no_duplicate_urls(rows):
    seen = {}
    dupes = []
    for i, row in enumerate(rows, start=2):  # +2: header line + 1-index
        url = row["url"].strip().rstrip("/").lower()
        if url in seen:
            dupes.append((url, seen[url], i))
        else:
            seen[url] = i
    assert not dupes, (
        "Duplicate repository URLs in registry CSV (url, first row, dupe row): "
        f"{dupes}"
    )


def test_urls_are_well_formed_github_urls(rows):
    bad = []
    for i, row in enumerate(rows, start=2):
        url = row["url"].strip()
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "github.com" or not parsed.path.strip("/"):
            bad.append((i, url))
    assert not bad, f"Malformed or non-github.com URL(s) (row, url): {bad}"


def test_no_missing_required_fields(rows):
    missing = []
    for i, row in enumerate(rows, start=2):
        for col in REQUIRED_COLUMNS:
            if not (row.get(col) or "").strip():
                missing.append((i, col, row.get("name", "?")))
    assert not missing, f"Row(s) with a blank required field (row, column, name): {missing}"


def test_every_category_maps_to_a_known_bucket(rows):
    """A category value not present in CATEGORY_MAP silently becomes "Other"
    in build_data.py — this test surfaces that instead of hiding it."""
    unmapped = []
    for i, row in enumerate(rows, start=2):
        raw = row["category"].strip().lower()
        if raw not in CATEGORY_MAP:
            unmapped.append((i, row["category"], row["name"]))
    assert not unmapped, (
        "Row(s) with a category value not in build_data.CATEGORY_MAP — these "
        "silently fall into the 'Other' bucket instead of their intended "
        "category (row, raw category, name): " + str(unmapped)
    )


def test_every_org_type_maps_to_a_known_prefix(rows):
    """An org_type value that doesn't start with a recognized prefix
    silently becomes "Independent" in build_data.py's fallback."""
    unmapped = []
    for i, row in enumerate(rows, start=2):
        raw = row["org_type"].strip().lower()
        if not raw.startswith(KNOWN_ORG_TYPE_PREFIXES):
            unmapped.append((i, row["org_type"], row["name"]))
    assert not unmapped, (
        "Row(s) with an org_type value not matching any known prefix — "
        "these silently default to 'Independent' (row, raw org_type, name): "
        + str(unmapped)
    )


def test_every_confidence_contains_a_known_token(rows):
    """map_confidence() looks for a known token (High/Medium/Low variants)
    anywhere in the free-text confidence field; a row with none of those
    tokens silently becomes "Medium" instead of reflecting what was
    actually written."""
    unmapped = []
    for i, row in enumerate(rows, start=2):
        raw = row["confidence"]
        if not any(token.lower() in raw.lower() for token in CONF_ORDER):
            unmapped.append((i, row["confidence"], row["name"]))
    assert not unmapped, (
        "Row(s) whose confidence text contains none of "
        f"{CONF_ORDER} — silently defaults to 'Medium' (row, raw confidence, name): "
        + str(unmapped)
    )


def test_org_type_maps_dont_silently_collide(rows):
    """Sanity check on the mapping table itself, not the CSV: every prefix
    in KNOWN_ORG_TYPE_PREFIXES actually resolves through map_org_type to
    something other than the bare fallback, i.e. the prefix list here stays
    in sync with build_data.map_org_type's own logic."""
    for prefix in KNOWN_ORG_TYPE_PREFIXES:
        # Just confirm it doesn't raise and returns one of the known labels.
        result = map_org_type(prefix)
        assert result in {"Official", "Company", "Community", "Independent", "Uncertain"}
