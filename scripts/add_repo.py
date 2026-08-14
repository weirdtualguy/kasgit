#!/usr/bin/env python3
"""
add_repo.py — add a new repo to the Kasgit registry without hand-editing the
CSV. Hand-editing data/kaspa_github_ecosystem_inventory.csv in a plain text
editor is a real footgun: the `description`/`last_activity` fields contain
commas and are quoted, and one stray unescaped comma silently shifts every
column after it for that row. This script writes the row through Python's
csv module instead, so it's always correctly quoted, and validates the
category/org_type against the exact vocabulary build_data.py understands
(anything else silently falls into "Other"/"Independent" there — this script
warns you before that happens instead of after).

Usage — interactive (recommended from Termux):
    python3 scripts/add_repo.py

Usage — non-interactive (for scripting; any omitted arg is prompted for):
    python3 scripts/add_repo.py \\
        --url https://github.com/someone/kaspa-thing \\
        --category "sdk/library" \\
        --org-type "independent contributor" \\
        --description "A thing that does the thing."

After appending, this regenerates data.js by running build_data.py (skip
with --no-build). It does NOT run the GitHub ingestion — the new repo shows
as a modeled placeholder in the Registry tab until the next ingestion run
(scheduled daily via GitHub Actions, or run manually: see
scripts/ingest_github_activity.py).
"""
import argparse
import csv
import datetime
import os
import subprocess
import sys
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(BASE_DIR, "data", "kaspa_github_ecosystem_inventory.csv")
BUILD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_data.py")

FIELDNAMES = ["name", "url", "org_type", "category", "description", "last_activity", "confidence", "verified", "verified_at"]

# (number shown to the user, friendly label, exact raw string written to the
# CSV — must match a key/prefix build_data.py's map_category/map_org_type
# actually recognizes, or the row silently becomes "Other"/"Independent").
CATEGORIES = [
    ("Core protocol", "core protocol"),
    ("Wallet", "wallet"),
    ("Explorer", "explorer"),
    ("Mining / pool", "mining/pool"),
    ("SDK / library", "sdk/library"),
    ("CLI tooling", "cli tooling"),
    ("API / indexer", "api/indexer"),
    ("KRC20 / token standard", "krc20/token standard"),
    ("Documentation", "documentation"),
    ("Infrastructure", "infrastructure"),
    ("DeFi", "other (defi)"),
    ("dApp", "other (dapp)"),
    ("Programmability / covenants", "programmability/covenants"),
    ("Other", "other"),
]

ORG_TYPES = [
    ("Official (kaspanet org)", "official"),
    ("Company-affiliated", "company-affiliated"),
    ("Community org", "community org"),
    ("Independent contributor", "independent contributor"),
    ("Uncertain — not sure this belongs yet", "uncertain"),
]


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def parse_github_url(url):
    """Returns (owner, repo, normalized_url) or None if not a
    github.com/<owner>/<repo> URL."""
    parsed = urlparse(url.strip())
    if parsed.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    repo = repo[:-4] if repo.endswith(".git") else repo
    return owner, repo, f"https://github.com/{owner}/{repo}"


def prompt(label, default=None, required=True):
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if not value and default is not None:
            return default
        if not value and not required:
            return ""
        if value:
            return value
        print("  (required)")


