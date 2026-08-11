#!/usr/bin/env python3
"""
Scheduled GitHub activity ingestion for the Kaspa GitHub Ecosystem Dashboard.

Fetches commits, merged pull requests, issues, releases, and point-in-time
star/fork/watcher counts for tracked repositories, and writes normalized
JSON under data/activity/. The static front-end (assets/js/app.js) reads
these files at runtime and falls back to modeled placeholder data for any
repo that hasn't been ingested yet.

Tracked repos come from three sources:
  1. ORGS — full auto-discovery of every non-archived, non-fork repo owned
     by these GitHub accounts (works for both orgs and users).
  2. The registry CSV (data/kaspa_github_ecosystem_inventory.csv) — any row
     whose org_type is "company-affiliated" or "community org" is ingested
     individually, even if its owner isn't in ORGS.
  3. Any registry row with verified=yes, regardless of org_type — this is
     how independent-contributor repos graduate into live ingestion once
     someone has actually checked them (edit the CSV's verified/verified_at
     columns, no code change needed).

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
# rest of that owner's unrelated repos.
EXTRA_ORG_TYPE_PREFIXES = ("company-affiliated", "community org")

LOOKBACK_DAYS = 400  # covers the dashboard's 1Y range with margin
STAR_HISTORY_MAX_POINTS = 400
STARGAZER_BACKFILL_MAX_PAGES = 5   # 5 * 100 = up to 500 stargazers backfilled
FEED_MAX_ITEMS = 150
FEED_LOOKBACK_DAYS = 180

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(BASE_DIR, "data", "activity")
FEED_DIR = os.path.join(BASE_DIR, "data", "feed")
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
                return results
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
        else:
            log(f"Giving up on {next_url} after retries")
            return results

        if resp.status_code != 200:
            return results

        page_data = resp.json()
        if isinstance(page_data, list):
            results.extend(page_data)
            if stop_when and stop_when(page_data):
                return results
        else:
            return page_data

        next_url = None
        next_params = None
        link_header = resp.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                next_url = part[part.find("<") + 1: part.find(">")]
                break

    return results


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
    repos = api_get(f"{API_ROOT}/orgs/{owner}/repos", org_params)
    if not repos:
        user_params = {"type": "owner", "per_page": PER_PAGE, "sort": "pushed"}
        repos = api_get(f"{API_ROOT}/users/{owner}/repos", user_params)
        # /users/.../repos with an unauthenticated or low-scope token already
        # only returns public repos, but filter defensively in case a PAT
        # with broader access is ever used to run this script.
        repos = [r for r in repos if not r.get("private")]
    return [r for r in repos if not r.get("archived") and not r.get("fork")]


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
    if their owner isn't in ORGS: rows whose org_type matches
    EXTRA_ORG_TYPE_PREFIXES, or any row explicitly marked verified=yes
    (independent-contributor repos someone has actually checked). Returns
    [(owner, repo, reason), ...]. Skips non-github.com URLs and bare
    org/user rows (no repo path)."""
    targets = []
    if not os.path.exists(CSV_PATH):
        log(f"Registry CSV not found at {CSV_PATH}, skipping extra targets.")
        return targets

    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            org_type = map_org_type(row.get("org_type", ""))
            verified = (row.get("verified") or "").strip().lower() in ("yes", "true", "1", "y")

            if org_type in EXTRA_ORG_TYPE_PREFIXES:
                reason = f"org_type:{org_type}"
            elif verified:
                reason = "verified"
            else:
                continue

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
    # login -> {commits, avatarUrl, htmlUrl, lastCommitAt}. Only commits with a
    # real linked GitHub account (commit.author.login) are counted here —
    # commits from an email not linked to a GitHub account have no login to
    # attribute to, so they're excluded from contributor identity entirely
    # rather than guessed at from the free-text commit author name.
    contributors = {}

    commits = api_get(
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
                "lastCommitAt": author_date,
            })
            entry["commits"] += 1
            if author_date and (not entry["lastCommitAt"] or author_date > entry["lastCommitAt"]):
                entry["lastCommitAt"] = author_date

    pulls = api_get(
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

    issues = api_get(
        f"{API_ROOT}/repos/{full}/issues",
        {"state": "all", "since": since_iso, "per_page": PER_PAGE},
    )
    for issue in issues:
        if "pull_request" in issue:
            continue
        bucket = date_bucket(issue.get("created_at"))
        if bucket:
            days[bucket]["issues"] += 1

    releases = api_get(f"{API_ROOT}/repos/{full}/releases", {"per_page": PER_PAGE})
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

    return days, release_items, contributors


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
    """Point-in-time repo metadata: stars, forks, watchers, open issues."""
    data = api_get_json(f"{API_ROOT}/repos/{owner}/{repo_name}")
    if not data:
        return None
    return {
        "stars": data.get("stargazers_count", 0),
        "forks": data.get("forks_count", 0),
        "watchers": data.get("subscribers_count", data.get("watchers_count", 0)),
        "openIssues": data.get("open_issues_count", 0),
    }


def backfill_star_history(owner, repo_name):
    """Best-effort real star history from GitHub's stargazers timestamp API
    (Accept: application/vnd.github.star+json gives a starred_at per
    stargazer). Bounded to STARGAZER_BACKFILL_MAX_PAGES pages to avoid
    blowing the rate limit on very popular repos — if a repo has more
    stargazers than that, this returns a partial (oldest-missing) history
    and the daily snapshot mechanism fills in the rest going forward."""
    full = f"{owner}/{repo_name}"
    url = f"{API_ROOT}/repos/{full}/stargazers"
    headers = {"Accept": "application/vnd.github.star+json"}
    params = {"per_page": PER_PAGE}

    daily_counts = defaultdict(int)
    page_count = 0
    next_url = url
    next_params = params

    while next_url and page_count < STARGAZER_BACKFILL_MAX_PAGES:
        resp = SESSION.get(next_url, params=next_params, headers={**SESSION.headers, **headers}, timeout=REQUEST_TIMEOUT)
        page_count += 1
        if resp.status_code != 200:
            break
        page = resp.json()
        if not isinstance(page, list) or not page:
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

    if not daily_counts:
        return []

    # Convert daily new-star counts into a cumulative history.
    ordered_days = sorted(daily_counts.keys())
    history = []
    cumulative = 0
    for day in ordered_days:
        cumulative += daily_counts[day]
        history.append({"date": day, "stars": cumulative, "forks": None, "watchers": None, "openIssues": None})
    return history


def load_existing_star_history(out_path):
    if not os.path.exists(out_path):
        return []
    try:
        with open(out_path, encoding="utf-8") as fh:
            existing = json.load(fh)
        return existing.get("starHistory", [])
    except (json.JSONDecodeError, OSError):
        return []


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
    verified_count = 0
    for owner, repo_name, reason in extra_targets:
        key = (owner.lower(), repo_name.lower())
        if key in covered:
            continue
        covered.add(key)
        targets.append((owner, repo_name, reason))
        if reason == "verified":
            verified_count += 1

    log(f"Total tracked repos this run: {len(targets)} "
        f"({len(targets) - len(extra_targets)} via full org/user discovery, "
        f"{len(extra_targets)} explicit registry targets, "
        f"{verified_count} of those from verified=yes)")

    repo_index = {}
    stars_summary = {}
    all_release_items = []
    summary_days = defaultdict(lambda: {"commits": 0, "prs": 0, "issues": 0, "releases": 0, "_authors": set()})
    # login -> aggregated contributor record across every tracked repo.
    contributors_agg = {}
    ingested_count = 0
    stars_captured_count = 0
    star_backfills_performed = 0
    failed = []

    for owner, repo_name, _reason in targets:
        full = f"{owner}/{repo_name}"
        log(f"Ingesting {full}...")

        owner_dir = os.path.join(OUTPUT_DIR, owner)
        os.makedirs(owner_dir, exist_ok=True)
        out_path = os.path.join(owner_dir, f"{repo_name}.json")

        try:
            days_map, release_items, repo_contributors = ingest_repo_activity(owner, repo_name, since_dt)
            series = zero_filled_series(days_map, since_dt, now)
        except Exception as exc:  # noqa: BLE001
            log(f"  activity fetch failed: {exc}")
            failed.append(full)
            continue

        all_release_items.extend(release_items)

        for login, info in repo_contributors.items():
            agg = contributors_agg.setdefault(login, {
                "login": login,
                "avatarUrl": info["avatarUrl"],
                "htmlUrl": info["htmlUrl"],
                "commits": 0,
                "repos": {},
                "lastCommitAt": None,
            })
            # A contributor's avatar/profile URL can't change mid-run, but keep
            # the most recently seen non-null value just in case an earlier
            # repo's API response had a gap.
            agg["avatarUrl"] = agg["avatarUrl"] or info["avatarUrl"]
            agg["htmlUrl"] = agg["htmlUrl"] or info["htmlUrl"]
            agg["commits"] += info["commits"]
            agg["repos"][full] = agg["repos"].get(full, 0) + info["commits"]
            if info["lastCommitAt"] and (not agg["lastCommitAt"] or info["lastCommitAt"] > agg["lastCommitAt"]):
                agg["lastCommitAt"] = info["lastCommitAt"]

        star_history = load_existing_star_history(out_path)
        if not star_history:
            try:
                backfilled = backfill_star_history(owner, repo_name)
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

        if snapshot:
            star_history = append_star_point(star_history, today_key, snapshot)
            stars_captured_count += 1
            stars_summary[full] = {**snapshot, "capturedAt": now.isoformat()}

        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump({
                "repo": full,
                "generatedAt": now.isoformat(),
                "lookbackDays": LOOKBACK_DAYS,
                "series": series,
                "starHistory": star_history,
            }, fh, indent=2)

        repo_index[full] = f"activity/{owner}/{repo_name}.json"
        ingested_count += 1

        for day in series:
            bucket = summary_days[day["date"]]
            bucket["commits"] += day["commits"]
            bucket["prs"] += day["prs"]
            bucket["issues"] += day["issues"]
            bucket["releases"] += day["releases"]
            # Union of authors isn't tracked at summary level (per-repo files
            # already collapsed to counts); approximate with max day activeDevs.
            bucket["_authors"].add(day["activeDevs"])

    feed_count = write_release_feed(all_release_items, now)

    summary_series = []
    for date_key in sorted(summary_days.keys()):
        bucket = summary_days[date_key]
        approx_active_devs = max(bucket["_authors"]) if bucket["_authors"] else 0
        summary_series.append({
            "date": date_key,
            "commits": bucket["commits"],
            "prs": bucket["prs"],
            "issues": bucket["issues"],
            "releases": bucket["releases"],
            "activeDevs": approx_active_devs,
        })

    with open(os.path.join(OUTPUT_DIR, "_summary.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "generatedAt": now.isoformat(),
            "lookbackDays": LOOKBACK_DAYS,
            "orgs": ORGS,
            "repoCount": ingested_count,
            "series": summary_series,
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

    with open(os.path.join(OUTPUT_DIR, "_contributors.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "generatedAt": now.isoformat(),
            "lookbackDays": LOOKBACK_DAYS,
            "note": (
                "Commit counts are cumulative over lookbackDays, not sliced by "
                "the dashboard's 7/30/90/365-day range selector — there is no "
                "per-day breakdown per contributor, only per repo. Only commits "
                "linked to a real GitHub account are included; commits from an "
                "email with no linked account are excluded, not attributed by "
                "guesswork."
            ),
            "contributors": contributors_out,
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
            "failed": failed,
            "lookbackDays": LOOKBACK_DAYS,
            "status": "ok" if ingested_count else "no-data",
        }, fh, indent=2)

    log(f"Done. Ingested {ingested_count} repos ({stars_captured_count} star snapshots, "
        f"{star_backfills_performed} star-history backfills, {feed_count} release feed items, "
        f"{len(contributors_out)} contributors captured), {len(failed)} failed.")


if __name__ == "__main__":
    main()
