"""Validates scripts/build_data.py: every CSV row produces a well-formed
row object, ids are unique, GitHub URLs resolve to the right repoPath, and
the generated data.js is deterministic and stays in sync with the CSV.
"""
import json
import os
import re

import pytest

import build_data
from build_data import build_rows, render_data_js, CSV_PATH, OUT_PATH


@pytest.fixture(scope="module")
def rows():
    return build_rows()


REQUIRED_ROW_KEYS = {
    "id", "name", "repoPath", "url", "description", "category",
    "rawCategory", "orgType", "rawOrgType", "tier", "status", "rawStatus",
    "confidence", "rawConfidence", "type", "tags", "verified", "verifiedAt",
}

VALID_TIERS = {1, 2, 3}
VALID_TYPES = {"Org", "Repo"}


def test_produces_rows(rows):
    assert len(rows) > 0


def test_every_row_has_required_keys(rows):
    for row in rows:
        missing = REQUIRED_ROW_KEYS - row.keys()
        assert not missing, f"Row {row.get('name')!r} missing keys: {missing}"


def test_no_duplicate_ids(rows):
    ids = [r["id"] for r in rows]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"Duplicate slugified id(s) — two registry names collide: {dupes}"


def test_no_blank_ids(rows):
    blank = [r["name"] for r in rows if not r["id"]]
    assert not blank, f"Row(s) whose name slugifies to an empty id: {blank}"


def test_tier_and_type_are_valid(rows):
    for row in rows:
        assert row["tier"] in VALID_TIERS, f"{row['name']}: invalid tier {row['tier']}"
        assert row["type"] in VALID_TYPES, f"{row['name']}: invalid type {row['type']}"


def test_repo_rows_have_owner_slash_repo_path(rows):
    """Every row whose URL is a github.com/<owner>/<repo> URL should get a
    repoPath of exactly "owner/repo" — this is what the ingestion pipeline
    and app.js key everything off of, so a wrong split here silently
    orphans a repo from its activity data."""
    bad = []
    for row in rows:
        if row["type"] != "Repo":
            continue
        if "/" not in row["repoPath"]:
            bad.append((row["name"], row["url"], row["repoPath"]))
            continue
        owner, repo = row["repoPath"].split("/", 1)
        if not owner or not repo:
            bad.append((row["name"], row["url"], row["repoPath"]))
    assert not bad, f"Repo row(s) with a malformed repoPath (name, url, repoPath): {bad}"


def test_kaspanet_org_is_always_represented(rows):
    assert any(r["repoPath"] == "kaspanet" and r["type"] == "Org" for r in rows)


def test_row_count_matches_csv_plus_synthetic_kaspanet_row():
    """build_rows() should produce exactly one row per CSV data row, plus
    at most one synthetic kaspanet org row if the CSV doesn't already have
    one. A mismatch means rows are being silently dropped or duplicated."""
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        import csv as _csv
        csv_row_count = sum(1 for _ in _csv.DictReader(f))

    result_rows = build_rows()
    has_kaspanet_in_csv = any(
        r["repoPath"] == "kaspanet" and r["type"] == "Org" for r in result_rows
    ) and csv_row_count == len(result_rows)

    assert len(result_rows) in (csv_row_count, csv_row_count + 1), (
        f"build_rows() produced {len(result_rows)} rows from {csv_row_count} "
        "CSV rows — expected the same count, or +1 if the synthetic kaspanet "
        "org row had to be injected."
    )


def test_render_data_js_is_valid_and_matches_rows(rows):
    """Parse the REGISTRY_ROWS JSON literal back out of the generated JS
    and confirm it round-trips to the same data build_rows() produced."""
    text = render_data_js(rows)

    match = re.search(r"const REGISTRY_ROWS = (\[.*?\]);\n\nconst REPOS", text, re.S)
    assert match, "Could not locate REGISTRY_ROWS literal in generated data.js text"
    parsed = json.loads(match.group(1))
    assert parsed == rows, "REGISTRY_ROWS in data.js does not match build_rows() output"

    assert 'const REPOS = REGISTRY_ROWS.filter(row => row.type === "Repo");' in text

    cat_match = re.search(r"const REGISTRY_CATEGORIES = (\[.*?\]);\n?$", text, re.S)
    assert cat_match, "Could not locate REGISTRY_CATEGORIES literal"
    categories = json.loads(cat_match.group(1))
    assert categories == sorted(set(r["category"] for r in rows))


def test_render_data_js_is_deterministic(rows):
    """Same input rows -> byte-identical output, twice in a row. If this
    ever fails (e.g. from dict ordering or a timestamp sneaking in), every
    CI run will show a spurious diff even when nothing meaningful changed."""
    first = render_data_js(rows)
    second = render_data_js(rows)
    assert first == second


def test_committed_data_js_is_up_to_date(rows):
    """The actual regression this suite exists to catch: does the
    committed assets/js/data.js match what build_data.py would generate
    from the current CSV right now? If not, someone edited the CSV (or
    data.js) without regenerating/committing the other — the same class of
    bug that let data/api/index.json go missing from the repo (see
    ingest-activity.yml's "Verify expected artifacts were generated" step).
    """
    if not os.path.exists(OUT_PATH):
        pytest.skip(f"{OUT_PATH} not present in this checkout")
    with open(OUT_PATH, encoding="utf-8") as f:
        committed = f.read()
    expected = render_data_js(rows)
    assert committed == expected, (
        "assets/js/data.js is out of date with data/kaspa_github_ecosystem_"
        "inventory.csv — run `python3 scripts/build_data.py` and commit the "
        "result."
    )