def prompt_choice(label, options):
    print(f"\n{label}")
    for i, (friendly, _raw) in enumerate(options, start=1):
        print(f"  {i}. {friendly}")
    while True:
        raw = input(f"Choose 1-{len(options)}: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][1]
        print("  (enter a number from the list)")


def load_existing_urls():
    if not os.path.exists(CSV_PATH):
        fail(f"registry CSV not found at {CSV_PATH}")
    urls = {}
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            parsed = parse_github_url(row.get("url", ""))
            if parsed:
                urls[f"{parsed[0]}/{parsed[1]}".lower()] = row.get("name", "")
    return urls


def main():
    ap = argparse.ArgumentParser(description="Add a repo to the Kasgit registry CSV.")
    ap.add_argument("--url", help="github.com/<owner>/<repo> URL")
    ap.add_argument("--name", help="Display name (default: repo name from the URL)")
    ap.add_argument("--category", help="Exact category string (see script source for the list)")
    ap.add_argument("--org-type", dest="org_type", help="Exact org_type string (see script source for the list)")
    ap.add_argument("--description", help="One or two sentences describing the project")
    ap.add_argument("--last-activity", dest="last_activity", default="", help="Free-text note; leave blank to let live ingestion speak for itself")
    ap.add_argument("--confidence", default="Medium", choices=["High", "Medium-High", "Medium", "Low-Medium", "Low"])
    ap.add_argument("--verified", action="store_true", help="Mark as manually checked against GitHub right now")
    ap.add_argument("--no-build", action="store_true", help="Don't regenerate data.js after appending")
    args = ap.parse_args()

    interactive = sys.stdin.isatty()

    url_input = args.url or (prompt("GitHub URL (https://github.com/owner/repo)") if interactive else fail("--url is required in non-interactive mode"))
    parsed = parse_github_url(url_input)
    if not parsed:
        fail(f"'{url_input}' doesn't look like a github.com/<owner>/<repo> URL")
    owner, repo_name, normalized_url = parsed

    existing = load_existing_urls()
    key = f"{owner}/{repo_name}".lower()
    if key in existing:
        fail(f"{owner}/{repo_name} is already in the registry (as \"{existing[key]}\"). Edit that row instead of adding a duplicate.")

    name = args.name or (prompt("Display name", default=repo_name) if interactive else repo_name)

    category = args.category
    if not category:
        if interactive:
            category = prompt_choice("Category:", CATEGORIES)
        else:
            fail("--category is required in non-interactive mode")

    org_type = args.org_type
    if not org_type:
        if interactive:
            org_type = prompt_choice("Org type:", ORG_TYPES)
        else:
            fail("--org-type is required in non-interactive mode")

    description = args.description or (prompt("Description (one or two sentences)") if interactive else fail("--description is required in non-interactive mode"))

    row = {
        "name": name,
        "url": normalized_url,
        "org_type": org_type,
        "category": category,
        "description": description,
        "last_activity": args.last_activity,
        "confidence": args.confidence,
        "verified": "yes" if args.verified else "",
        "verified_at": datetime.date.today().isoformat() if args.verified else "",
    }

    with open(CSV_PATH, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writerow(row)

    print(f"\nAdded: {name}  ({normalized_url})")
    print(f"  category={category!r}  org_type={org_type!r}")

    known_categories = {raw for _friendly, raw in CATEGORIES}
    if category.strip().lower() not in known_categories:
        print(f"  Warning: {category!r} doesn't match a known category string exactly — build_data.py")
        print("  will silently file this repo under 'Other' rather than erroring. Edit the CSV's category")
        print("  column directly if that's not what you want (see the CATEGORIES list in this script).")

    if org_type.strip().lower().startswith("uncertain"):
        print("  Note: org_type starts with 'uncertain', so this repo will be EXCLUDED from live")
        print("  ingestion and shown as a modeled placeholder until you change it — see Methodology.")

    if args.no_build:
        print("\nSkipped data.js rebuild (--no-build). Run scripts/build_data.py before publishing.")
        return

    print("\nRegenerating data.js...")
    result = subprocess.run([sys.executable, BUILD_SCRIPT], cwd=BASE_DIR)
    if result.returncode != 0:
        fail("build_data.py failed — data.js was NOT regenerated. Fix the error above and rerun scripts/build_data.py manually.")
    print("\nDone. This repo will show as modeled until the next ingestion run picks it up")
    print("(scheduled daily, or run scripts/ingest_github_activity.py manually before pushing).")


if __name__ == "__main__":
    main()
