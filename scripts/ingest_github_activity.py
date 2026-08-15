#!/usr/bin/env python3
"""
Scheduled GitHub activity ingestion for the Kaspa GitHub Ecosystem Dashboard.

Fetches commits, merged pull requests, issues, releases, and point-in-time
star/fork/watcher counts for tracked repositories, and writes normalized
JSON under data/activity/. The static front-end (assets/js/app.js) reads
these files at runtime and falls back to modeled placeholder data for any
repo that hasn't been ingested yet.

Tracked repos come from two sources:
  1. ORGS — full auto-discovery of every non-archived, non-fork repo owned
     by these GitHub accounts (works for both orgs and users).
  2. The registry CSV (data/kaspa_github_ecosystem_inventory.csv) — every
     row with a github.com/<owner>/<repo> URL is ingested individually,
     even if its owner isn't in ORGS. This is opt-out, not opt-in: a row
     is skipped only if org_type is "uncertain" or its verified column is
     explicitly "no" (someone checked it and it's wrong/dead/unrelated).
     Rows with verified=yes are still tagged with that reason in logs/meta,
     but no longer need it to be ingested — it's now just a "someone
     actually confirmed this one" marker, not a gate.

Each run also appends a point-in-time stars/forks/watchers snapshot to a
per-repo starHistory array, and — on a repo's first ingestion — attempts a
bounded backfill of real historical star growth from GitHub's stargazers
timestamp API (capped to avoid rate-limit blowups on very popular repos).

Real release metadata (not just counts) is collected across all tracked
repos and published as a combined feed at data/feed/releases.json and
data/feed/releases.xml.

Run manually:
    GITHUB_TOKEN=ghp_xxx python3 scripts/ingest_github_activity.py

Run in CI: see .github/workflows/ingest-activity.yml (scheduled + manual dispatch).
"""

import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

API_ROOT = "https://api.github.com"

# Full auto-discovery: every repo owned by these accounts is ingested.
# Works for GitHub Orgs and User accounts alike (see list_owner_repos).
ORGS = ["kaspanet"]

# Which org_type values from the registry CSV get individually ingested
# (in addition to whatever ORGS already covers), without pulling in the
# rest of that owner's unrelated repos. (Every other org_type is now
# ingested too, by default — see EXCLUDED_ORG_TYPES below — this list just
# controls the "reason" tag those rows get in logs/_meta.json.)
EXTRA_ORG_TYPE_PREFIXES = ("company-affiliated", "community org")

# org_type values that opt a registry row OUT of live ingestion. "Uncertain"
# means nobody has confirmed the repo is legitimate/relevant yet, so it stays
# on modeled placeholder data until the CSV's org_type or verified column
# says otherwise.
EXCLUDED_ORG_TYPES = ("uncertain",)

LOOKBACK_DAYS = 400  # covers the dashboard's 1Y range with margin
STAR_HISTORY_MAX_POINTS = 400
STARGAZER_BACKFILL_MAX_PAGES = 5   # 5 * 100 = up to 500 stargazers backfilled
FEED_MAX_ITEMS = 150
FEED_LOOKBACK_DAYS = 180

# Idea board: open issues on THIS repo (the dashboard's own repo, not a
# tracked ecosystem repo) labeled IDEAS_LABEL are published as a lightweight
# "wanted" board — see data/ideas.json and the Ideas tab in app.js. Set
# SELF_REPO_OVERRIDE to "owner/repo" for local/manual runs where
# GITHUB_REPOSITORY isn't set (GitHub Actions sets it automatically).
IDEAS_LABEL = "idea"
IDEAS_MAX_ITEMS = 60
SELF_REPO_OVERRIDE = None

