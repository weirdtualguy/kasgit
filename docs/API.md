# KasGit Data API

KasGit publishes everything it ingests as static, versioned JSON — no
auth, no API key, no rate limit imposed by this project (you're subject
to GitHub Pages' normal serving limits, same as loading any other file
from the site). Anyone can build a bot, alert, or dashboard on top of it.

This document is the human-readable companion to the machine-readable
manifest — **start there for the authoritative, always-current resource
list**: [`data/api/index.json`](../data/api/index.json). Fetch it first;
don't hardcode paths from this doc, since the manifest is regenerated
every ingestion run and is the source of truth if the two ever disagree.

```
GET https://<owner>.github.io/<repo>/data/api/index.json
```

All paths below are relative to the site root. Everything updates once
daily via the `ingest-activity.yml` GitHub Actions workflow.

## Versioning

`schemaVersion` in the manifest only increments on a **breaking** change
to a resource already listed — a field renamed, removed, or given a new
meaning. Adding a new optional field, or a new resource entirely, does
not bump it. If you're building something long-lived, pin to a
`schemaVersion` rather than an exact field list.

## Freshness

Every generated file carries its own `generatedAt`. There's no separate
cache-busting scheme on the API side — if you're polling, compare
`generatedAt` (or `_meta.json`'s) to detect a new run rather than assuming
a fixed daily time.

## Resources

### `data/activity/_repos.json` — repo index

Map of every live-ingested repo (`"owner/repo"`) to the relative path of
its own activity file. Start here to see what's actually in ingestion
scope this run — the registry CSV lists everything *tracked*, but not
every tracked repo necessarily has live data (see `_meta.json`'s
`failed` list for ones that errored out).

```json
{ "kaspanet/rusty-kaspa": "activity/kaspanet/rusty-kaspa.json", "...": "..." }
```

### `data/activity/{owner}/{repo}.json` — per-repo activity

The most detailed resource. One file per repo listed in the repo index.

| Field | Type | Notes |
|---|---|---|
| `series` | array | Daily `{date, commits, prs, issues, releases, activeDevs}` for `lookbackDays` (currently 400) |
| `dataQuality` | object | Per datatype (`commits`/`prs`/`issues`/`releases`): `{status: "complete"\|"partial", lookbackDays}`. `"partial"` means a fetch was abandoned after exhausting retries — that datatype covers *less* than the full window. Treat a partial datatype as "live, but incomplete," not "we have everything." |
| `starHistory` | array | Daily cumulative star counts, best-effort backfilled |
| `starHistoryQuality` | `"complete"` \| `"partial"` \| `null` | `"partial"` = the stargazer backfill hit its page cap (or errored) before reaching the repo's earliest stargazers — the *start* of the curve is missing. `null` = backfill was never attempted (pre-dates this field). Don't render a `"partial"` curve as if it were the full history. |
| `archived` | `true` \| `false` \| `null` | From GitHub directly. `null` means the snapshot call itself failed — distinct from `false`, which is GitHub confirming it's *not* archived. |
| `identifiedCommits` | number | Commits attributable to a real linked GitHub account — the correct denominator for any contributor-share calculation, since bot/unlinked-email commits have no author to attribute |
| `busFactor` | number \| `null` | Minimum number of contributors whose combined commits reach ≥50% of `identifiedCommits`. `null` if `identifiedCommits` is 0. `1` means a single person accounts for half the repo's identified history — the smaller this number, the more concentrated the repo's authorship |
| `topContributors` | array | Top 5 by commit count, `{login, commits}` — for the full per-repo list use `identifiedCommits`/`busFactor` above, not this (it's capped for payload size) |
| `primaryLanguage` | string \| `null` | GitHub's single dominant-language guess |
| `languages` | object | Full byte-count breakdown, `{"Rust": 120000, "TypeScript": 3400, ...}` |
| `topics` | array | GitHub topics as set on the repo |
| `license` | string \| `null` | SPDX identifier (e.g. `"MIT"`), or `null` if unlicensed or GitHub couldn't confidently match one |
| `defaultBranch`, `homepage`, `createdAt`, `pushedAt` | | Straight from GitHub's repo metadata |

### `data/activity/_summary.json` — ecosystem-wide rollup

Pre-aggregated across every ingested repo — the dashboard's own frontend
uses this directly for the default (unfiltered) Overview chart, so you're
getting the same fast path rather than a secondary/inconsistent view.

| Field | Notes |
|---|---|
| `series` | Daily ecosystem totals. `activeDevs` is a true distinct-author count across every repo that day (a developer active on two repos the same day counts once) — not a sum or max of per-repo counts |
| `partialDataRepos` | `{commits: [...], prs: [...], issues: [...], releases: [...]}` — which repos hit a `"partial"` fetch *this run*, per datatype, without needing to fetch and inspect all ~90 individual repo files |
| `languages` | Ecosystem-wide byte totals per language, summed across every repo |
| `topics` | Repo counts per topic, summed across every repo |

### `data/activity/_stars.json` — current star snapshot

Current star/fork/watcher/open-issue count per ingested repo, as of this
run. Lighter-weight than fetching every repo file just to show "how many
stars does X have right now."

### `data/activity/_contributors.json` — ecosystem-wide contributors

Commits, first/last commit date, every distinct active day, and repos
touched — per contributor, capped to the top 100 by total commits. For a
specific repo's own (uncapped) contributor breakdown, use that repo's
`repoActivity` file instead.

`activeDates` is every distinct UTC day they committed to any tracked
repo — this is what lets a consumer compute real period-over-period
distinct-contributor deltas, rather than approximating one.

### `data/activity/ideas.json` — community idea board

Open issues labeled the ideas label on this project's own repo, sorted
by 👍 reaction count then recency. `bodyExcerpt` is **plain text**,
truncated to 240 characters — render as text, never as HTML/markdown.

### `data/feed/releases.json` / `data/feed/releases.xml`

Combined release feed across every ingested repo, newest first. The
`.xml` version is standard RSS 2.0 — subscribe directly in any feed
reader.

### `data/kaspa_github_ecosystem_inventory.csv` — the source registry

The hand-maintained ground truth for what's tracked at all (live or
modeled), including repos outside live ingestion scope. Notable columns:

- `category` — top-level classification (Core, Wallet, SDK, Programmability, ...)
- `org_type` / `org_type_note` — classification (Official/Company/Community/Independent/Uncertain) split from its free-text explanation
- `confidence` / `confidence_note` — same split, for how confident the registry curator is in the entry
- `verified` / `verified_at` — whether a human has independently confirmed the entry

The *derived* form of this file (after category/org-type/confidence
mapping, slugified IDs, and — for a handful of Programmability-adjacent
repos — a `programmabilitySubcategory` tag) is what `assets/js/data.js`
embeds for the frontend; that file isn't published as a separate JSON
API resource today since it's generated as a JS source file, not JSON.

### `data/activity/_meta.json` — this run's health check

Repo/contributor/idea counts, `failed` (repos that errored during this
run — see `dataQuality`/`starHistoryQuality`/`archived: null` above for
what "errored" can mean at a finer grain), and timing. Check this before
trusting anything else from a given run.

## A worked example: "which Kaspa repos are most at single-maintainer risk?"

```bash
curl -s https://<owner>.github.io/<repo>/data/activity/_repos.json \
  | jq -r 'to_entries[] | .value' \
  | while read -r path; do
      curl -s "https://<owner>.github.io/<repo>/data/$path"
    done \
  | jq -s 'map(select(.busFactor != null and .busFactor <= 2))
           | sort_by(.busFactor)
           | .[] | {repo, busFactor, identifiedCommits}'
```

(In practice, fetch resources in parallel — this loop is illustrative,
not the fast path; see `_repos.json`'s size before deciding whether to
parallelize.)
