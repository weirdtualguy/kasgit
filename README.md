# Kaspa GitHub Ecosystem — Phase 1 Dashboard

A static, single-page dashboard for exploring the public Kaspa (KAS) GitHub
ecosystem: a curated registry of repositories/orgs, category breakdown, and
an activity view. Built with vanilla HTML/CSS/JS, a compiled/purged Tailwind
CSS build, and ECharts. No backend, no runtime build step for the app itself
— deploy as-is to GitHub Pages. Tailwind's CSS is pre-compiled and checked
in (`assets/css/tailwind.built.css`); only regenerate it if you add new
Tailwind classes (see [Rebuilding the compiled CSS](#rebuilding-the-compiled-css)).

**Live status:** Phase 1. The **Registry** and **Repositories** data (names,
URLs, categories, org type, status, confidence) come from an AI-assisted
research pass over public GitHub data and still need manual verification —
see each row's confidence badge. Commit/PR/issue/release activity is
ingested daily from the real GitHub API for the **kaspanet** org (see
[Activity ingestion pipeline](#activity-ingestion-pipeline) below) — those
repos carry a **● Live** badge. Every other repo, plus star counts and the
Contributors tab, still use deterministically generated placeholder numbers.
See the in-app **Methodology** tab for the full breakdown.

## Project structure

```
.
├── index.html                  # Entry point (GitHub Pages serves this)
├── tailwind.config.js          # Theme tokens (colors/fonts/shadows) for the compiled build
├── assets/
│   ├── css/
│   │   ├── style.css              # Hand-written custom styles
│   │   ├── tailwind-src.css       # Tailwind directives — input to the compiler
│   │   └── tailwind.built.css     # Compiled, purged, checked-in Tailwind output (do not hand-edit)
│   └── js/
│       ├── data.js             # Generated registry data (do not hand-edit)
│       └── app.js              # App state, rendering, charts, live-data loading, event wiring
├── data/
│   ├── kaspa_github_ecosystem_inventory.csv   # Source-of-truth CSV for the registry
│   └── activity/                # CI-populated GitHub activity aggregates (see below)
├── scripts/
│   ├── build_data.py                 # Regenerates assets/js/data.js from the CSV
│   └── ingest_github_activity.py     # Fetches & aggregates real GitHub activity
├── .github/workflows/
│   └── ingest-activity.yml     # Scheduled + manual-dispatch ingestion job
├── LICENSE
└── README.md
```

## Updating the registry data

The registry is generated from `data/kaspa_github_ecosystem_inventory.csv`.
To add, remove, or correct an entry:

1. Edit the CSV (columns: `name,url,org_type,category,description,last_activity,confidence,verified,verified_at`).
2. Regenerate the data file:
   ```bash
   python3 scripts/build_data.py
   ```
3. Commit both the CSV and the regenerated `assets/js/data.js`.

The build script maps free-text CSV values into the fixed vocab the UI uses:

- **category** → Core, Wallet, Explorer, Mining, SDK, API, CLI, Docs, KRC20, Infra, DeFi, dApp, Other
- **status** → Active, Verify, Slowing, Stale, Deprecated, Archived
- **confidence** → High, Medium-High, Medium, Low-Medium, Low

**`verified` / `verified_at`** are separate from the AI-research `confidence`
score — they mean a human actually checked that row against GitHub. Set
`verified` to `yes` and `verified_at` to a date (e.g. `2026-08-15`) once
you've confirmed a row. This also has a functional effect: any row marked
`verified=yes` gets included in the live activity ingestion (see below)
regardless of its `org_type`, so it's how an independent-contributor repo
graduates from modeled to live data.

## Activity ingestion pipeline

`.github/workflows/ingest-activity.yml` runs `scripts/ingest_github_activity.py`
once a day (cron `17 3 * * *`, plus manual `workflow_dispatch`). It tracks three
kinds of targets:

1. **Full auto-discovery** — every non-archived, non-fork repo owned by the
   accounts in the script's `ORGS` list (currently `kaspanet`). Works for
   both GitHub Orgs and User accounts.
2. **Registry-driven extras** — any repo in `data/kaspa_github_ecosystem_inventory.csv`
   whose `org_type` is `company-affiliated` or `community org` (see
   `EXTRA_ORG_TYPE_PREFIXES`) is ingested individually, even if its owner
   isn't in `ORGS`. This currently covers `aspectron`, `kasplex`,
   `K-Kluster`, `kaspa-ng`, `kaspa-live`, `bzminer`, `forbole`, `KASPACOM`,
   `ScopeLift`, and `kaspacom` repos — scoped to just the repos already
   curated in the registry, not an owner's entire account.
3. **Verified rows** — any CSV row with `verified=yes`, regardless of
   `org_type`. This is how an independent-contributor repo graduates into
   live ingestion once someone has actually checked it — edit the CSV, no
   code change needed.

For every tracked repo it pulls:

- commits (default branch, via `GET /repos/{owner}/{repo}/commits`)
- merged pull requests (`GET /repos/{owner}/{repo}/pulls?state=closed`)
- issues, excluding PRs (`GET /repos/{owner}/{repo}/issues`)
- releases, keeping the real title/tag/date/URL, not just a count
  (`GET /repos/{owner}/{repo}/releases`)
- a point-in-time snapshot of stars/forks/watchers/open issues
  (`GET /repos/{owner}/{repo}`)

Commits/PRs/issues/releases are bucketed into UTC daily counts over a
rolling 400-day window. Stars can't be backfilled from a single snapshot
(GitHub doesn't expose a stars-over-time endpoint for that), so on a
repo's **first** ingestion the script attempts a bounded backfill of real
historical star growth from GitHub's stargazer timestamps
(`GET /repos/{owner}/{repo}/stargazers` with the `star+json` media type,
capped at `STARGAZER_BACKFILL_MAX_PAGES` pages so it doesn't blow the rate
limit on very popular repos). After that, each run appends today's
snapshot to the running `starHistory` array. Output:

- `data/activity/<owner>/<repo>.json` — `{ series, starHistory }` for that repo
- `data/activity/_repos.json` — `"owner/repo"` → file path index
- `data/activity/_stars.json` — latest star snapshot per repo (quick lookup)
- `data/activity/_meta.json` — last run time, repo/star/verified counts, any failures
- `data/feed/releases.json` / `data/feed/releases.xml` — the most recent
  releases (last 180 days, up to 150 items) across every tracked repo, as
  JSON and as a standard RSS 2.0 feed you can subscribe to directly

The workflow then commits and pushes any changes back to the repo. The
front-end (`assets/js/app.js`) fetches `_repos.json`, `_meta.json`, and
`releases.json` at load time and, for each repo it has live data for, uses
the real daily series and latest star count instead of the modeled
generator — everywhere (KPIs, activity chart, heatmap, repo table, top
repos, This Week digest, JSON export). Repos with no ingested data keep
using the deterministic modeled series so the UI never shows an empty
state.

**Before this runs for the first time:** `data/activity/*.json` ship as
empty placeholders, so the dashboard shows "Modeled — ingestion pending
first run." No action needed beyond pushing this repo to GitHub with
Actions enabled — the first scheduled or manually triggered run populates
everything.

**Required repo setting:** under **Settings → Actions → General → Workflow
permissions**, select **"Read and write permissions"** so the workflow's
default `GITHUB_TOKEN` can push the updated JSON back to the branch.

**Extending coverage further:** add owners to `ORGS` for full auto-discovery,
or broaden `EXTRA_ORG_TYPE_PREFIXES` (e.g. to include `independent
contributor` once those repos are verified) in
`scripts/ingest_github_activity.py`.

**Running it manually:**
```bash
export GITHUB_TOKEN=ghp_your_token_here   # a plain public-repo read token is enough
python3 scripts/ingest_github_activity.py
```

## Running locally

No build step required. Any static file server works, e.g.:

```bash
python3 -m http.server 8000
```

Then open `http://localhost:8000`.

## Deploying to GitHub Pages

1. Push this repo to GitHub.
2. Repo Settings → Pages → Source: **Deploy from a branch** → select `main` (or your default branch) and `/ (root)`.
3. Your site will be live at `https://<username>.github.io/<repo-name>/`.

The included `.nojekyll` file tells GitHub Pages to serve files as-is
without running them through Jekyll.

## Security notes

- A `Content-Security-Policy` meta tag restricts script/style/font origins to
  the specific CDNs this page loads from. Tailwind no longer needs an entry
  there — see below.
- The two jsDelivr assets (`@fontsource/*`, ECharts) are version-pinned and
  ready for Subresource Integrity — add `integrity`/`crossorigin` once you've
  generated the real hashes for the pinned versions (do not guess a hash;
  a wrong one fails closed and breaks the page).
- `frame-ancestors` is deliberately not in the CSP: it's ignored when
  delivered via `<meta>` (spec limitation — needs an HTTP response header),
  and GitHub Pages' static hosting can't set one. Not a gap you can close
  without moving off pure static hosting.

## Rebuilding the compiled CSS

Tailwind is no longer loaded from the Play CDN — `assets/css/tailwind.built.css`
is a compiled, minified, checked-in build (`tailwindcss@3.4.19`, scanned
against `index.html` and `assets/js/**/*.js` per `tailwind.config.js`). This
removes the CDN's runtime JIT compile, the "not for production" console
warning, the impossibility of pinning/SRI-verifying it, and the `unsafe-eval`
CSP allowance it needed.

**You only need to rebuild it if you add a Tailwind utility class that isn't
already used somewhere in the project** (the compiler only emits CSS for
classes it finds by scanning those files):

```bash
npm install -D tailwindcss@3.4.19
npx tailwindcss -i ./assets/css/tailwind-src.css \
  -o ./assets/css/tailwind.built.css \
  -c ./tailwind.config.js --minify
```

Commit the regenerated `assets/css/tailwind.built.css` alongside your change.

## Known limitations / next steps

- Activity is live for ingested repos and modeled placeholders for
  everything else (see Methodology tab and the "● Live" / "○ Modeled"
  badges) — extend `ORGS`, `EXTRA_ORG_TYPE_PREFIXES`, or the CSV's
  `verified` column to bring more repos onto real data.
- Contributor identities are real GitHub commit authors (login, avatar,
  profile link, commit count) once the ingestion workflow has run — see
  `data/activity/_contributors.json` — but only commits, not PRs, reviews,
  or issues, are attributed per author; those columns show "—" for live
  contributors rather than a fabricated number. Falls back to synthetic
  placeholder handles before the workflow's first run. Commit counts are
  cumulative over the ingestion lookback window, not sliced by the
  dashboard's range selector.
- No repo-to-repo relationship data yet (e.g. "this wallet depends on that
  SDK") — would need a `related_to` column added to the registry CSV and
  corresponding UI, intentionally left out rather than guessed at.
- The registry's `confidence` (AI-research estimate) and `verified`
  (human-checked) fields are deliberately separate — don't conflate a high
  confidence score with actual verification.

## License

MIT — see [LICENSE](LICENSE).