# Public data API manifest (data/api/index.json) — a stable, documented entry
# point so someone can build a bot/alert/other-dashboard on top of this
# project's ingested data without reverse-engineering file names out of
# app.js. Bump MANIFEST_SCHEMA_VERSION only on a breaking change to an
# existing resource's shape (renamed/removed field, changed meaning) — adding
# a new optional field or a new resource doesn't require a bump.
MANIFEST_SCHEMA_VERSION = 1

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(BASE_DIR, "data", "activity")
FEED_DIR = os.path.join(BASE_DIR, "data", "feed")
IDEAS_PATH = os.path.join(OUTPUT_DIR, "ideas.json")
MANIFEST_PATH = os.path.join(BASE_DIR, "data", "api", "index.json")
CSV_PATH = os.path.join(BASE_DIR, "data", "kaspa_github_ecosystem_inventory.csv")
REQUEST_TIMEOUT = 30
PER_PAGE = 100

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "kaspa-github-ecosystem-dashboard-ingest",
})
if TOKEN:
    SESSION.headers["Authorization"] = f"Bearer {TOKEN}"


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def api_get(url, params=None, headers=None, stop_when=None):
    """GET with pagination support, basic retry, and rate-limit backoff.

    stop_when: optional callable(page_items) -> bool, checked after each page
    is fetched and appended to results. If it returns True, pagination stops
    without requesting further pages — used for endpoints like /pulls that
    don't support a `since` filter, so we can bound how many pages we pull
    by watching a sort-order-derived cutoff instead of fetching full history
    every run (see ingest_repo_activity).

    Returns (results, quality) where quality is "complete" if pagination
    ran to a natural end (no more Link: rel="next", a 404, or stop_when
    firing — all cases where we know we have everything in scope) or
    "partial" if it was abandoned after exhausting retries — in that case
    `results` holds whatever was fetched before the failure, silently
    covering less than the intended window unless a caller checks quality.
    """
    results = []
    next_url = url
    next_params = params
    request_headers = {**SESSION.headers, **(headers or {})}

    while next_url:
        for attempt in range(5):
            resp = SESSION.get(next_url, params=next_params, headers=request_headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                break
            if resp.status_code in (403, 429):
                remaining = resp.headers.get("X-RateLimit-Remaining")
                reset = resp.headers.get("X-RateLimit-Reset")
                if remaining == "0" and reset:
                    wait_seconds = max(0, int(reset) - int(time.time())) + 2
                    log(f"Rate limited. Sleeping {wait_seconds}s...")
                    time.sleep(min(wait_seconds, 900))
                    continue
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 404:
                return results, "complete"
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
        else:
            log(f"Giving up on {next_url} after retries")
            return results, "partial"

        if resp.status_code != 200:
            return results, "partial"

        page_data = resp.json()
        if isinstance(page_data, list):
            results.extend(page_data)
            if stop_when and stop_when(page_data):
                return results, "complete"
        else:
            return page_data, "complete"

        next_url = None
        next_params = None
        link_header = resp.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                next_url = part[part.find("<") + 1: part.find(">")]
                break

    return results, "complete"


def api_get_json(url, params=None, headers=None):
    """GET a single JSON object (not a paginated list)."""
    request_headers = {**SESSION.headers, **(headers or {})}
    for attempt in range(5):
        resp = SESSION.get(url, params=params, headers=request_headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return None
        if resp.status_code in (403, 429):
            remaining = resp.headers.get("X-RateLimit-Remaining")
            reset = resp.headers.get("X-RateLimit-Reset")
            if remaining == "0" and reset:
                wait_seconds = max(0, int(reset) - int(time.time())) + 2
                log(f"Rate limited. Sleeping {wait_seconds}s...")
                time.sleep(min(wait_seconds, 900))
                continue
            time.sleep(2 ** attempt)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        return None
    return None


def date_bucket(iso_string):
    if not iso_string:
        return None
    return iso_string.split("T")[0]


def empty_day():
    return {"commits": 0, "prs": 0, "issues": 0, "releases": 0, "_authors": set()}


def list_owner_repos(owner):
    """Auto-discover repos for an owner, trying Org first then User.

    The two endpoints don't share a `type` vocabulary: /orgs/{owner}/repos
    accepts "public" (what we want — no private/internal repos leak in even
    if the token had access), but /users/{owner}/repos only documents
    "all" | "owner" | "member" (default "owner") and does not accept
    "public". Using "public" there previously risked a rejected/ignored
    param on the user fallback path.
    """
    org_params = {"type": "public", "per_page": PER_PAGE, "sort": "pushed"}
    repos, _quality = api_get(f"{API_ROOT}/orgs/{owner}/repos", org_params)
    if not repos:
        user_params = {"type": "owner", "per_page": PER_PAGE, "sort": "pushed"}
        repos, _quality = api_get(f"{API_ROOT}/users/{owner}/repos", user_params)
        # /users/.../repos with an unauthenticated or low-scope token already
        # only returns public repos, but filter defensively in case a PAT
        # with broader access is ever used to run this script.
        repos = [r for r in repos if not r.get("private")]
    return [r for r in repos if not r.get("archived") and not r.get("fork")]


def resolve_self_repo():
    """(owner, repo) for the dashboard's own GitHub repo, used for the idea
    board (issues live here, not on a tracked ecosystem repo). GitHub Actions
    sets GITHUB_REPOSITORY as "owner/repo" automatically; for a manual/local
    run (e.g. Termux) set SELF_REPO_OVERRIDE above. Returns None if neither
    is available — the idea board is skipped, not fabricated."""
    raw = SELF_REPO_OVERRIDE or os.environ.get("GITHUB_REPOSITORY")
    if not raw or "/" not in raw:
        return None
    owner, repo = raw.split("/", 1)
    return (owner, repo) if owner and repo else None


def fetch_idea_issues(owner, repo):
    """Open issues labeled IDEAS_LABEL on (owner, repo) — the community's
    'someone should build this' board. Excludes pull requests (the /issues
    endpoint returns both; PRs carry a "pull_request" key issues don't).
    Returns normalized dicts, newest first, capped to IDEAS_MAX_ITEMS."""
    raw_issues, _quality = api_get(
        f"{API_ROOT}/repos/{owner}/{repo}/issues",
        {"labels": IDEAS_LABEL, "state": "open", "per_page": PER_PAGE,
         "sort": "created", "direction": "desc"},
    )
    ideas = []
    for issue in raw_issues:
        if "pull_request" in issue:
            continue
        author = issue.get("user") or {}
        body = (issue.get("body") or "").strip()
        # Plain-text excerpt only — the front end escapes and shows this as
        # text, never renders it as markdown/HTML, so no sanitization beyond
        # length-capping is needed here.
        excerpt = " ".join(body.split())[:240]
        reactions = issue.get("reactions") or {}
        ideas.append({
            "id": issue.get("id"),
            "number": issue.get("number"),
            "title": issue.get("title", ""),
            "htmlUrl": issue.get("html_url"),
            "author": {
                "login": author.get("login"),
                "avatarUrl": author.get("avatar_url"),
                "htmlUrl": author.get("html_url"),
            },
            "createdAt": issue.get("created_at"),
            "updatedAt": issue.get("updated_at"),
            "commentsCount": issue.get("comments", 0),
            "thumbsUp": reactions.get("+1", 0),
            "labels": [
                label.get("name") if isinstance(label, dict) else label
                for label in (issue.get("labels") or [])
            ],
            "bodyExcerpt": excerpt,
        })
    ideas.sort(key=lambda item: (item["thumbsUp"], item["createdAt"] or ""), reverse=True)
    return ideas[:IDEAS_MAX_ITEMS]


def map_org_type(raw):
    r = raw.strip().lower()
    if r.startswith("official"):
        return "official"
    if r.startswith("company-affiliated"):
        return "company-affiliated"
    if r.startswith("community org"):
        return "community org"
    if r.startswith("uncertain"):
        return "uncertain"
    return "independent"


def load_extra_targets():
    """Repos from the registry CSV that should be ingested individually even
    if their owner isn't in ORGS. Opt-out, not opt-in: every row with a
    github.com/<owner>/<repo> URL is a live target unless org_type is in
    EXCLUDED_ORG_TYPES or its verified column is explicitly a "no"-like
    value. Returns [(owner, repo, reason), ...]. Skips non-github.com URLs
    and bare org/user rows (no repo path)."""
    targets = []
    if not os.path.exists(CSV_PATH):
        log(f"Registry CSV not found at {CSV_PATH}, skipping extra targets.")
        return targets

    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            org_type = map_org_type(row.get("org_type", ""))
            verified_raw = (row.get("verified") or "").strip().lower()
            verified_yes = verified_raw in ("yes", "true", "1", "y")
            verified_no = verified_raw in ("no", "false", "0", "n")

            if org_type in EXCLUDED_ORG_TYPES or verified_no:
                continue

            if org_type in EXTRA_ORG_TYPE_PREFIXES:
                reason = f"org_type:{org_type}"
            elif verified_yes:
                reason = "verified"
            else:
                reason = "registry_default"

            url = row.get("url", "").strip()
            parsed = urlparse(url)
            if parsed.netloc != "github.com":
                continue
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) < 2:
                continue
            targets.append((parts[0], parts[1], reason))

    # de-duplicate while preserving order (first reason wins)
    seen = set()
    unique_targets = []
    for owner, repo, reason in targets:
        key = (owner.lower(), repo.lower())
        if key in seen:
            continue
        seen.add(key)
        unique_targets.append((owner, repo, reason))
    return unique_targets


def _page_fully_before_cutoff(page_items, since_dt, date_field):
    """True once every item on this page is older than since_dt. Used as an
    api_get stop_when for endpoints (like /pulls) that don't support a
    `since` query param — safe only when the endpoint is sorted newest-first
    by a field that is >= the field we actually care about (e.g. sorting
    PRs by `updated_at` desc, where updated_at >= merged_at always, since a
    merge itself counts as an update). The already-fetched page is still
    kept in results; this only stops requesting further, guaranteed-older
    pages."""
    if not page_items:
        return True
    last_item = page_items[-1]
    last_value = last_item.get(date_field)
    if not last_value:
        return False
    try:
        return datetime.fromisoformat(last_value.replace("Z", "+00:00")) < since_dt
    except ValueError:
        return False


def ingest_repo_activity(owner, repo_name, since_dt):
    full = f"{owner}/{repo_name}"
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    days = defaultdict(empty_day)
    # login -> {commits, avatarUrl, htmlUrl, firstCommitAt, lastCommitAt}.
    # Only commits with a real linked GitHub account (commit.author.login)
    # are counted here — commits from an email not linked to a GitHub
    # account have no login to attribute to, so they're excluded from
    # contributor identity entirely rather than guessed at from the
    # free-text commit author name.
    contributors = {}

    commits, commits_quality = api_get(
        f"{API_ROOT}/repos/{full}/commits",
        {"since": since_iso, "per_page": PER_PAGE},
    )
    for commit in commits:
        author_date = (commit.get("commit", {}).get("author", {}) or {}).get("date")
        bucket = date_bucket(author_date)
        if not bucket:
            continue
        author = commit.get("author") or {}
        login = author.get("login")
        fallback_name = (commit.get("commit", {}).get("author", {}) or {}).get("name", "unknown")
        days[bucket]["commits"] += 1
        days[bucket]["_authors"].add(login or fallback_name)

        if login:
            entry = contributors.setdefault(login, {
                "login": login,
                "avatarUrl": author.get("avatar_url"),
                "htmlUrl": author.get("html_url"),
                "commits": 0,
                "firstCommitAt": author_date,
                "lastCommitAt": author_date,
                "activeDates": set(),
            })
            entry["commits"] += 1
            entry["activeDates"].add(bucket)
            if author_date and (not entry["lastCommitAt"] or author_date > entry["lastCommitAt"]):
                entry["lastCommitAt"] = author_date
            if author_date and (not entry["firstCommitAt"] or author_date < entry["firstCommitAt"]):
                entry["firstCommitAt"] = author_date

    pulls, prs_quality = api_get(
        f"{API_ROOT}/repos/{full}/pulls",
        {"state": "closed", "per_page": PER_PAGE, "sort": "updated", "direction": "desc"},
        # /pulls has no `since` filter, so without this every run would
        # re-fetch a repo's *entire* closed-PR history just to keep the last
        # LOOKBACK_DAYS of it (previously the case — see audit). Sorted
        # updated-desc, updated_at >= merged_at always, so once a page is
        # fully older than since_dt every later page is guaranteed to be
        # too — safe to stop there without missing anything in range.
        stop_when=lambda page: _page_fully_before_cutoff(page, since_dt, "updated_at"),
    )
    for pr in pulls:
        merged_at = pr.get("merged_at")
        if not merged_at:
            continue
        if datetime.fromisoformat(merged_at.replace("Z", "+00:00")) < since_dt:
            continue
        bucket = date_bucket(merged_at)
        if bucket:
            days[bucket]["prs"] += 1

    issues, issues_quality = api_get(
        f"{API_ROOT}/repos/{full}/issues",
        {"state": "all", "since": since_iso, "per_page": PER_PAGE},
    )
    for issue in issues:
        if "pull_request" in issue:
            continue
        bucket = date_bucket(issue.get("created_at"))
        if bucket:
            days[bucket]["issues"] += 1

    releases, releases_quality = api_get(f"{API_ROOT}/repos/{full}/releases", {"per_page": PER_PAGE})
    release_items = []
    for release in releases:
        published = release.get("published_at")
        if not published:
            continue
        if datetime.fromisoformat(published.replace("Z", "+00:00")) < since_dt:
            continue
        bucket = date_bucket(published)
        if bucket:
            days[bucket]["releases"] += 1
        release_items.append({
            "repo": full,
            "name": release.get("name") or release.get("tag_name") or "release",
            "tag": release.get("tag_name"),
            "publishedAt": published,
            "url": release.get("html_url"),
            "prerelease": bool(release.get("prerelease")),
        })

    data_quality = {
        "commits": {"status": commits_quality, "lookbackDays": LOOKBACK_DAYS},
        "prs": {"status": prs_quality, "lookbackDays": LOOKBACK_DAYS},
        "issues": {"status": issues_quality, "lookbackDays": LOOKBACK_DAYS},
        "releases": {"status": releases_quality, "lookbackDays": LOOKBACK_DAYS},
    }

    return days, release_items, contributors, data_quality


def zero_filled_series(days_map, since_dt, until_dt):
    series = []
    cursor = since_dt.date()
    end = until_dt.date()
    while cursor <= end:
        key = cursor.isoformat()
        day = days_map.get(key)
        if day:
            series.append({
                "date": key,
                "commits": day["commits"],
                "prs": day["prs"],
                "issues": day["issues"],
                "releases": day["releases"],
                "activeDevs": len(day["_authors"]),
            })
        else:
            series.append({"date": key, "commits": 0, "prs": 0, "issues": 0, "releases": 0, "activeDevs": 0})
        cursor += timedelta(days=1)
    return series


def fetch_repo_snapshot(owner, repo_name):
    """Point-in-time repo metadata. GitHub's repo endpoint already returns
    far more than the activity dashboard was using — language/topics/
    license/timestamps below cost nothing extra since they're on the same
    response as stars/forks/archived, just previously discarded.

    The archived flag feeds effectiveRepoStatus() in app.js — a repo
    GitHub itself reports as archived shows that status regardless of how
    recently it was committed to, taking priority over commit-recency
    buckets (Active/Slowing/Stale)."""
    data = api_get_json(f"{API_ROOT}/repos/{owner}/{repo_name}")
    if not data:
        return None
    license_info = data.get("license") or {}
    return {
        "stars": data.get("stargazers_count", 0),
        "forks": data.get("forks_count", 0),
        "watchers": data.get("subscribers_count", data.get("watchers_count", 0)),
        "openIssues": data.get("open_issues_count", 0),
        "archived": bool(data.get("archived", False)),
        "primaryLanguage": data.get("language"),
        "topics": data.get("topics") or [],
        "license": license_info.get("spdx_id") if license_info.get("spdx_id") not in (None, "NOASSERTION") else None,
        "defaultBranch": data.get("default_branch"),
        "homepage": data.get("homepage") or None,
        "createdAt": data.get("created_at"),
        "pushedAt": data.get("pushed_at"),
    }


def fetch_repo_languages(owner, repo_name):
    """Bytes of code per language (GET /repos/{o}/{r}/languages) — GitHub's
    own basis for the "languages" bar on a repo page. One extra call per
    repo per run; cheap relative to the commits/PRs/issues/releases calls
    already made for the same repo, and it's the only way to get the full
    breakdown rather than just the single dominant `language` field above.
    Returns {} (not None) on failure so callers can treat "no breakdown
    available" and "genuinely no code" the same way."""
    data = api_get_json(f"{API_ROOT}/repos/{owner}/{repo_name}/languages")
    return data if isinstance(data, dict) else {}


def compute_bus_factor(repo_contributors):
    """Minimum number of contributors whose combined commits reach at
    least 50% of a repo's identified commits — a much more informative
    signal than a fixed "one person did >=85%" threshold: a repo where
    the top 4 contributors are needed to cover half the history is
    meaningfully healthier than one where the top 1 alone does, even
    though neither would trip an 85% single-author check.

    Returns None if there are no identified commits to rank at all
    (repo_contributors empty) — never a modeled guess.
    """
    commit_counts = sorted(
        (c["commits"] for c in repo_contributors.values()), reverse=True
    )
    total = sum(commit_counts)
    if total <= 0:
        return None
    target = total / 2
    cumulative = 0
    for i, commits in enumerate(commit_counts, start=1):
        cumulative += commits
        if cumulative >= target:
            return i
    return len(commit_counts)


def backfill_star_history(owner, repo_name):
    """Best-effort real star history from GitHub's stargazers timestamp API
    (Accept: application/vnd.github.star+json gives a starred_at per
    stargazer). Bounded to STARGAZER_BACKFILL_MAX_PAGES pages to avoid
    blowing the rate limit on very popular repos — if a repo has more
    stargazers than that, this returns a partial (oldest-missing) history
    and the daily snapshot mechanism fills in the rest going forward.

    Returns (history, quality) where quality is "complete" if every
    stargazer was walked (the API's own pagination ended the loop) or
    "partial" if STARGAZER_BACKFILL_MAX_PAGES was hit while GitHub still
    had more pages to give — i.e. the earliest portion of history is
    missing and the curve should not be presented as the repo's full
    star history.
    """
    full = f"{owner}/{repo_name}"
    url = f"{API_ROOT}/repos/{full}/stargazers"
    headers = {"Accept": "application/vnd.github.star+json"}
    params = {"per_page": PER_PAGE}

    daily_counts = defaultdict(int)
    page_count = 0
    next_url = url
    next_params = params
    incomplete = False

    while next_url and page_count < STARGAZER_BACKFILL_MAX_PAGES:
        resp = SESSION.get(next_url, params=next_params, headers={**SESSION.headers, **headers}, timeout=REQUEST_TIMEOUT)
        page_count += 1
        if resp.status_code != 200:
            # Stopped short due to a transient/rate-limit error, not because
            # we reached the end of the stargazer list — the walk is
            # unverified, so don't label it complete.
            incomplete = True
            break
        page = resp.json()
        if not isinstance(page, list) or not page:
            next_url = None
            break
        for entry in page:
            starred_at = entry.get("starred_at") if isinstance(entry, dict) else None
            bucket = date_bucket(starred_at)
            if bucket:
                daily_counts[bucket] += 1

        next_url = None
        next_params = None
        link_header = resp.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                next_url = part[part.find("<") + 1: part.find(">")]
                break

    # Incomplete if we bailed on an error mid-walk, or hit the page cap
    # while GitHub still had a next page queued up — either way the
    # earliest stargazers were never fetched.
    quality = "partial" if (incomplete or (page_count >= STARGAZER_BACKFILL_MAX_PAGES and next_url)) else "complete"

    if not daily_counts:
        return [], quality

    # Convert daily new-star counts into a cumulative history.
    ordered_days = sorted(daily_counts.keys())
    history = []
    cumulative = 0
    for day in ordered_days:
        cumulative += daily_counts[day]
        history.append({"date": day, "stars": cumulative, "forks": None, "watchers": None, "openIssues": None})
    return history, quality


def load_existing_star_history(out_path):
    if not os.path.exists(out_path):
        return [], None
    try:
        with open(out_path, encoding="utf-8") as fh:
            existing = json.load(fh)
        return existing.get("starHistory", []), existing.get("starHistoryQuality")
    except (json.JSONDecodeError, OSError):
        return [], None


def append_star_point(history, date_key, snapshot):
    history = [point for point in history if point.get("date") != date_key]
    history.append({
        "date": date_key,
        "stars": snapshot["stars"],
        "forks": snapshot["forks"],
        "watchers": snapshot["watchers"],
        "openIssues": snapshot["openIssues"],
    })
    history.sort(key=lambda point: point["date"])
    return history[-STAR_HISTORY_MAX_POINTS:]


def write_release_feed(release_items, generated_at):
    os.makedirs(FEED_DIR, exist_ok=True)

    cutoff = generated_at - timedelta(days=FEED_LOOKBACK_DAYS)
    recent = [
        item for item in release_items
        if item.get("publishedAt") and datetime.fromisoformat(item["publishedAt"].replace("Z", "+00:00")) >= cutoff
    ]
    recent.sort(key=lambda item: item["publishedAt"], reverse=True)
    recent = recent[:FEED_MAX_ITEMS]

    with open(os.path.join(FEED_DIR, "releases.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "generatedAt": generated_at.isoformat(),
            "lookbackDays": FEED_LOOKBACK_DAYS,
            "releases": recent,
        }, fh, indent=2)

    def esc(text):
        return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    items_xml = []
    for item in recent:
        title = f"{item['repo']} — {item['name']}"
        link = item.get("url") or f"https://github.com/{item['repo']}"
        try:
            pub_dt = datetime.fromisoformat(item["publishedAt"].replace("Z", "+00:00"))
            pub_date = pub_dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except ValueError:
            pub_date = generated_at.strftime("%a, %d %b %Y %H:%M:%S +0000")
        items_xml.append(
            "    <item>\n"
            f"      <title>{esc(title)}</title>\n"
            f"      <link>{esc(link)}</link>\n"
            f"      <guid isPermaLink=\"true\">{esc(link)}</guid>\n"
            f"      <pubDate>{pub_date}</pubDate>\n"
            f"      <description>{esc(item.get('tag') or '')}</description>\n"
            "    </item>"
        )

    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        "    <title>Kaspa GitHub Ecosystem — Releases</title>\n"
        "    <description>Recent releases across tracked Kaspa ecosystem repositories.</description>\n"
        "    <link>https://github.com/kaspanet</link>\n"
        f"    <lastBuildDate>{generated_at.strftime('%a, %d %b %Y %H:%M:%S +0000')}</lastBuildDate>\n"
        + ("\n".join(items_xml) + "\n" if items_xml else "")
        + "  </channel>\n"
        "</rss>\n"
    )

    with open(os.path.join(FEED_DIR, "releases.xml"), "w", encoding="utf-8") as fh:
        fh.write(rss)

    return len(recent)


