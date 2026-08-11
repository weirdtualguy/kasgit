# data/feed/

Populated automatically by `.github/workflows/ingest-activity.yml` /
`scripts/ingest_github_activity.py`.

- `releases.json` — the most recent releases (up to 150, last 180 days)
  across every tracked repo, sorted newest first. Each item has `repo`,
  `name`, `tag`, `publishedAt`, `url`, `prerelease`.
- `releases.xml` — the same data as a standard RSS 2.0 feed. Point any feed
  reader at the raw GitHub Pages URL
  (`https://<username>.github.io/<repo>/data/feed/releases.xml`) to follow
  Kaspa ecosystem releases outside the dashboard.

Both files ship as empty placeholders until the ingestion workflow's first
run.
