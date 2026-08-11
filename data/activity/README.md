# data/activity/

This folder is populated automatically by
`.github/workflows/ingest-activity.yml`, which runs
`scripts/ingest_github_activity.py` on a daily schedule (and on manual
`workflow_dispatch`).

- `_summary.json` — ecosystem-wide daily aggregates (commits, merged PRs,
  issues, releases, approximate active devs) across all ingested repos.
- `_repos.json` — map of `"org/repo"` → path to that repo's daily series file.
- `_stars.json` — latest star/fork/watcher/open-issue snapshot per repo.
- `_meta.json` — last run timestamp, orgs covered, repo/star/contributor counts, any failures.
- `_contributors.json` — top contributors aggregated across all ingested repos:
  real GitHub login, avatar URL, profile URL, cumulative commit count, which
  repos they committed to, and last commit date. Only commits linked to a
  real GitHub account are included — an unlinked commit-author email has no
  login to attribute to, so it's excluded rather than guessed at. Counts are
  cumulative over the ingestion lookback window (not sliced by the
  dashboard's 7/30/90/365-day range selector), and PRs/reviews/issues are
  not attributed per author — the dashboard shows "—" for those on live
  contributor rows.
- `<owner>/<repo>.json` — per-repo `series` (daily commits/PRs/issues/releases)
  plus `starHistory` (one point-in-time stars/forks/watchers/openIssues
  snapshot appended per ingestion run, so a real history builds up day by day).

Until the workflow has run at least once (after this repo is pushed to
GitHub with Actions enabled), these files contain empty placeholders and the
dashboard automatically falls back to its modeled/placeholder activity and
star numbers — see the Methodology tab in the app for what's live vs.
modeled at any given time.

**Coverage** (see `ORGS` and `EXTRA_ORG_TYPE_PREFIXES` in
`scripts/ingest_github_activity.py`):

- Full auto-discovery (every non-archived, non-fork repo owned by the
  account): `kaspanet`.
- Individually ingested, sourced from the registry CSV: every repo whose
  `org_type` is `company-affiliated` or `community org` (e.g. `aspectron`,
  `kasplex`, `K-Kluster`, `kaspa-ng`, `kaspa-live`, `bzminer`, `forbole`,
  `KASPACOM`, `ScopeLift`, `kaspacom`) — scoped to just those curated repos,
  not an owner's entire account.
- Independent-contributor and uncertain-status repos stay modeled for now;
  add an owner to `ORGS`, or broaden `EXTRA_ORG_TYPE_PREFIXES`, to bring
  more of the registry live.