def accumulate_summary_day(summary_days, date_key, day_totals, day_authors):
    """Merge one repo's one-day totals into the ecosystem-wide summary
    bucket for that date. day_authors is the set of distinct authors
    active on that repo on that day (from days_map, before
    zero_filled_series collapses it down to a bare count) — unioned into
    the summary bucket so a developer active on two repos the same day
    counts once ecosystem-wide, not twice."""
    bucket = summary_days[date_key]
    bucket["commits"] += day_totals["commits"]
    bucket["prs"] += day_totals["prs"]
    bucket["issues"] += day_totals["issues"]
    bucket["releases"] += day_totals["releases"]
    bucket["_authors"].update(day_authors)


def finalize_summary_series(summary_days):
    """summary_days (date -> accumulated bucket) -> the sorted list of
    per-day records written to _summary.json. activeDevs is the true
    distinct-author count for that date across every tracked repo."""
    summary_series = []
    for date_key in sorted(summary_days.keys()):
        bucket = summary_days[date_key]
        summary_series.append({
            "date": date_key,
            "commits": bucket["commits"],
            "prs": bucket["prs"],
            "issues": bucket["issues"],
            "releases": bucket["releases"],
            "activeDevs": len(bucket["_authors"]),
        })
    return summary_series


def main():
    if not TOKEN:
        log("Warning: no GITHUB_TOKEN/GH_TOKEN set. Proceeding unauthenticated (low rate limit).")

    now = datetime.now(timezone.utc)
    today_key = now.date().isoformat()
    since_dt = now - timedelta(days=LOOKBACK_DAYS)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Build the full target list: (owner, repo, reason) tuples from full
    # org/user discovery, plus explicit registry-driven extras not already
    # covered (company/community org_type, or individually verified rows).
    targets = []
    covered = set()

    for owner in ORGS:
        log(f"Discovering repos for owner: {owner}")
        try:
            repos = list_owner_repos(owner)
        except Exception as exc:  # noqa: BLE001
            log(f"Failed to list repos for {owner}: {exc}")
            repos = []
        for repo in repos:
            key = (owner.lower(), repo["name"].lower())
            if key in covered:
                continue
            covered.add(key)
            targets.append((owner, repo["name"], "org_discovery"))

    extra_targets = load_extra_targets()
    reason_counts = defaultdict(int)
    for owner, repo_name, reason in extra_targets:
        key = (owner.lower(), repo_name.lower())
        if key in covered:
            continue
        covered.add(key)
        targets.append((owner, repo_name, reason))
        reason_counts[reason] += 1
    verified_count = reason_counts["verified"]

    log(f"Total tracked repos this run: {len(targets)} "
        f"({len(targets) - len(extra_targets)} via full org/user discovery, "
        f"{len(extra_targets)} registry targets: "
        + ", ".join(f"{count} {reason}" for reason, count in sorted(reason_counts.items()))
        + ")")

    repo_index = {}
    stars_summary = {}
    all_release_items = []
    summary_days = defaultdict(lambda: {"commits": 0, "prs": 0, "issues": 0, "releases": 0, "_authors": set()})
    # login -> aggregated contributor record across every tracked repo.
    contributors_agg = {}
    ingested_count = 0
    stars_captured_count = 0
    # Ecosystem-wide rollup of which repos hit a partial fetch this run,
    # per datatype — lets _summary.json (and data/api/index.json) surface
    # "N repos have incomplete PR data this run" without a consumer having
    # to fetch and inspect all ~90 individual repo files.
    partial_data_repos = {"commits": [], "prs": [], "issues": [], "releases": []}
    # Ecosystem-wide rollup of what's already computed above — total bytes
    # per language and repo counts per topic — so the frontend can render a
    # "tech stack across the ecosystem" view from one small file instead of
    # fetching all ~90 per-repo files just to sum up their languages/topics.
    languages_agg = defaultdict(int)
    topics_agg = defaultdict(int)
    star_backfills_performed = 0
    failed = []

    for owner, repo_name, _reason in targets:
        full = f"{owner}/{repo_name}"
        log(f"Ingesting {full}...")

        owner_dir = os.path.join(OUTPUT_DIR, owner)
        os.makedirs(owner_dir, exist_ok=True)
        out_path = os.path.join(owner_dir, f"{repo_name}.json")

        try:
            days_map, release_items, repo_contributors, data_quality = ingest_repo_activity(owner, repo_name, since_dt)
            series = zero_filled_series(days_map, since_dt, now)
        except Exception as exc:  # noqa: BLE001
            log(f"  activity fetch failed: {exc}")
            failed.append(full)
            continue

        for datatype, info in data_quality.items():
            if info["status"] == "partial":
                partial_data_repos[datatype].append(full)

        all_release_items.extend(release_items)

        for login, info in repo_contributors.items():
            agg = contributors_agg.setdefault(login, {
                "login": login,
                "avatarUrl": info["avatarUrl"],
                "htmlUrl": info["htmlUrl"],
                "commits": 0,
                "repos": {},
                "firstCommitAt": None,
                "firstCommitRepo": None,
                "lastCommitAt": None,
                "activeDates": set(),
            })
            # A contributor's avatar/profile URL can't change mid-run, but keep
            # the most recently seen non-null value just in case an earlier
            # repo's API response had a gap.
            agg["avatarUrl"] = agg["avatarUrl"] or info["avatarUrl"]
            agg["htmlUrl"] = agg["htmlUrl"] or info["htmlUrl"]
            agg["commits"] += info["commits"]
            agg["repos"][full] = agg["repos"].get(full, 0) + info["commits"]
            agg["activeDates"] |= info["activeDates"]
            if info["lastCommitAt"] and (not agg["lastCommitAt"] or info["lastCommitAt"] > agg["lastCommitAt"]):
                agg["lastCommitAt"] = info["lastCommitAt"]
            # Earliest commit across every tracked repo, within the ingestion's
            # LOOKBACK_DAYS window. This is what "new contributor" detection is
            # built on (see NEW_CONTRIBUTOR_WINDOW_DAYS in app.js): if someone's
            # earliest commit we've ever seen for them is recent, we haven't
            # observed any commit from them in the prior LOOKBACK_DAYS, which is
            # a reasonable proxy for "new to the project" without needing a full
            # unbounded history lookup per author.
            if info["firstCommitAt"] and (not agg["firstCommitAt"] or info["firstCommitAt"] < agg["firstCommitAt"]):
                agg["firstCommitAt"] = info["firstCommitAt"]
                agg["firstCommitRepo"] = full

        star_history, star_history_quality = load_existing_star_history(out_path)
        if not star_history:
            try:
                backfilled, star_history_quality = backfill_star_history(owner, repo_name)
                if backfilled:
                    star_history = backfilled
                    star_backfills_performed += 1
            except Exception as exc:  # noqa: BLE001
                log(f"  star history backfill failed: {exc}")

        snapshot = None
        try:
            snapshot = fetch_repo_snapshot(owner, repo_name)
        except Exception as exc:  # noqa: BLE001
            log(f"  star snapshot failed: {exc}")

        languages = {}
        try:
            languages = fetch_repo_languages(owner, repo_name)
        except Exception as exc:  # noqa: BLE001
            log(f"  language breakdown failed: {exc}")

        if snapshot:
            star_history = append_star_point(star_history, today_key, snapshot)
            stars_captured_count += 1
            stars_summary[full] = {**snapshot, "capturedAt": now.isoformat()}
            for topic in snapshot.get("topics", []):
                topics_agg[topic] += 1

        for lang, byte_count in languages.items():
            languages_agg[lang] += byte_count

        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump({
                "repo": full,
                "generatedAt": now.isoformat(),
                "lookbackDays": LOOKBACK_DAYS,
                "series": series,
                # Repo metadata beyond activity counts — language/topics/
                # license/timestamps come free on the same API response as
                # stars/forks/archived (fetch_repo_snapshot); languages is
                # one extra call for the full per-language byte breakdown.
                "primaryLanguage": (snapshot or {}).get("primaryLanguage"),
                "languages": languages,
                "topics": (snapshot or {}).get("topics", []),
                "license": (snapshot or {}).get("license"),
                "defaultBranch": (snapshot or {}).get("defaultBranch"),
                "homepage": (snapshot or {}).get("homepage"),
                "createdAt": (snapshot or {}).get("createdAt"),
                "pushedAt": (snapshot or {}).get("pushedAt"),
                # Per-datatype fetch completeness for this run's lookback
                # window — "complete" means pagination ran to a natural end
                # (or GitHub's own stop condition), "partial" means retries
                # were exhausted mid-walk and this datatype covers less than
                # lookbackDays worth of history. Consumers (the frontend,
                # or anyone building on data/api/index.json) should treat a
                # "Live" badge next to a partial datatype as "live, but
                # incomplete" rather than "we have everything".
                "dataQuality": data_quality,
                "starHistory": star_history,
                # "complete" = every stargazer was walked via the timestamped
                # stargazers API; "partial" = STARGAZER_BACKFILL_MAX_PAGES was
                # hit (or the walk errored) before reaching the repo's
                # earliest stargazers, so the start of this curve is missing
                # — don't render it as if it were the full history. null
                # means backfill was never attempted (e.g. history already
                # existed from a prior run before this field was added).
                "starHistoryQuality": star_history_quality,
                # From fetch_repo_snapshot's GitHub response; null if the
                # snapshot call itself failed (rate limit, transient error) —
                # distinct from false, which means GitHub confirmed it's NOT
                # archived. app.js's effectiveRepoStatus() only trusts a
                # literal true/false, never treats null as either.
                "archived": snapshot.get("archived") if snapshot else None,
                # Per-repo contributor concentration, uncapped (unlike the
                # org-wide top-100 in _contributors.json, which could miss a
                # niche repo's dominant author if they're not otherwise very
                # active). identifiedCommits is the sum of commits with a
                # linked GitHub account — the correct denominator for a
                # "bus factor" share, since some commits (bots, old
                # unlinked-email commits) have no author to attribute.
                # See repoBusFactor() in app.js.
                "identifiedCommits": sum(c["commits"] for c in repo_contributors.values()),
                # Real bus-factor: the minimum number of contributors whose
                # combined commits reach 50% of identifiedCommits — see
                # compute_bus_factor(). null if identifiedCommits is 0.
                # Superseded the old "does contributor #1 alone have >=85%"
                # heuristic, which only ever detected the single-maintainer
                # case and said nothing about repos with 2-3 core devs.
                "busFactor": compute_bus_factor(repo_contributors),
                "topContributors": [
                    {"login": c["login"], "commits": c["commits"]}
                    for c in sorted(repo_contributors.values(), key=lambda c: c["commits"], reverse=True)[:5]
                ],
            }, fh, indent=2)

        repo_index[full] = f"activity/{owner}/{repo_name}.json"
        ingested_count += 1

        for day in series:
            accumulate_summary_day(
                summary_days,
                day["date"],
                day,
                days_map.get(day["date"], {}).get("_authors", set()),
            )

    feed_count = write_release_feed(all_release_items, now)

    summary_series = finalize_summary_series(summary_days)

    with open(os.path.join(OUTPUT_DIR, "_summary.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "generatedAt": now.isoformat(),
            "lookbackDays": LOOKBACK_DAYS,
            "orgs": ORGS,
            "repoCount": ingested_count,
            "series": summary_series,
            "partialDataRepos": partial_data_repos,
            "languages": dict(sorted(languages_agg.items(), key=lambda kv: kv[1], reverse=True)),
            "topics": dict(sorted(topics_agg.items(), key=lambda kv: kv[1], reverse=True)),
        }, fh, indent=2)

    with open(os.path.join(OUTPUT_DIR, "_repos.json"), "w", encoding="utf-8") as fh:
        json.dump(repo_index, fh, indent=2)

    with open(os.path.join(OUTPUT_DIR, "_stars.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "generatedAt": now.isoformat(),
            "repos": stars_summary,
        }, fh, indent=2)

    # Sorted by commit count, capped to keep the file small — this is a
    # "top contributors" list, not a full org member directory.
    CONTRIBUTOR_CAP = 100
    contributors_out = sorted(
        contributors_agg.values(), key=lambda c: c["commits"], reverse=True
    )[:CONTRIBUTOR_CAP]
    for entry in contributors_out:
        entry["repoCount"] = len(entry["repos"])
        entry["repos"] = [
            {"repo": repo, "commits": count}
            for repo, count in sorted(entry["repos"].items(), key=lambda kv: kv[1], reverse=True)
        ]
        # Sorted list of every distinct day (UTC, "YYYY-MM-DD") this person
        # committed to any tracked repo, within LOOKBACK_DAYS. This is what
        # lets the front end compute a real "distinct contributors active in
        # this period vs. the one before it" delta (period membership is a
        # date-range containment check against this list) instead of either
        # fabricating one or leaving it blank.
        entry["activeDates"] = sorted(entry["activeDates"])

    with open(os.path.join(OUTPUT_DIR, "_contributors.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "generatedAt": now.isoformat(),
            "lookbackDays": LOOKBACK_DAYS,
            "note": (
                "Commit counts are cumulative over lookbackDays, not sliced by "
                "the dashboard's 7/30/90/365-day range selector. Only commits "
                "linked to a real GitHub account are included; commits from an "
                "email with no linked account are excluded, not attributed by "
                "guesswork. firstCommitAt is the earliest commit seen for that "
                "login within lookbackDays, used as a 'new contributor' signal "
                "on the front end — not a claim about their GitHub history "
                "before this window. activeDates is every distinct day (UTC) "
                "they committed to any tracked repo, used to compute real "
                "period-over-period distinct-contributor deltas."
            ),
            "contributors": contributors_out,
        }, fh, indent=2)

    # Idea board — open issues labeled IDEAS_LABEL on the dashboard's own
    # repo, not a tracked ecosystem repo. Skipped (not fabricated) if the
    # repo can't be resolved, e.g. a local run without GITHUB_REPOSITORY set.
    self_repo = resolve_self_repo()
    ideas_out = []
    if self_repo:
        self_owner, self_name = self_repo
        try:
            ideas_out = fetch_idea_issues(self_owner, self_name)
        except Exception as exc:
            log(f"Idea board fetch failed for {self_owner}/{self_name}: {exc}")
        with open(IDEAS_PATH, "w", encoding="utf-8") as fh:
            json.dump({
                "generatedAt": now.isoformat(),
                "repo": f"{self_owner}/{self_name}",
                "label": IDEAS_LABEL,
                "note": (
                    "Open issues labeled '{}' on this repo, sorted by 👍 reaction "
                    "count then recency. bodyExcerpt is plain text, truncated to "
                    "240 characters — render it as text, never as HTML/markdown."
                ).format(IDEAS_LABEL),
                "ideas": ideas_out,
            }, fh, indent=2)
    else:
        log("Could not resolve the dashboard's own repo (no GITHUB_REPOSITORY "
            "and no SELF_REPO_OVERRIDE) — skipping idea board, not writing ideas.json.")

    os.makedirs(os.path.dirname(MANIFEST_PATH), exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as fh:
        json.dump({
            "schemaVersion": MANIFEST_SCHEMA_VERSION,
            "generatedAt": now.isoformat(),
            "generator": "kasgit ingest_github_activity.py",
            "sourceRepo": f"{self_repo[0]}/{self_repo[1]}" if self_repo else None,
            "license": (
                "This manifest and the resources it lists are derived from the public GitHub API "
                "(commit/PR/issue/release metadata, star counts, public repo listings) and from this "
                "project's own hand-maintained registry CSV. Treat the GitHub-derived fields as a mirror "
                "of that public data, subject to GitHub's Terms of Service "
                "(https://docs.github.com/site-policy/github-terms/github-terms-of-service). This "
                "project's own code and registry curation are under the LICENSE file in the repo root."
            ),
            "notes": [
                "All paths are relative to this site's root (e.g. https://<owner>.github.io/<repo>/<path>).",
                "Static files — no auth, no API key, no rate limit imposed by this project. Normal HTTP "
                "caching applies; see _meta.json's generatedAt or this file's generatedAt for freshness.",
                "Updated once daily by the ingest-activity.yml GitHub Actions workflow.",
                "schemaVersion only bumps on a breaking change (a field renamed, removed, or changed "
                "meaning) to a resource already listed here — a new optional field or a new resource does "
                "not bump it, so pin to a schemaVersion rather than an exact field list if you build "
                "something long-lived on this.",
            ],
            "resources": [
                {
                    "id": "repoIndex",
                    "path": "data/activity/_repos.json",
                    "format": "json",
                    "description": (
                        "Map of every live-ingested repo (\"owner/repo\") to the relative path of its own "
                        "activity file. Start here to discover what's actually being ingested this run."
                    ),
                },
                {
                    "id": "repoActivity",
                    "path": "data/activity/{owner}/{repo}.json",
                    "format": "json",
                    "description": (
                        "Per-repo daily commit/PR/issue/release series, star history "
                        "(with starHistoryQuality: \"complete\"/\"partial\"/null — see Methodology), "
                        "dataQuality (per-datatype fetch completeness for this run's lookback window), "
                        "identifiedCommits/topContributors/busFactor (minimum contributors covering "
                        "50% of identified commits — see Methodology), primaryLanguage/languages "
                        "(byte breakdown)/topics/license/defaultBranch/homepage/createdAt/pushedAt, "
                        "and the archived flag (used for effective status — see Methodology). One "
                        "file per repo listed in repoIndex — repos outside live ingestion scope have "
                        "no file here."
                    ),
                },
                {
                    "id": "orgSummary",
                    "path": "data/activity/_summary.json",
                    "format": "json",
                    "description": (
                        "Org-wide daily activity series pre-aggregated across every ingested repo "
                        "(the dashboard's own frontend uses this directly for its unfiltered Overview "
                        "chart — see Methodology), plus partialDataRepos (which repos hit a partial "
                        "fetch this run, per datatype) and languages/topics (ecosystem-wide rollups of "
                        "the same fields in each repoActivity file)."
                    ),
                },
                {
                    "id": "orgStars",
                    "path": "data/activity/_stars.json",
                    "format": "json",
                    "description": "Current star count per ingested repo, as of this run.",
                },
                {
                    "id": "contributors",
                    "path": "data/activity/_contributors.json",
                    "format": "json",
                    "description": (
                        "Org-wide contributor list (commits, first/last commit date, every distinct active "
                        "day, repos touched), capped "
                        "to the top 100 by total commits. For a specific repo's own contributor breakdown "
                        "(uncapped), use that repo's file via repoIndex instead."
                    ),
                },
                {
                    "id": "ideas",
                    "path": "data/activity/ideas.json",
                    "format": "json",
                    "description": f"Open issues labeled '{IDEAS_LABEL}' on {self_repo[0]}/{self_repo[1]}" if self_repo
                        else f"Open issues labeled '{IDEAS_LABEL}' on this project's own repo — the community idea board.",
                },
                {
                    "id": "releaseFeedJson",
                    "path": "data/feed/releases.json",
                    "format": "json",
                    "description": "Combined release feed across every ingested repo, newest first.",
                },
                {
                    "id": "releaseFeedRss",
                    "path": "data/feed/releases.xml",
                    "format": "rss",
                    "description": "The same release feed as standard RSS 2.0 — subscribe directly in any feed reader.",
                },
                {
                    "id": "registryCsv",
                    "path": "data/kaspa_github_ecosystem_inventory.csv",
                    "format": "csv",
                    "description": (
                        "The hand-maintained source registry (project name, category, org_type/"
                        "org_type_note, status, confidence/confidence_note, verified flag) that "
                        "ingestion targets are computed from — the ground truth for what's tracked "
                        "at all, live or modeled. org_type_note/confidence_note hold the free-text "
                        "explanation split out from the classification value itself (see "
                        "scripts/migrate_split_notes.py) — the classification columns are meant to "
                        "stay short, enumerable values."
                    ),
                },
                {
                    "id": "runMeta",
                    "path": "data/activity/_meta.json",
                    "format": "json",
                    "description": "This run's own stats — repo/contributor/idea counts, failures, timing. Useful for a freshness/health check before trusting the rest.",
                },
            ],
        }, fh, indent=2)

    with open(os.path.join(OUTPUT_DIR, "_meta.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "generatedAt": now.isoformat(),
            "orgs": ORGS,
            "extraTargetOrgTypes": list(EXTRA_ORG_TYPE_PREFIXES),
            "verifiedTargetCount": verified_count,
            "repoCount": ingested_count,
            "starsCaptured": stars_captured_count,
            "starHistoryBackfills": star_backfills_performed,
            "releaseFeedItems": feed_count,
            "contributorsCaptured": len(contributors_out),
            "ideasCaptured": len(ideas_out),
            "failed": failed,
            "lookbackDays": LOOKBACK_DAYS,
            "status": "ok" if ingested_count else "no-data",
        }, fh, indent=2)

    log(f"Done. Ingested {ingested_count} repos ({stars_captured_count} star snapshots, "
        f"{star_backfills_performed} star-history backfills, {feed_count} release feed items, "
        f"{len(contributors_out)} contributors captured, {len(ideas_out)} idea-board issues), "
        f"{len(failed)} failed.")


if __name__ == "__main__":
    main()
