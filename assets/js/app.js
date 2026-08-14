(function () {
  "use strict";

  // ---------------------------------------------------------------------
  // Guard: make sure data.js loaded before this file runs.
  // ---------------------------------------------------------------------
  if (typeof REGISTRY_ROWS === "undefined" || typeof REPOS === "undefined") {
    showFatalError("Dashboard data failed to load (assets/js/data.js). Check the file was deployed alongside index.html.");
    return;
  }

  const state = {
    view: "overview",
    range: 30,
    category: "All",
    repoQuery: "",
    contributorQuery: "",
    registryQuery: "",
    registryStatus: "All",
    repoSort: "commits",
    nonce: 1
  };

  const CATEGORY_META = {
    All:      { color: "#22d3ee", intensity: 1.00 },
    Core:     { color: "#2dd4bf", intensity: 1.00 },
    Wallet:   { color: "#38bdf8", intensity: 0.62 },
    Explorer: { color: "#a78bfa", intensity: 0.46 },
    Mining:   { color: "#f59e0b", intensity: 0.58 },
    SDK:      { color: "#34d399", intensity: 0.44 },
    API:      { color: "#60a5fa", intensity: 0.28 },
    CLI:      { color: "#94a3b8", intensity: 0.16 },
    Docs:     { color: "#f472b6", intensity: 0.22 },
    KRC20:    { color: "#facc15", intensity: 0.34 },
    Programmability: { color: "#e879f9", intensity: 0.52 },
    Infra:    { color: "#4ade80", intensity: 0.26 },
    DeFi:     { color: "#fb7185", intensity: 0.30 },
    dApp:     { color: "#fb923c", intensity: 0.24 },
    Other:    { color: "#94a3b8", intensity: 0.14 }
  };

  const TABS = [
    { id: "overview", label: "Overview" },
    { id: "repos", label: "Repositories" },
    { id: "contributors", label: "Contributors" },
    { id: "registry", label: "Registry" },
    { id: "ideas", label: "Ideas" },
    { id: "methodology", label: "Methodology" }
  ];

  const CONTRIBUTOR_HANDLES = (() => {
    const prefixes = ["kaspa", "dag", "block", "ghost", "heavy", "krc", "node", "hash", "script", "merkle"];
    const suffixes = ["builder", "dev", "coder", "labs", "contrib", "eng", "ops", "sec", "research", "tooling"];
    const handles = [];
    for (let i = 0; i < 24; i += 1) {
      const prefix = prefixes[i % prefixes.length];
      const suffix = suffixes[Math.floor(i / prefixes.length) % suffixes.length];
      handles.push(`${prefix}_${suffix}_${String(i + 1).padStart(2, "0")}`);
    }
    return handles;
  })();

  let activityChart = null;
  let categoryChart = null;
  const echartsAvailable = typeof window.echarts !== "undefined";

  // Live GitHub activity data, populated asynchronously by loadLiveActivity()
  // from data/activity/*.json (written by the scheduled ingestion workflow).
  // Any repo not present here falls back to modeled placeholder data.
  const LIVE = { repos: {}, meta: null, releases: [], contributors: [], ideas: [] };

  // Set to "owner/repo" if this dashboard is hosted on a custom domain —
  // resolveRepoSlug() can only auto-detect the repo on the default
  // *.github.io Pages URL. Leave null to rely on auto-detection.
  const KASGIT_REPO_OVERRIDE = null;

  // Must match IDEAS_LABEL in scripts/ingest_github_activity.py.
  const IDEAS_LABEL = "idea";

  // ---------------------------------------------------------------------
  // Utilities
  // ---------------------------------------------------------------------

  function debounce(fn, wait = 150) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[ch]));
  }

  function safeUrl(url) {
    try {
      const parsed = new URL(url, window.location.href);
      if (parsed.protocol === "http:" || parsed.protocol === "https:") {
        return parsed.href;
      }
    } catch (err) {
      /* fall through */
    }
    return "#";
  }

  function hashString(input) {
    let hash = 0;
    for (let idx = 0; idx < input.length; idx += 1) {
      hash = Math.imul(31, hash) + input.charCodeAt(idx) | 0;
    }
    return hash >>> 0;
  }

  function mulberry32(seed) {
    let localSeed = seed;
    return function next() {
      localSeed |= 0;
      localSeed = localSeed + 0x6D2B79F5 | 0;
      let t = Math.imul(localSeed ^ localSeed >>> 15, 1 | localSeed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }

  function formatNumber(value) {
    const numeric = Number(value) || 0;
    if (numeric >= 10000) {
      return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(numeric);
    }
    return new Intl.NumberFormat("en-US").format(numeric);
  }

  function formatDate(date, withYear = false) {
    return date.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      ...(withYear ? { year: "numeric" } : {})
    });
  }

  function animateNumber(element, target) {
    if (!element) return;
    const start = Number(element.dataset.value || 0);
    const duration = 650;
    const startTime = performance.now();

    function tick(now) {
      const progress = Math.min(1, (now - startTime) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      const current = Math.round(start + (target - start) * eased);
      element.textContent = formatNumber(current);
      if (progress < 1) requestAnimationFrame(tick);
      else element.dataset.value = String(target);
    }

    requestAnimationFrame(tick);
  }

  function statusBadge(status) {
    const map = {
      Active: "badge-emerald",
      Unconfirmed: "badge-slate",
      Unreachable: "badge-amber",
      Slowing: "badge-cyan",
      Stale: "badge-slate",
      Deprecated: "badge-rose",
      Archived: "badge-rose"
    };
    const cls = map[status] || "badge-slate";
    return `<span class="badge ${cls}">${escapeHtml(status)}</span>`;
  }

  const STATUS_ACTIVE_DAYS = 30;
  const STATUS_SLOWING_DAYS = 120;

  /**
   * The status actually shown everywhere in the UI — live GitHub data first,
   * the registry CSV's status only as a fallback for rows outside live
   * ingestion scope. Precedence, highest first:
   *   1. Deprecated in the CSV — a human call ("this is superseded") that
   *      commit recency can't see and shouldn't override.
   *   2. Ingestion targeted this repo but the GitHub API call itself failed
   *      (dead URL, renamed, gone private) — "Unreachable", not silently
   *      hidden, since that's a genuinely actionable signal.
   *   3. GitHub reports the repo as archived (real API field, not a guess).
   *   4. Live commit recency — Active/Slowing/Stale, computed from the same
   *      per-repo series everything else on this dashboard already uses.
   *   5. No live data at all (row excluded from ingestion scope) — whatever
   *      the registry CSV says, which by now is only ever "Unconfirmed"
   *      unless a human set something more specific.
   * Cached per render pass (see statusCache in updateDashboard) since it's
   * called from several render functions for the same repos.
   */
  function effectiveRepoStatus(repo) {
    if (statusCache.has(repo.repoPath)) return statusCache.get(repo.repoPath);

    let result;
    if (repo.status === "Deprecated") {
      result = { label: "Deprecated", reason: null };
    } else if (LIVE.failedRepoSet?.has(repo.repoPath)) {
      result = {
        label: "Unreachable",
        reason: "GitHub ingestion failed for this repo's URL on the last run — it may have moved, gone private, or been deleted."
      };
    } else {
      const liveRecord = LIVE.repos[repo.repoPath];
      if (!liveRecord) {
        result = { label: repo.status, reason: null };
      } else if (liveRecord.archived === true) {
        result = { label: "Archived", reason: null };
      } else {
        const days = repoLastCommitDays(repo);
        if (days === null) result = { label: "Stale", reason: null };
        else if (days <= STATUS_ACTIVE_DAYS) result = { label: "Active", reason: null };
        else if (days <= STATUS_SLOWING_DAYS) result = { label: "Slowing", reason: null };
        else result = { label: "Stale", reason: null };
      }
    }
    statusCache.set(repo.repoPath, result);
    return result;
  }

  function categoryBadge(category) {
    const color = CATEGORY_META[category]?.color || "#94a3b8";
    return `<span class="badge" style="color:${color}; border-color:${color}33; background:${color}14;">${escapeHtml(category)}</span>`;
  }

  function avatarColor(seedText) {
    const hue = hashString(seedText) % 360;
    return `hsl(${hue} 75% 62%)`;
  }

  /**
   * Avatar markup shared by every contributor/idea-author list. Always uses
   * a Kaspicon (https://github.com/weirdtualguy/kaspicon) deterministic pixel
   * identicon generated from the handle — NOT GitHub's own avatar_url. GitHub
   * always returns an avatar_url, including for accounts with no uploaded
   * photo (it points at GitHub's own generated identicon in that case), so
   * there's no reliable "do they actually have a real photo" signal to key a
   * GitHub-photo-vs-Kaspicon fallback on — an earlier version of this
   * function tried exactly that and Kaspicon effectively never rendered
   * because of it. `{ preset: "list" }` is Kaspicon's own recommended bundle
   * for this case (many small avatars shown together, spectrum palette so
   * they're distinguishable at a glance). Falls back to a colored-initials
   * tile only if window.Kaspicon itself isn't available (script failed to
   * load, or threw) — see the script tag in index.html.
   */
  function avatarMarkup(seedText, sizeClass, textSizeClass = "text-[10px]") {
    if (typeof window.Kaspicon !== "undefined") {
      try {
        const dataUri = window.Kaspicon.toDataURL(seedText, { preset: "list" });
        return `<img src="${escapeHtml(dataUri)}" alt="" class="${sizeClass} shrink-0" loading="lazy" />`;
      } catch (err) {
        // Falls through to the initials tile below.
      }
    }
    const initials = seedText.split(/[_-]/).slice(0, 2).map(part => part[0]?.toUpperCase() || "").join("");
    const color = avatarColor(seedText);
    return `<div class="${sizeClass} flex items-center justify-center ${textSizeClass} font-bold text-black/80 shrink-0" style="background:${color}">${escapeHtml(initials)}</div>`;
  }

  function filteredRepos({ query = "", category = state.category } = {}) {
    const normalizedQuery = query.trim().toLowerCase();
    return REPOS.filter(repo => {
      const matchesCategory = category === "All" || repo.category === category;
      const matchesQuery =
        !normalizedQuery ||
        repo.name.toLowerCase().includes(normalizedQuery) ||
        repo.repoPath.toLowerCase().includes(normalizedQuery) ||
        repo.description.toLowerCase().includes(normalizedQuery) ||
        repo.tags.some(tag => tag.toLowerCase().includes(normalizedQuery));
      return matchesCategory && matchesQuery;
    });
  }

  // ---------------------------------------------------------------------
  // Data layer: live GitHub activity (when ingested) blended with modeled
  // placeholder data for repos that haven't been ingested yet. See the
  // Methodology tab and data/activity/README.md for what's live vs modeled.
  // ---------------------------------------------------------------------

  function dateKeysForRange(days) {
    const now = new Date();
    const endUTC = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
    const keys = [];
    for (let i = days - 1; i >= 0; i -= 1) {
      keys.push(new Date(endUTC - i * 86400000).toISOString().slice(0, 10));
    }
    return keys;
  }

  function modeledRepoDailySeries(repo, days) {
    const keys = dateKeysForRange(days);
    // Seed intentionally excludes state.category: categoryIntensity below already
    // scales the series by category, so a repo's modeled numbers must stay stable
    // across category-filter changes (previously they didn't — see audit).
    const seed = hashString(`repo:${repo.id}:${days}:${state.nonce}`);
    const rng = mulberry32(seed);
    const categoryIntensity = CATEGORY_META[repo.category]?.intensity || 0.35;
    const tierMultiplier = repo.tier === 1 ? 1.45 : repo.tier === 2 ? 0.85 : 0.55;
    const dailyBase = categoryIntensity * tierMultiplier * (1.05 + rng() * 1.5);

    return keys.map(key => {
      const weekday = new Date(`${key}T00:00:00Z`).getUTCDay();
      const weekendFactor = weekday === 0 || weekday === 6 ? 0.5 : 1;
      const noise = 0.4 + rng() * 1.3;
      const commits = Math.max(0, Math.round(dailyBase * weekendFactor * noise));
      const prs = Math.round(commits * (0.16 + rng() * 0.22));
      const issues = Math.round(commits * (0.10 + rng() * 0.22));
      const releases = rng() > 0.965 ? 1 : 0;
      const activeDevs = commits > 0 ? Math.max(1, Math.round(commits / (2.4 + rng() * 3.2))) : 0;
      return { date: key, commits, prs, issues, releases, activeDevs };
    });
  }

  /**
   * Returns { series, isLive } for a repo over the last `days` days.
   * Uses ingested GitHub data (assets fetched from data/activity/) when
   * available for that repo, otherwise falls back to a deterministic
   * modeled series.
   */
  // Per-render-pass cache: updateDashboard() calls renderKpis, renderActivityChart,
  // renderHeatmap, renderTopRepos, renderRepoTable, renderWeeklyDigest in sequence,
  // several of which independently ask for the same repo+days series. Without this,
  // every one of those re-fetches/re-generates the same data (see audit). Cleared
  // at the top of updateDashboard() below.
  let seriesCache = new Map();

  // effectiveRepoStatus() cache — same per-render-pass rationale as
  // seriesCache above (it's called from renderKpis, renderRepoTable,
  // renderRegistryTable, renderStartHere, renderNeedsBuilder, and
  // renderBusFactorWatch, all for the same repos in one pass). Cleared
  // alongside seriesCache in updateDashboard().
  let statusCache = new Map();

  function getRepoDailySeries(repo, days) {
    const cacheKey = `${repo.id}:${days}`;
    const cached = seriesCache.get(cacheKey);
    if (cached) return cached;

    let result;
    const liveRecord = LIVE.repos[repo.repoPath];
    if (liveRecord && Array.isArray(liveRecord.series) && liveRecord.series.length) {
      const byDate = new Map(liveRecord.series.map(day => [day.date, day]));
      const keys = dateKeysForRange(days);
      const series = keys.map(key => byDate.get(key) || {
        date: key, commits: 0, prs: 0, issues: 0, releases: 0, activeDevs: 0
      });
      result = { series, isLive: true };
    } else {
      result = { series: modeledRepoDailySeries(repo, days), isLive: false };
    }
    seriesCache.set(cacheKey, result);
    return result;
  }

  function sumSeries(series, field) {
    return series.reduce((sum, day) => sum + (day[field] || 0), 0);
  }

  function downsampleTrend(series, buckets = 12) {
    if (series.length <= buckets) {
      return series.map(day => day.commits);
    }
    const chunkSize = series.length / buckets;
    const out = [];
    for (let i = 0; i < buckets; i += 1) {
      const start = Math.floor(i * chunkSize);
      const end = Math.max(start + 1, Math.floor((i + 1) * chunkSize));
      const chunk = series.slice(start, end);
      out.push(Math.round(sumSeries(chunk, "commits") / chunk.length));
    }
    return out;
  }

  let ecosystemSeriesCache = new Map();

  function ecosystemSeries(days) {
    const cacheKey = `${days}:${state.category}:${state.nonce}`;
    const cached = ecosystemSeriesCache.get(cacheKey);
    if (cached) return cached;

    const keys = dateKeysForRange(days);
    const repos = filteredRepos();
    const totals = new Map(keys.map(key => [key, { commits: 0, prs: 0, issues: 0, releases: 0, activeDevs: 0 }]));

    repos.forEach(repo => {
      const { series } = getRepoDailySeries(repo, days);
      series.forEach(day => {
        const bucket = totals.get(day.date);
        if (!bucket) return;
        bucket.commits += day.commits;
        bucket.prs += day.prs;
        bucket.issues += day.issues;
        bucket.releases += day.releases;
        bucket.activeDevs += day.activeDevs;
      });
    });

    const result = keys.map(key => {
      const bucket = totals.get(key);
      return { date: new Date(`${key}T00:00:00Z`), ...bucket };
    });
    ecosystemSeriesCache.set(cacheKey, result);
    return result;
  }

  /** Sum of a field over the most recent `days` entries of an ecosystemSeries(2*days) call,
   * split into the current window and the equal-length window immediately before it. Used
   * to compute real vs.-previous-period deltas instead of random placeholders. */
  function periodComparison(days, field) {
    const doubled = ecosystemSeries(days * 2);
    const prior = doubled.slice(0, days);
    const current = doubled.slice(days);
    const sum = (arr) => arr.reduce((total, item) => total + (item[field] || 0), 0);
    const priorSum = sum(prior);
    const currentSum = sum(current);
    const pct = priorSum > 0 ? ((currentSum - priorSum) / priorSum) * 100 : (currentSum > 0 ? 100 : 0);
    return { priorSum, currentSum, pct };
  }

  /**
   * Distinct-contributor period delta: current `days`-day window vs. the
   * equal-length window immediately before it, using each contributor's real
   * activeDates (every distinct UTC day they committed anywhere, from
   * ingestion — see _contributors.json). This is a genuine set-membership
   * check, not a sum, so someone active in both windows is correctly counted
   * once in each rather than double-counted or dropped. hasData is false
   * when LIVE.contributors is empty or predates activeDates being ingested —
   * callers must show "--" rather than a delta computed from nothing.
   */
  function contributorsPeriodDelta(days) {
    const doubled = dateKeysForRange(days * 2);
    const priorKeys = new Set(doubled.slice(0, days));
    const currentKeys = new Set(doubled.slice(days));

    let priorCount = 0;
    let currentCount = 0;
    let hasData = false;
    LIVE.contributors.forEach(entry => {
      const dates = entry.activeDates;
      if (!Array.isArray(dates)) return;
      hasData = true;
      if (dates.some(d => priorKeys.has(d))) priorCount += 1;
      if (dates.some(d => currentKeys.has(d))) currentCount += 1;
    });

    const pct = priorCount > 0 ? ((currentCount - priorCount) / priorCount) * 100 : (currentCount > 0 ? 100 : 0);
    return { priorCount, currentCount, pct, hasData };
  }

  function repoMetrics(repo, days) {
    const { series, isLive } = getRepoDailySeries(repo, days);
    const commits = sumSeries(series, "commits");
    const prs = sumSeries(series, "prs");
    const issues = sumSeries(series, "issues");
    const releases = sumSeries(series, "releases");

    const activeDaysWithCommits = series.filter(day => day.commits > 0);
    const activeDevs = activeDaysWithCommits.length
      ? Math.max(1, Math.round(sumSeries(activeDaysWithCommits, "activeDevs") / activeDaysWithCommits.length))
      : (isLive ? 0 : 1);

    // Star counts: use the real latest snapshot when the ingestion pipeline
    // has captured one (data/activity/<owner>/<repo>.json starHistory),
    // otherwise fall back to a deterministic modeled figure.
    const categoryIntensity = CATEGORY_META[repo.category]?.intensity || 0.35;
    const liveRecord = LIVE.repos[repo.repoPath];
    const starHistory = liveRecord?.starHistory;
    const latestStarPoint = Array.isArray(starHistory) && starHistory.length
      ? starHistory[starHistory.length - 1]
      : null;

    let stars;
    let starsLive = false;
    if (latestStarPoint) {
      stars = latestStarPoint.stars;
      starsLive = true;
    } else {
      const starSeed = hashString(`stars:${repo.id}:${state.nonce}`);
      const starRng = mulberry32(starSeed);
      stars = Math.round((repo.tier === 1 ? 1900 : 760) * categoryIntensity * (0.85 + starRng() * 1.6));
    }

    const trend = downsampleTrend(series);

    return { commits, prs, issues, releases, activeDevs, stars, starsLive, trend, isLive };
  }

  function contributorMetrics(handle, days) {
    // Seed intentionally excludes state.category — the Contributors tab has no
    // category filter or indicator of its own, so contributor numbers must not
    // silently shift based on whatever category was last selected on Overview
    // (previously they did — see audit).
    const seed = hashString(`contributor:${handle}:${days}:${state.nonce}`);
    const rng = mulberry32(seed);
    const intensity = 1;

    const commits = Math.round(days * intensity * (1.8 + rng() * 13.5));
    const prs = Math.round(commits * (0.12 + rng() * 0.24));
    const reviews = Math.round(prs * (0.5 + rng() * 1.8));
    const issues = Math.round(commits * (0.05 + rng() * 0.18));
    const repos = 1 + Math.floor(rng() * 6);
    const lastActive = Math.floor(rng() * Math.min(days, 30));

    return { commits, prs, reviews, issues, repos, lastActive };
  }

  /**
   * Normalized contributor rows for both renderTopContributors and
   * renderContributorTable. Prefers real data from data/activity/_contributors.json
   * (see ingest_github_activity.py) once the ingestion workflow has run;
   * falls back to the fully-synthetic CONTRIBUTOR_HANDLES/contributorMetrics
   * generator otherwise, so the tab still shows something before first ingest.
   *
   * IMPORTANT ASYMMETRY: live rows only have real commits + repos + first/last-commit
   * dates (that's what the GitHub commits API gives us per author). PRs, reviews,
   * and issues are NOT attributed per-author by the ingestion script, so live
   * rows report those as null — rendered as "—", never as a fabricated 0 or a
   * modeled guess. Also note live commit counts are cumulative over the
   * ingestion's lookback window (currently 400 days), not sliced by the
   * dashboard's range selector — there's no per-day-per-author breakdown.
   * isNew is true when firstCommitAt falls within NEW_CONTRIBUTOR_WINDOW_DAYS —
   * i.e. we have no record of a commit from them before that, within the
   * ingestion's lookback window. Always false for modeled rows; never guessed.
   */
  const NEW_CONTRIBUTOR_WINDOW_DAYS = 14;

  function contributorRows() {
    if (LIVE.contributors.length) {
      const now = Date.now();
      return LIVE.contributors.map(entry => {
        const firstCommitDays = entry.firstCommitAt
          ? Math.max(0, Math.floor((now - new Date(entry.firstCommitAt).getTime()) / 86400000))
          : null;
        return {
          handle: entry.login,
          htmlUrl: entry.htmlUrl,
          avatarUrl: entry.avatarUrl,
          isLive: true,
          metrics: {
            commits: entry.commits,
            prs: null,
            reviews: null,
            issues: null,
            repos: entry.repoCount,
            lastActive: entry.lastCommitAt
              ? Math.max(0, Math.floor((now - new Date(entry.lastCommitAt).getTime()) / 86400000))
              : null,
            firstCommitAt: entry.firstCommitAt || null,
            firstCommitRepo: entry.firstCommitRepo || null,
            isNew: firstCommitDays !== null && firstCommitDays <= NEW_CONTRIBUTOR_WINDOW_DAYS
          }
        };
      });
    }
    return CONTRIBUTOR_HANDLES.map(handle => ({
      handle,
      htmlUrl: null,
      avatarUrl: null,
      isLive: false,
      metrics: { ...contributorMetrics(handle, state.range), firstCommitAt: null, firstCommitRepo: null, isNew: false }
    }));
  }

  function sparklineSvg(values, color, width = 96, height = 30) {
    const max = Math.max(...values, 1);
    const min = Math.min(...values, 0);
    const span = Math.max(max - min, 1);
    const points = values.map((value, index) => {
      const x = (index / (values.length - 1)) * (width - 4) + 2;
      const y = height - 3 - ((value - min) / span) * (height - 8);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");

    return `
      <svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" fill="none" aria-hidden="true">
        <polyline points="${points}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity="0.95" />
      </svg>
    `;
  }

  function computeKpis() {
    const series = ecosystemSeries(state.range);
    const totalCommits = series.reduce((sum, item) => sum + item.commits, 0);
    const totalPRs = series.reduce((sum, item) => sum + item.prs, 0);
    const totalIssues = series.reduce((sum, item) => sum + item.issues, 0);
    const totalReleases = series.reduce((sum, item) => sum + item.releases, 0);

    const activeRepoCount = filteredRepos().length;
    const avgDevs = series.length
      ? Math.round(series.reduce((sum, item) => sum + item.activeDevs, 0) / series.length)
      : 0;

    const contributorMultiplier = state.category === "All" ? 2.65 : 1.75;
    const contributors = Math.max(3, Math.round(avgDevs * contributorMultiplier));

    return { totalCommits, totalPRs, totalIssues, totalReleases, activeRepoCount, contributors };
  }

  // ---------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------

  function renderKpis() {
    const kpis = computeKpis();

    animateNumber(document.getElementById("kpiCommits"), kpis.totalCommits);
    animateNumber(document.getElementById("kpiPRs"), kpis.totalPRs);
    animateNumber(document.getElementById("kpiRepos"), kpis.activeRepoCount);
    animateNumber(document.getElementById("kpiDevs"), kpis.contributors);
    animateNumber(document.getElementById("kpiReleases"), kpis.totalReleases);
    animateNumber(document.getElementById("kpiIssues"), kpis.totalIssues);

    // Real vs.-previous-period deltas (current range vs. the equal-length range
    // immediately before it), computed from the same blended live/modeled series
    // as the KPI totals above — not random noise (see audit).
    const deltaFields = [
      ["deltaCommits", "commits"],
      ["deltaPRs", "prs"],
      ["deltaReleases", "releases"],
      ["deltaIssues", "issues"]
    ];

    deltaFields.forEach(([id, field]) => {
      const el = document.getElementById(id);
      if (!el) return;
      const { pct, priorSum, currentSum } = periodComparison(state.range, field);
      if (priorSum === 0 && currentSum === 0) {
        el.className = "mt-1 text-xs font-medium text-slate-500";
        el.textContent = "No activity in either period";
        return;
      }
      const value = Math.round(pct * 10) / 10;
      const positive = value >= 0;
      el.className = `mt-1 text-xs font-medium ${positive ? "text-emerald-300" : "text-rose-300"}`;
      el.textContent = `${positive ? "▲" : "▼"} ${Math.abs(value)}% vs previous period`;
    });

    // Repos isn't a time series over the range in the same sense as the four
    // fields above (the registry's size doesn't change with the range
    // selector), so it stays "--" rather than fabricate a delta. Contributors
    // gets a real one — see contributorsPeriodDelta — but only when live
    // per-contributor activeDates data actually exists.
    const reposEl = document.getElementById("deltaRepos");
    if (reposEl) { reposEl.className = "mt-1 text-xs text-slate-500"; reposEl.textContent = "--"; }

    const devsEl = document.getElementById("deltaDevs");
    if (devsEl) {
      const { priorCount, currentCount, pct, hasData } = contributorsPeriodDelta(state.range);
      if (!hasData) {
        devsEl.className = "mt-1 text-xs text-slate-500";
        devsEl.textContent = "--";
      } else if (priorCount === 0 && currentCount === 0) {
        devsEl.className = "mt-1 text-xs font-medium text-slate-500";
        devsEl.textContent = "No activity in either period";
      } else {
        const value = Math.round(pct * 10) / 10;
        const positive = value >= 0;
        devsEl.className = `mt-1 text-xs font-medium ${positive ? "text-emerald-300" : "text-rose-300"}`;
        devsEl.textContent = `${positive ? "▲" : "▼"} ${Math.abs(value)}% vs previous period`;
      }
    }

    const activeCount = REPOS.filter(repo => effectiveRepoStatus(repo).label === "Active").length;
    const needsLookCount = REPOS.filter(repo => {
      const label = effectiveRepoStatus(repo).label;
      return label === "Unconfirmed" || label === "Unreachable";
    }).length;
    const totalRows = REGISTRY_ROWS.length;

    // The six KPI tiles above sum live-ingested + modeled numbers together with
    // no per-tile breakdown (only the activity chart has a live/modeled badge).
    // Surface the blend ratio once here so the totals aren't read as fully real.
    const liveNoteEl = document.getElementById("kpiLiveNote");
    if (liveNoteEl) {
      const repos = filteredRepos();
      const liveRepoCount = repos.filter(repo => getRepoDailySeries(repo, state.range).isLive).length;
      liveNoteEl.textContent = repos.length
        ? `KPI totals blend ${liveRepoCount} of ${repos.length} repos on live GitHub data with modeled placeholders for the rest (Contributors tab: ${LIVE.contributors.length ? "real GitHub commit authors" : "fully modeled — run ingestion to populate"}) — see Methodology.`
        : "--";
    }

    document.getElementById("registryCoverage").textContent = `${totalRows} resources`;
    document.getElementById("verifiedActiveRepos").textContent = `${activeCount} repo${activeCount === 1 ? "" : "s"}`;
    document.getElementById("needsVerification").textContent = `${needsLookCount} repo${needsLookCount === 1 ? "" : "s"}`;
  }

  function renderActivityChart() {
    if (!activityChart) return;

    const series = ecosystemSeries(state.range);
    const labels = series.map(item => formatDate(item.date));

    const option = {
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis",
        backgroundColor: "rgba(3,7,18,0.96)",
        borderColor: "rgba(255,255,255,0.08)",
        textStyle: { color: "#e2e8f0" },
        axisPointer: { type: "shadow" }
      },
      legend: {
        top: 4,
        left: "center",
        icon: "roundRect",
        itemWidth: 9,
        itemHeight: 9,
        itemGap: 14,
        textStyle: { color: "#94a3b8", fontSize: 11 }
      },
      grid: { left: 42, right: 42, top: 56, bottom: 62 },
      dataZoom: [
        { type: "inside" },
        {
          type: "slider",
          height: 18,
          bottom: 8,
          borderColor: "rgba(255,255,255,0.08)",
          backgroundColor: "rgba(255,255,255,0.03)",
          fillerColor: "rgba(45,212,191,0.12)",
          handleStyle: { color: "#2dd4bf" },
          textStyle: { color: "#64748b", fontSize: 10 }
        }
      ],
      xAxis: {
        type: "category",
        data: labels,
        axisLine: { lineStyle: { color: "rgba(255,255,255,0.08)" } },
        axisTick: { show: false },
        axisLabel: { color: "#64748b", hideOverlap: true, fontSize: 10 }
      },
      yAxis: [
        {
          type: "value",
          name: "Commits",
          nameTextStyle: { color: "#64748b", fontSize: 10 },
          axisLabel: { color: "#64748b", fontSize: 10 },
          splitLine: { lineStyle: { color: "rgba(255,255,255,0.05)" } }
        },
        {
          type: "value",
          axisLabel: { color: "#64748b", fontSize: 10 },
          splitLine: { show: false }
        }
      ],
      series: [
        {
          name: "Commits",
          type: "bar",
          barWidth: "42%",
          data: series.map(item => item.commits),
          itemStyle: {
            borderRadius: [7, 7, 0, 0],
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: "rgba(45,212,191,0.95)" },
              { offset: 1, color: "rgba(8,145,178,0.18)" }
            ])
          }
        },
        {
          name: "Merged PRs",
          type: "line",
          yAxisIndex: 1,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: "#38bdf8" },
          areaStyle: { opacity: 0.08, color: "#38bdf8" },
          data: series.map(item => item.prs)
        },
        {
          name: "Active Devs",
          type: "line",
          yAxisIndex: 1,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: "#a78bfa" },
          areaStyle: { opacity: 0.05, color: "#a78bfa" },
          data: series.map(item => item.activeDevs)
        }
      ]
    };

    activityChart.setOption(option, true);
  }

  function renderCategoryChart() {
    if (!categoryChart) return;

    const counts = {};
    REPOS.forEach(repo => {
      if (state.category === "All" || repo.category === state.category) {
        counts[repo.category] = (counts[repo.category] || 0) + 1;
      }
    });

    const data = Object.entries(counts).map(([name, value]) => ({
      name,
      value,
      itemStyle: { color: CATEGORY_META[name]?.color || "#94a3b8" }
    }));

    const option = {
      backgroundColor: "transparent",
      tooltip: {
        trigger: "item",
        backgroundColor: "rgba(3,7,18,0.96)",
        borderColor: "rgba(255,255,255,0.08)",
        textStyle: { color: "#e2e8f0" },
        formatter: "{b}<br/>{c} repos ({d}%)"
      },
      legend: {
        type: "scroll",
        bottom: 0,
        left: "center",
        width: "92%",
        icon: "circle",
        itemWidth: 8,
        itemHeight: 8,
        itemGap: 10,
        textStyle: { color: "#94a3b8", fontSize: 10 },
        pageIconColor: "#94a3b8",
        pageIconInactiveColor: "#334155",
        pageTextStyle: { color: "#64748b", fontSize: 9 }
      },
      series: [
        {
          name: "Categories",
          type: "pie",
          radius: ["52%", "82%"],
          center: ["50%", "42%"],
          avoidLabelOverlap: true,
          itemStyle: { borderColor: "#050810", borderWidth: 3 },
          label: { show: false },
          labelLine: { show: false },
          data
        }
      ]
    };

    categoryChart.setOption(option, true);
  }

  function renderHeatmap() {
    const root = document.getElementById("repoHeatmap");
    if (!root) return;

    const repos = filteredRepos().slice(0, 8);
    const columns = state.range <= 30 ? state.range : state.range === 90 ? 13 : 26;
    const unitDays = state.range <= 30 ? 1 : 7;
    const now = Date.now();
    const labelEvery = columns > 32 ? 6 : columns > 16 ? 3 : columns > 10 ? 2 : 1;

    let html = `
      <div class="grid gap-1 items-center" style="grid-template-columns: 180px repeat(${columns}, minmax(12px, 1fr));">
        <div class="text-[11px] uppercase tracking-[0.12em] text-slate-500 pr-2">Repo</div>
    `;

    for (let col = 0; col < columns; col += 1) {
      const cellDate = new Date(now - (columns - 1 - col) * unitDays * 86400000);
      const label = unitDays === 1 ? cellDate.getDate() : `W${col + 1}`;
      html += `<div class="text-center text-[10px] text-slate-500">${col % labelEvery === 0 ? label : ""}</div>`;
    }

    if (repos.length === 0) {
      html += `</div><p class="text-xs text-slate-500 mt-3">No repositories match the current filter.</p>`;
      root.innerHTML = html;
      return;
    }

    repos.forEach(repo => {
      const { series, isLive } = getRepoDailySeries(repo, columns * unitDays);
      const cellValues = [];
      for (let col = 0; col < columns; col += 1) {
        const chunk = series.slice(col * unitDays, (col + 1) * unitDays);
        cellValues.push(sumSeries(chunk, "commits"));
      }
      const maxValue = Math.max(...cellValues, 1);

      html += `
        <a href="${escapeHtml(safeUrl(repo.url))}" target="_blank" rel="noreferrer noopener" class="text-xs text-slate-300 hover:text-teal-200 truncate pr-2 flex items-center gap-1.5" title="${escapeHtml(repo.name)}">
          ${isLive ? '<span class="text-emerald-300" title="Live GitHub data">●</span>' : ""}
          <span class="truncate">${escapeHtml(repo.repoPath.replace(/^kaspanet\//, ""))}</span>
        </a>
      `;

      cellValues.forEach(value => {
        const alpha = value <= 0 ? 0.05 : Math.min(0.92, 0.16 + (value / maxValue) * 0.76);
        const background = value <= 0 ? "rgba(255,255,255,0.05)" : `rgba(45,212,191,${alpha})`;
        html += `<div class="heatmap-cell h-4 rounded-[5px]" style="background:${background}" title="${escapeHtml(repo.name)}: ${value} commit${value === 1 ? "" : "s"}"></div>`;
      });
    });

    html += `</div>`;
    root.innerHTML = html;
  }

  const BUS_FACTOR_MIN_COMMITS = 5;      // ignore repos too new/quiet to say anything meaningful
  const BUS_FACTOR_SHARE_THRESHOLD = 0.85; // top contributor's share of identified commits

  /**
   * Single-maintainer risk for a repo, from real per-repo contributor data
   * (see topContributors/identifiedCommits in ingest_github_activity.py).
   * Returns null when there's no live data, too little identified commit
   * history to say anything meaningful, or the top contributor's share is
   * below threshold — never a modeled guess, since fabricating "who wrote
   * this" would be actively misleading rather than just incomplete.
   */
  function repoBusFactor(repo) {
    const liveRecord = LIVE.repos[repo.repoPath];
    const top = liveRecord?.topContributors?.[0];
    const identified = liveRecord?.identifiedCommits || 0;
    if (!top || identified < BUS_FACTOR_MIN_COMMITS) return null;
    const share = top.commits / identified;
    if (share < BUS_FACTOR_SHARE_THRESHOLD) return null;
    return { login: top.login, share, identifiedCommits: identified };
  }

  function liveIndicator(isLive) {
    return isLive
      ? `<span class="badge badge-emerald" title="Live GitHub data (ingested)">● Live</span>`
      : `<span class="badge badge-slate" title="Modeled placeholder data — not yet ingested">○ Modeled</span>`;
  }

  function busFactorBadge(repo) {
    const risk = repoBusFactor(repo);
    if (!risk) return "";
    const pct = Math.round(risk.share * 100);
    return `<span class="badge badge-amber" title="${escapeHtml(`@${risk.login} authored ${pct}% of this repo's identified commits`)}">⚠ Bus factor 1</span>`;
  }

  function repoRowMarkup(repo, metrics, color, variant) {
    const descMaxWidth = variant === "top" ? "max-w-[300px]" : "max-w-[320px]";
    return `
      <td>
        <a href="${escapeHtml(safeUrl(repo.url))}" target="_blank" rel="noreferrer noopener" class="block group">
          <span class="font-medium text-slate-100 group-hover:text-teal-200 transition">
            ${escapeHtml(repo.name)}
            ${variant === "top" ? (metrics.isLive ? '<span class="ml-1 text-emerald-300" title="Live GitHub data">●</span>' : "") : ""}
          </span>
          <span class="block text-xs text-slate-500 mt-1 ${descMaxWidth} truncate" title="${escapeHtml(repo.description)}">${escapeHtml(repo.description)}</span>
        </a>
      </td>
      <td>${categoryBadge(repo.category)}${variant === "full" ? ` ${liveIndicator(metrics.isLive)} ${busFactorBadge(repo)}` : ""}</td>
      ${variant === "full" ? `<td class="font-mono text-slate-300">Tier ${repo.tier}</td>` : ""}
      <td>${statusBadge(effectiveRepoStatus(repo).label)}</td>
      ${variant === "full" ? `<td class="text-right font-mono ${metrics.starsLive ? "text-emerald-200" : "text-slate-200"}" title="${metrics.starsLive ? "Live star count" : "Modeled placeholder"}">${formatNumber(metrics.stars)}</td>` : ""}
      <td class="text-right font-mono text-slate-200">${formatNumber(metrics.commits)}</td>
      <td class="text-right font-mono text-slate-200">${formatNumber(metrics.prs)}</td>
      ${variant === "full" ? `<td class="text-right font-mono text-slate-200">${formatNumber(metrics.issues)}</td>` : ""}
      <td class="text-right font-mono text-slate-200">${formatNumber(metrics.releases)}</td>
      ${variant === "full" ? `<td class="text-right font-mono text-slate-200">${formatNumber(metrics.activeDevs)}</td>` : ""}
      <td>${sparklineSvg(metrics.trend, color)}</td>
    `;
  }

  function renderTopRepos() {
    const tbody = document.getElementById("topRepoBody");
    if (!tbody) return;

    const rows = filteredRepos()
      .map(repo => ({ repo, metrics: repoMetrics(repo, state.range) }))
      .sort((a, b) => b.metrics.commits - a.metrics.commits)
      .slice(0, 8);

    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="text-center text-slate-500 py-6">No repositories match the current filter.</td></tr>`;
      return;
    }

    tbody.innerHTML = rows.map(({ repo, metrics }) => {
      const color = CATEGORY_META[repo.category]?.color || "#94a3b8";
      return `<tr>${repoRowMarkup(repo, metrics, color, "top")}</tr>`;
    }).join("");
  }

  function renderRepoTable() {
    const tbody = document.getElementById("repoTableBody");
    if (!tbody) return;

    let rows = filteredRepos({ query: state.repoQuery }).map(repo => ({
      repo,
      metrics: repoMetrics(repo, state.range)
    }));

    if (state.repoSort === "name") {
      rows.sort((a, b) => a.repo.name.localeCompare(b.repo.name));
    } else {
      rows.sort((a, b) => b.metrics[state.repoSort] - a.metrics[state.repoSort]);
    }

    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="11" class="text-center text-slate-500 py-6">No repositories match your search.</td></tr>`;
      return;
    }

    tbody.innerHTML = rows.map(({ repo, metrics }) => {
      const color = CATEGORY_META[repo.category]?.color || "#94a3b8";
      return `<tr>${repoRowMarkup(repo, metrics, color, "full")}</tr>`;
    }).join("");
  }

  function renderTopContributors() {
    const root = document.getElementById("topContributorList");
    if (!root) return;

    const rows = contributorRows()
      .sort((a, b) => b.metrics.commits - a.metrics.commits)
      .slice(0, 6);

    const topCommits = rows[0]?.metrics.commits || 1;

    root.innerHTML = rows.map((row, index) => {
      const focus = Math.min(100, Math.round((row.metrics.commits / topCommits) * 100));
      const avatar = avatarMarkup(row.handle, "h-9 w-9 rounded-2xl", "text-[11px]");
      const nameLabel = row.htmlUrl
        ? `<a href="${escapeHtml(row.htmlUrl)}" target="_blank" rel="noopener noreferrer" class="text-sm font-medium text-slate-100 truncate font-mono hover:underline">${escapeHtml(row.handle)}</a>`
        : `<span class="text-sm font-medium text-slate-100 truncate font-mono">${escapeHtml(row.handle)}</span>`;
      const newBadge = row.metrics.isNew ? `<span class="badge badge-emerald text-[10px]">New</span>` : "";
      const prsLabel = row.metrics.prs === null ? "—" : `${formatNumber(row.metrics.prs)} PRs`;

      return `
        <div class="rounded-3xl border border-white/[0.06] bg-white/[0.02] p-3">
          <div class="flex items-center gap-3">
            ${avatar}
            <div class="min-w-0 flex-1">
              <div class="flex items-center justify-between gap-3">
                <span class="flex items-center gap-1.5 min-w-0">${nameLabel}${newBadge}</span>
                <span class="text-xs text-slate-400">#${index + 1}</span>
              </div>
              <div class="mt-2 h-1.5 rounded-full bg-white/[0.06] overflow-hidden">
                <div class="h-full rounded-full" style="width:${focus}%; background:linear-gradient(90deg,#2dd4bf,#38bdf8)"></div>
              </div>
              <div class="mt-2 flex items-center justify-between text-xs text-slate-400">
                <span>${formatNumber(row.metrics.commits)} commits</span>
                <span>${prsLabel}</span>
              </div>
            </div>
          </div>
        </div>
      `;
    }).join("");
  }

  function renderContributorTable() {
    const tbody = document.getElementById("contributorTableBody");
    if (!tbody) return;

    const subtitleEl = document.getElementById("contributorSubtitle");
    if (subtitleEl) {
      subtitleEl.textContent = LIVE.contributors.length
        ? `Real GitHub commit authors from ${LIVE.contributors.length} tracked contributors, aggregated across ingested repos. Commit counts are cumulative over the ingestion lookback window, not the range selector above — PRs/Reviews/Issues aren't attributed per-author yet, shown as "—".`
        : "Placeholder contributor analytics — no ingestion data yet. Run the Ingest GitHub Activity workflow to populate real contributors.";
    }

    const normalizedQuery = state.contributorQuery.trim().toLowerCase();

    const rows = contributorRows()
      .filter(row => !normalizedQuery || row.handle.toLowerCase().includes(normalizedQuery))
      .sort((a, b) => b.metrics.commits - a.metrics.commits);

    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-slate-500 py-6">No contributors match your search.</td></tr>`;
      return;
    }

    tbody.innerHTML = rows.map((row, index) => {
      const lastActiveLabel = row.metrics.lastActive === null ? "—"
        : row.metrics.lastActive === 0 ? "Today" : `${row.metrics.lastActive}d ago`;
      const avatar = avatarMarkup(row.handle, "h-8 w-8 rounded-xl", "text-[10px]");
      const nameLabel = row.htmlUrl
        ? `<a href="${escapeHtml(row.htmlUrl)}" target="_blank" rel="noopener noreferrer" class="font-mono text-slate-100 hover:underline">${escapeHtml(row.handle)}</a>`
        : `<span class="font-mono text-slate-100">${escapeHtml(row.handle)}</span>`;
      const newBadge = row.metrics.isNew ? `<span class="badge badge-emerald text-[10px]">New</span>` : "";
      const cell = (value) => value === null ? `<span class="text-slate-600">—</span>` : formatNumber(value);

      return `
        <tr>
          <td class="text-slate-500 font-mono">${index + 1}</td>
          <td>
            <div class="flex items-center gap-3">
              ${avatar}
              <span class="flex items-center gap-2">
                ${nameLabel}
                ${row.isLive ? `<span class="h-1.5 w-1.5 rounded-full bg-emerald-400" title="Live GitHub data"></span>` : ""}
                ${newBadge}
              </span>
            </div>
          </td>
          <td class="text-right font-mono text-slate-200">${formatNumber(row.metrics.commits)}</td>
          <td class="text-right font-mono text-slate-200">${cell(row.metrics.prs)}</td>
          <td class="text-right font-mono text-slate-200">${cell(row.metrics.reviews)}</td>
          <td class="text-right font-mono text-slate-200">${cell(row.metrics.issues)}</td>
          <td class="text-right font-mono text-slate-200">${formatNumber(row.metrics.repos)}</td>
          <td>${lastActiveLabel}</td>
          <td>
            <div class="w-24 h-1.5 rounded-full bg-white/[0.06] overflow-hidden">
              <div class="h-full rounded-full" style="width:${Math.min(100, Math.round(row.metrics.commits / (rows[0]?.metrics.commits || 1) * 100))}%; background:linear-gradient(90deg,#2dd4bf,#38bdf8)"></div>
            </div>
          </td>
        </tr>
      `;
    }).join("");
  }

  function renderRegistryTable() {
    const tbody = document.getElementById("registryTableBody");
    if (!tbody) return;

    const normalizedQuery = state.registryQuery.trim().toLowerCase();

    const rows = REGISTRY_ROWS.filter(row => {
      const matchesStatus = state.registryStatus === "All" || effectiveRepoStatus(row).label === state.registryStatus;
      const matchesQuery =
        !normalizedQuery ||
        row.name.toLowerCase().includes(normalizedQuery) ||
        row.description.toLowerCase().includes(normalizedQuery) ||
        row.category.toLowerCase().includes(normalizedQuery) ||
        row.tags.some(tag => tag.toLowerCase().includes(normalizedQuery));
      return matchesStatus && matchesQuery;
    });

    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="text-center text-slate-500 py-6">No registry entries match your search.</td></tr>`;
      return;
    }

    tbody.innerHTML = rows.map(row => `
      <tr>
        <td>
          <a href="${escapeHtml(safeUrl(row.url))}" target="_blank" rel="noreferrer noopener" class="block group">
            <span class="font-medium text-slate-100 group-hover:text-teal-200 transition font-mono">${escapeHtml(row.name)}</span>
            <span class="block text-xs text-slate-500 mt-1 max-w-[360px] truncate" title="${escapeHtml(row.description)}">${escapeHtml(row.description)}</span>
          </a>
        </td>
        <td>${row.type === "Org" ? `<span class="badge badge-cyan">Org</span>` : `<span class="badge badge-slate">Repo</span>`}</td>
        <td>${categoryBadge(row.category)}</td>
        <td class="font-mono text-slate-300">Tier ${row.tier}</td>
        <td>${statusBadge(effectiveRepoStatus(row).label)}</td>
        <td>
          <div class="flex flex-wrap gap-1.5">
            ${row.tags.map(tag => `<span class="badge badge-slate">${escapeHtml(tag)}</span>`).join("")}
          </div>
        </td>
      </tr>
    `).join("");
  }

  function renderTabs() {
    const html = TABS.map(tab => `
      <button class="tab-btn" role="tab" aria-selected="${state.view === tab.id}"
        aria-controls="view-${tab.id}" id="tabbtn-${tab.id}" data-tab-button="${tab.id}">${escapeHtml(tab.label)}</button>
    `).join("");

    const navEl = document.getElementById("tabNav");
    const navMobileEl = document.getElementById("tabNavMobile");
    navEl.setAttribute("role", "tablist");
    navMobileEl.setAttribute("role", "tablist");
    navEl.innerHTML = html;
    navMobileEl.innerHTML = html;
  }

  function renderCategoryControls() {
    const chipsRoot = document.getElementById("categoryChips");
    const selectRoot = document.getElementById("repoCategorySelect");

    const categoriesPresent = Array.from(new Set(REPOS.map(r => r.category)))
      .sort((a, b) => a.localeCompare(b));
    const categories = ["All", ...categoriesPresent];

    chipsRoot.innerHTML = categories.map(category => `
      <button class="chip ${state.category === category ? "chip-active" : ""}" aria-pressed="${state.category === category}" data-category="${escapeHtml(category)}">
        ${escapeHtml(category)}
      </button>
    `).join("");

    selectRoot.innerHTML = categories.map(category => `
      <option value="${escapeHtml(category)}" ${state.category === category ? "selected" : ""}>${escapeHtml(category)}</option>
    `).join("");
  }

  function renderRegistryStatusControl() {
    const selectRoot = document.getElementById("registryStatus");
    if (!selectRoot) return;

    // Computed from effectiveRepoStatus (live-first), not the raw registry
    // CSV status — otherwise a repo showing "Active" in the table wouldn't
    // actually appear when filtering by "Active", since almost every repo's
    // raw CSV status differs from its live-corrected one.
    const statusesPresent = Array.from(new Set(REGISTRY_ROWS.map(r => effectiveRepoStatus(r).label))).sort();
    const statuses = ["All", ...statusesPresent];

    selectRoot.innerHTML = statuses.map(status => `
      <option value="${escapeHtml(status)}" ${state.registryStatus === status ? "selected" : ""}>${escapeHtml(status)}</option>
    `).join("");
  }

  function renderRangeButtons() {
    document.querySelectorAll("[data-range]").forEach(button => {
      const range = Number(button.dataset.range);
      const active = range === state.range;
      button.classList.toggle("range-active", active);
      button.setAttribute("aria-pressed", String(active));
    });

    const label = document.getElementById("activityRangeLabel");
    if (label) {
      label.textContent = state.range === 365 ? "1Y" : `${state.range}D`;
    }
  }

  function switchView(viewId) {
    if (!TABS.some(tab => tab.id === viewId)) return;
    state.view = viewId;

    document.querySelectorAll(".view").forEach(section => {
      section.classList.toggle("hidden", section.id !== `view-${viewId}`);
    });

    document.querySelectorAll("[data-tab-button]").forEach(button => {
      const active = button.dataset.tabButton === viewId;
      button.classList.toggle("tab-active", active);
      button.setAttribute("aria-selected", String(active));
    });

    if (viewId === "overview") {
      setTimeout(() => {
        activityChart?.resize();
        categoryChart?.resize();
      }, 40);
    }

    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function updateLastUpdated() {
    const now = new Date();
    const el = document.getElementById("lastUpdated");
    if (el) {
      el.textContent = now.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
    }
  }

  const CONFIDENCE_RANK = { High: 5, "Medium-High": 4, Medium: 3, "Low-Medium": 2, Low: 1 };
  const START_HERE_CATEGORIES = ["Core", "Wallet", "Explorer", "Programmability", "SDK", "Mining", "Docs"];

  function timeAgoLabel(isoDate) {
    const then = new Date(isoDate).getTime();
    if (Number.isNaN(then)) return "";
    const diffMs = Date.now() - then;
    const days = Math.floor(diffMs / 86400000);
    if (days <= 0) return "Today";
    if (days === 1) return "Yesterday";
    if (days < 30) return `${days}d ago`;
    const months = Math.floor(days / 30);
    return `${months}mo ago`;
  }

  function renderWeeklyDigest() {
    const badgeEl = document.getElementById("weeklyDataBadge");
    const moversEl = document.getElementById("weeklyMovers");
    const releasesEl = document.getElementById("weeklyReleases");
    const newBuildersEl = document.getElementById("weeklyNewBuilders");
    if (!moversEl || !releasesEl) return;

    const repos = filteredRepos();
    const movers = repos.map(repo => {
      const { series, isLive } = getRepoDailySeries(repo, 14);
      const priorWeek = series.slice(0, 7);
      const recentWeek = series.slice(7, 14);
      const priorSum = sumSeries(priorWeek, "commits");
      const recentSum = sumSeries(recentWeek, "commits");
      return { repo, priorSum, recentSum, delta: recentSum - priorSum, isLive };
    })
      .filter(item => item.recentSum > 0 || item.priorSum > 0)
      .sort((a, b) => b.delta - a.delta)
      .slice(0, 5);

    if (badgeEl) {
      const liveMoverCount = movers.filter(item => item.isLive).length;
      badgeEl.className = liveMoverCount > 0 ? "badge badge-emerald" : "badge badge-slate";
      badgeEl.textContent = liveMoverCount > 0 ? `${liveMoverCount} live` : "Modeled";
    }

    moversEl.innerHTML = movers.length
      ? movers.map(item => {
          const positive = item.delta >= 0;
          return `
            <a href="${escapeHtml(safeUrl(item.repo.url))}" target="_blank" rel="noreferrer noopener"
               class="flex items-center justify-between gap-2 rounded-xl border border-white/[0.06] bg-white/[0.02] px-3 py-2 hover:bg-white/[0.04] transition">
              <span class="text-xs text-slate-200 truncate flex items-center gap-1.5">
                ${item.isLive ? '<span class="text-emerald-300" title="Live GitHub data">●</span>' : ""}
                ${escapeHtml(item.repo.name)}
              </span>
              <span class="text-xs font-mono ${positive ? "text-emerald-300" : "text-rose-300"} shrink-0">
                ${positive ? "+" : ""}${item.delta} commits
              </span>
            </a>
          `;
        }).join("")
      : `<p class="text-xs text-slate-500">No commit activity in the current filter.</p>`;

    const releases = (LIVE.releases || [])
      .filter(item => REPOS.some(repo => repo.repoPath === item.repo))
      .slice(0, 6);

    releasesEl.innerHTML = releases.length
      ? releases.map(item => `
          <a href="${escapeHtml(safeUrl(item.url || `https://github.com/${item.repo}`))}" target="_blank" rel="noreferrer noopener"
             class="block rounded-xl border border-white/[0.06] bg-white/[0.02] px-3 py-2 hover:bg-white/[0.04] transition">
            <div class="flex items-center justify-between gap-2">
              <span class="text-xs text-slate-200 truncate">${escapeHtml(item.repo)}</span>
              <span class="text-[10px] text-slate-500 shrink-0">${escapeHtml(timeAgoLabel(item.publishedAt))}</span>
            </div>
            <span class="text-xs text-teal-200 font-mono truncate block mt-0.5">${escapeHtml(item.name)}</span>
          </a>
        `).join("")
      : `<p class="text-xs text-slate-500">No releases ingested yet — this fills in once the ingestion workflow has run (see Methodology).</p>`;

    if (newBuildersEl) {
      // Anyone whose first-ever tracked commit (within the ingestion lookback
      // window) landed in the last NEW_CONTRIBUTOR_WINDOW_DAYS days. Live-data
      // only — there's no honest way to model "someone is new" for a repo that
      // hasn't been ingested yet, so this stays empty rather than guessing.
      const newBuilders = contributorRows()
        .filter(row => row.isLive && row.metrics.isNew)
        .sort((a, b) => new Date(b.metrics.firstCommitAt) - new Date(a.metrics.firstCommitAt))
        .slice(0, 5);

      newBuildersEl.innerHTML = newBuilders.length
        ? newBuilders.map(row => {
            const avatar = avatarMarkup(row.handle, "h-7 w-7 rounded-lg", "text-[10px]");
            const nameLabel = row.htmlUrl
              ? `<a href="${escapeHtml(row.htmlUrl)}" target="_blank" rel="noreferrer noopener" class="text-xs font-mono text-slate-100 hover:underline truncate">${escapeHtml(row.handle)}</a>`
              : `<span class="text-xs font-mono text-slate-100 truncate">${escapeHtml(row.handle)}</span>`;
            const repoName = (row.metrics.firstCommitRepo || "").split("/")[1] || row.metrics.firstCommitRepo || "";
            return `
              <div class="flex items-center gap-2 rounded-xl border border-white/[0.06] bg-white/[0.02] px-3 py-2">
                ${avatar}
                <div class="min-w-0 flex-1">
                  <div class="flex items-center justify-between gap-2">
                    ${nameLabel}
                    <span class="text-[10px] text-slate-500 shrink-0">${escapeHtml(timeAgoLabel(row.metrics.firstCommitAt))}</span>
                  </div>
                  <span class="text-[10px] text-slate-500 truncate block">first commit → ${escapeHtml(repoName)}</span>
                </div>
              </div>
            `;
          }).join("")
        : `<p class="text-xs text-slate-500">No new contributors detected in the last ${NEW_CONTRIBUTOR_WINDOW_DAYS} days of ingested history.</p>`;
    }
  }

  function renderStartHere() {
    const root = document.getElementById("startHereList");
    if (!root) return;

    const picks = START_HERE_CATEGORIES.map(category => {
      const candidates = REPOS.filter(repo => repo.category === category && effectiveRepoStatus(repo).label !== "Archived" && effectiveRepoStatus(repo).label !== "Deprecated");
      if (!candidates.length) return null;

      const best = [...candidates].sort((a, b) => {
        const statusRank = (repo) => (effectiveRepoStatus(repo).label === "Active" ? 1 : 0);
        if (statusRank(b) !== statusRank(a)) return statusRank(b) - statusRank(a);
        const confDiff = (CONFIDENCE_RANK[b.confidence] || 0) - (CONFIDENCE_RANK[a.confidence] || 0);
        if (confDiff !== 0) return confDiff;
        return a.tier - b.tier;
      })[0];

      return { category, repo: best };
    }).filter(Boolean);

    root.innerHTML = picks.length
      ? picks.map(({ category, repo }) => `
          <a href="${escapeHtml(safeUrl(repo.url))}" target="_blank" rel="noreferrer noopener"
             class="block rounded-xl border border-white/[0.06] bg-white/[0.02] px-3 py-2 hover:bg-white/[0.04] transition">
            <div class="flex items-center justify-between gap-2">
              ${categoryBadge(category)}
              ${statusBadge(effectiveRepoStatus(repo).label)}
            </div>
            <span class="text-sm text-slate-100 font-medium mt-1.5 block truncate">${escapeHtml(repo.name)}</span>
            <span class="text-xs text-slate-500 mt-0.5 block truncate" title="${escapeHtml(repo.description)}">${escapeHtml(repo.description)}</span>
          </a>
        `).join("")
      : `<p class="text-xs text-slate-500">No eligible repos found for the current filter.</p>`;
  }

  // Days since a repo's last commit, using the full ingested lookback window
  // (not clipped to the dashboard's range selector). Returns null for repos
  // with no live data yet — callers must not fabricate a value for those.
  function repoLastCommitDays(repo) {
    const liveRecord = LIVE.repos[repo.repoPath];
    if (!liveRecord || !Array.isArray(liveRecord.series) || !liveRecord.series.length) return null;
    let lastActiveDate = null;
    for (const day of liveRecord.series) {
      if (day.commits > 0) lastActiveDate = day.date;
    }
    if (!lastActiveDate) return null;
    const then = new Date(`${lastActiveDate}T00:00:00Z`).getTime();
    return Math.max(0, Math.floor((Date.now() - then) / 86400000));
  }

  const NEEDS_BUILDER_STALE_DAYS = 120;
  const NEEDS_BUILDER_MAX_ITEMS = 6;
  const NEEDS_BUILDER_THIN_COUNT = 4;

  /**
   * "Needs a Builder" — surfaces ecosystem gaps instead of just ranking what
   * already exists. Two signals, both derived from data already on the page
   * (no separate fetch, no manual curation):
   *   - thin: category has the fewest non-archived/deprecated registry rows.
   *   - stalled: category has live-ingested repos, and every single one of
   *     them hasn't committed in NEEDS_BUILDER_STALE_DAYS+ days. Categories
   *     with zero live-ingested repos can't earn this label — that would be
   *     mistaking "not ingested yet" for "abandoned", which isn't true.
   * A category can only appear once; "stalled" wins over "thin" when both
   * apply, since it's backed by real commit history rather than just a count.
   */
  function renderNeedsBuilder() {
    const badgeEl = document.getElementById("needsBuilderBadge");
    const root = document.getElementById("needsBuilderList");
    if (!root) return;

    const categories = Object.keys(CATEGORY_META).filter(cat => cat !== "All");

    const byCategory = categories.map(category => {
      const repos = REPOS.filter(repo =>
        repo.category === category && effectiveRepoStatus(repo).label !== "Archived" && effectiveRepoStatus(repo).label !== "Deprecated"
      );
      const withLastCommit = repos
        .map(repo => ({ repo, lastCommitDays: repoLastCommitDays(repo) }))
        .filter(item => item.lastCommitDays !== null)
        .sort((a, b) => b.lastCommitDays - a.lastCommitDays);

      const liveCount = withLastCommit.length;
      const staleOnly = liveCount > 0 && withLastCommit.every(item => item.lastCommitDays >= NEEDS_BUILDER_STALE_DAYS);

      return {
        category,
        repoCount: repos.length,
        liveCount,
        staleOnly,
        stalest: withLastCommit[0] || null
      };
    });

    const picks = new Map();
    [...byCategory]
      .sort((a, b) => a.repoCount - b.repoCount)
      .slice(0, NEEDS_BUILDER_THIN_COUNT)
      .forEach(item => picks.set(item.category, { ...item, reason: "thin" }));
    byCategory
      .filter(item => item.staleOnly)
      .forEach(item => picks.set(item.category, { ...item, reason: "stalled" }));

    const finalPicks = [...picks.values()].slice(0, NEEDS_BUILDER_MAX_ITEMS);
    const liveSignalTotal = byCategory.reduce((sum, item) => sum + item.liveCount, 0);

    if (badgeEl) {
      badgeEl.className = liveSignalTotal > 0 ? "badge badge-emerald" : "badge badge-slate";
      badgeEl.textContent = liveSignalTotal > 0
        ? `${liveSignalTotal} repo${liveSignalTotal === 1 ? "" : "s"} of live signal`
        : "Registry counts only — live signal pending ingestion";
    }

    root.innerHTML = finalPicks.length
      ? finalPicks.map(item => {
          const example = item.stalest?.repo;
          const label = item.reason === "stalled"
            ? `Only ${item.liveCount} tracked repo${item.liveCount === 1 ? "" : "s"} here — none has committed in ${Math.floor(item.stalest.lastCommitDays / 30)}+ months`
            : `Smallest tracked category — ${item.repoCount} repo${item.repoCount === 1 ? "" : "s"} total`;
          return `
            <div class="rounded-xl border border-white/[0.06] bg-white/[0.02] px-3 py-2.5">
              <div class="flex items-center justify-between gap-2 mb-1.5">
                ${categoryBadge(item.category)}
                <span class="text-[10px] uppercase tracking-wide font-medium ${item.reason === "stalled" ? "text-amber-300" : "text-slate-500"}">
                  ${item.reason === "stalled" ? "Stalled" : "Thin"}
                </span>
              </div>
              <p class="text-xs text-slate-300 leading-snug">${escapeHtml(label)}</p>
              ${example ? `
                <a href="${escapeHtml(safeUrl(example.url))}" target="_blank" rel="noreferrer noopener"
                   class="text-xs text-teal-200 hover:text-teal-100 mt-1.5 inline-block truncate max-w-full">
                  ${escapeHtml(example.name)} →
                </a>` : ""}
            </div>
          `;
        }).join("")
      : `<p class="text-xs text-slate-500 md:col-span-2 xl:col-span-3">Nothing stands out yet — every category has active coverage.</p>`;
  }

  /**
   * Idea Board — open "idea"-labeled issues on the dashboard's own repo,
   * fetched by loadIdeas(). Sorted by 👍 reaction count then recency (same
   * order the ingestion script already sorts in, re-sorted defensively here
   * in case a future data source doesn't). Live-data only: there's no
   * modeled fallback, since fabricating community submissions would be
   * actively misleading rather than just incomplete.
   */
  function renderIdeasView() {
    const badgeEl = document.getElementById("ideasDataBadge");
    const listEl = document.getElementById("ideasList");
    const ctaEl = document.getElementById("newIdeaLink");
    if (!listEl) return;

    const repoSlug = resolveRepoSlug();
    if (ctaEl) {
      if (repoSlug) {
        const title = encodeURIComponent("Idea: ");
        ctaEl.href = `https://github.com/${repoSlug}/issues/new?labels=idea&title=${title}`;
        ctaEl.classList.remove("hidden");
      } else {
        ctaEl.classList.add("hidden");
      }
    }

    const ideas = [...LIVE.ideas].sort((a, b) =>
      (b.thumbsUp - a.thumbsUp) || (new Date(b.createdAt) - new Date(a.createdAt))
    );

    if (badgeEl) {
      badgeEl.className = ideas.length ? "badge badge-emerald text-[11px]" : "badge badge-slate text-[11px]";
      badgeEl.textContent = ideas.length
        ? `${ideas.length} open idea${ideas.length === 1 ? "" : "s"}`
        : "No ideas ingested yet";
    }

    listEl.innerHTML = ideas.length
      ? ideas.map(idea => {
          const login = idea.author?.login || "unknown";
          const avatar = avatarMarkup(login, "h-7 w-7 rounded-lg", "text-[10px]");
          const labelBadges = (idea.labels || [])
            .filter(label => label !== IDEAS_LABEL)
            .slice(0, 3)
            .map(label => `<span class="badge badge-slate text-[10px]">${escapeHtml(label)}</span>`)
            .join("");

          return `
            <a href="${escapeHtml(safeUrl(idea.htmlUrl))}" target="_blank" rel="noreferrer noopener"
               class="block rounded-xl border border-white/[0.06] bg-white/[0.02] px-3 py-2.5 hover:bg-white/[0.04] transition">
              <div class="flex items-center justify-between gap-2 mb-1.5">
                <span class="flex items-center gap-2 min-w-0">
                  ${avatar}
                  <span class="text-xs font-mono text-slate-400 truncate">@${escapeHtml(login)}</span>
                </span>
                <span class="text-[10px] text-slate-500 shrink-0">${escapeHtml(timeAgoLabel(idea.createdAt))}</span>
              </div>
              <p class="text-sm font-medium text-slate-100 truncate">${escapeHtml(idea.title)}</p>
              ${idea.bodyExcerpt ? `<p class="text-xs text-slate-400 mt-1">${escapeHtml(idea.bodyExcerpt)}${idea.bodyExcerpt.length >= 240 ? "…" : ""}</p>` : ""}
              <div class="flex items-center justify-between gap-2 mt-2">
                <div class="flex items-center gap-3 text-[10px] text-slate-500">
                  <span>👍 ${formatNumber(idea.thumbsUp || 0)}</span>
                  <span>💬 ${formatNumber(idea.commentsCount || 0)}</span>
                </div>
                <div class="flex items-center gap-1.5">${labelBadges}</div>
              </div>
            </a>
          `;
        }).join("")
      : `<p class="text-xs text-slate-500 md:col-span-2">
           No open ideas yet. ${repoSlug ? `Be the first — file an issue labeled <span class="font-mono text-teal-200">idea</span> and it'll show up here on the next ingestion run.` : "Once someone opens an issue labeled idea on this repo, it'll show up here."}
         </p>`;
  }

  const BUS_FACTOR_WATCH_MAX_ITEMS = 6;

  /**
   * "Bus Factor Watch" — surfaces repos where one person authored 85%+ of
   * identified commits, using repoBusFactor() (real per-repo contributor
   * data, live-ingested repos only). Sorted by identifiedCommits descending
   * so the most active-yet-solo repos surface first — a quiet, barely-touched
   * repo being bus-factor-1 is far less notable than a busy one being that way.
   */
  function renderBusFactorWatch() {
    const badgeEl = document.getElementById("busFactorBadge");
    const root = document.getElementById("busFactorList");
    if (!root) return;

    const liveRepoCount = REPOS.filter(repo => LIVE.repos[repo.repoPath]).length;

    const flagged = REPOS
      .filter(repo => effectiveRepoStatus(repo).label !== "Archived" && effectiveRepoStatus(repo).label !== "Deprecated")
      .map(repo => ({ repo, risk: repoBusFactor(repo) }))
      .filter(item => item.risk)
      .sort((a, b) => b.risk.identifiedCommits - a.risk.identifiedCommits)
      .slice(0, BUS_FACTOR_WATCH_MAX_ITEMS);

    if (badgeEl) {
      badgeEl.textContent = liveRepoCount > 0
        ? `${flagged.length} flagged of ${liveRepoCount} live repos`
        : "Live signal pending ingestion";
    }

    root.innerHTML = flagged.length
      ? flagged.map(({ repo, risk }) => {
          const pct = Math.round(risk.share * 100);
          return `
            <a href="${escapeHtml(safeUrl(repo.url))}" target="_blank" rel="noreferrer noopener"
               class="block rounded-xl border border-white/[0.06] bg-white/[0.02] px-3 py-2.5 hover:bg-white/[0.04] transition">
              <div class="flex items-center justify-between gap-2 mb-1">
                ${categoryBadge(repo.category)}
                <span class="text-[10px] uppercase tracking-wide font-medium text-amber-300">${pct}%</span>
              </div>
              <p class="text-sm font-medium text-slate-100 truncate">${escapeHtml(repo.name)}</p>
              <p class="text-xs text-slate-400 mt-1">@${escapeHtml(risk.login)} authored ${pct}% of ${formatNumber(risk.identifiedCommits)} identified commits</p>
            </a>
          `;
        }).join("")
      : `<p class="text-xs text-slate-500 md:col-span-2 xl:col-span-3">${
          liveRepoCount > 0
            ? "No single-maintainer risk detected among live-ingested repos."
            : "Needs live ingestion data to compute — see Methodology."
        }</p>`;
  }

  function renderLiveBadge() {
    const el = document.getElementById("liveDataBadge");
    if (!el) return;

    const liveCount = Object.keys(LIVE.repos).length;
    if (liveCount > 0) {
      const ingestedAt = LIVE.meta?.generatedAt
        ? new Date(LIVE.meta.generatedAt).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
        : null;
      el.className = "badge badge-emerald";
      el.textContent = `${liveCount} repo${liveCount === 1 ? "" : "s"} live${ingestedAt ? ` · synced ${ingestedAt}` : ""}`;
    } else {
      el.className = "badge badge-slate";
      el.textContent = "Modeled — ingestion pending first run";
    }
  }

  function updateDashboard() {
    // These caches are only valid within a single render pass — state (range,
    // category, nonce) may change on the next call, so start clean each time.
    seriesCache = new Map();
    ecosystemSeriesCache = new Map();
    statusCache = new Map();

    renderRangeButtons();
    renderCategoryControls();
    renderRegistryStatusControl();
    renderKpis();
    renderActivityChart();
    renderCategoryChart();
    renderHeatmap();
    renderTopRepos();
    renderRepoTable();
    renderTopContributors();
    renderContributorTable();
    renderRegistryTable();
    renderWeeklyDigest();
    renderStartHere();
    renderNeedsBuilder();
    renderBusFactorWatch();
    renderIdeasView();
    renderLiveBadge();
    updateLastUpdated();
  }

  /**
   * Fetches the ingestion output (data/activity/_repos.json, _meta.json,
   * and each referenced per-repo series file) written by
   * .github/workflows/ingest-activity.yml. Safe to call before that
   * workflow has ever run — every fetch failure is caught and the
   * dashboard simply keeps using modeled placeholder data.
   */
  async function loadLiveActivity() {
    try {
      const [repoIndexRes, metaRes] = await Promise.all([
        fetch("data/activity/_repos.json", { cache: "no-store" }),
        fetch("data/activity/_meta.json", { cache: "no-store" })
      ]);

      const repoIndex = repoIndexRes.ok ? await repoIndexRes.json() : {};
      LIVE.meta = metaRes.ok ? await metaRes.json() : null;
      // Repos ingestion actually attempted (via _meta.json's failed list) but
      // the GitHub API call itself errored on — see effectiveRepoStatus()'s
      // "Unreachable" status. Distinct from a repo simply outside live
      // ingestion scope, which isn't in this list at all.
      LIVE.failedRepoSet = new Set(Array.isArray(LIVE.meta?.failed) ? LIVE.meta.failed : []);

      const entries = Object.entries(repoIndex).filter(([repoPath]) =>
        REPOS.some(repo => repo.repoPath === repoPath)
      );

      const fetched = await Promise.all(entries.map(async ([repoPath, relativePath]) => {
        try {
          const res = await fetch(`data/${relativePath}`, { cache: "no-store" });
          if (!res.ok) return null;
          const payload = await res.json();
          return [repoPath, payload];
        } catch (err) {
          return null;
        }
      }));

      fetched.forEach(entry => {
        if (entry) LIVE.repos[entry[0]] = entry[1];
      });
    } catch (err) {
      console.warn("Live activity data unavailable — using modeled placeholders.", err);
    }
  }

  /**
   * Fetches data/feed/releases.json (written by the same ingestion
   * workflow) for the "Recent releases" panel on Overview. Safe to call
   * before that workflow has ever run.
   */
  async function loadReleaseFeed() {
    try {
      const res = await fetch("data/feed/releases.json", { cache: "no-store" });
      if (!res.ok) return;
      const payload = await res.json();
      LIVE.releases = Array.isArray(payload.releases) ? payload.releases : [];
    } catch (err) {
      console.warn("Release feed unavailable.", err);
    }
  }

  /**
   * Fetches data/activity/_contributors.json — real GitHub commit authors
   * aggregated across every ingested repo (see ingest_github_activity.py).
   * Safe to call before that workflow has ever run; LIVE.contributors stays
   * empty and the Contributors tab falls back to modeled placeholder handles.
   */
  async function loadLiveContributors() {
    try {
      const res = await fetch("data/activity/_contributors.json", { cache: "no-store" });
      if (!res.ok) return;
      const payload = await res.json();
      LIVE.contributors = Array.isArray(payload.contributors) ? payload.contributors : [];
    } catch (err) {
      console.warn("Live contributor data unavailable — using modeled placeholders.", err);
    }
  }

  /**
   * Fetches data/activity/ideas.json — open "idea"-labeled issues on the
   * dashboard's own repo (see IDEAS_LABEL in ingest_github_activity.py).
   * Safe to call before that workflow has ever run; LIVE.ideas stays empty
   * and the Ideas tab shows an empty state rather than fabricated content.
   */
  async function loadIdeas() {
    try {
      const res = await fetch("data/activity/ideas.json", { cache: "no-store" });
      if (!res.ok) return;
      const payload = await res.json();
      LIVE.ideas = Array.isArray(payload.ideas) ? payload.ideas : [];
    } catch (err) {
      console.warn("Idea board data unavailable.", err);
    }
  }

  /**
   * "owner/repo" for wherever this dashboard is actually hosted, used to
   * build the "suggest an idea" new-issue link. Only auto-detectable on the
   * default *.github.io Pages URL (owner from the subdomain, repo from the
   * first path segment) — set KASGIT_REPO_OVERRIDE above for a custom
   * domain. Returns null rather than guessing wrong.
   */
  function resolveRepoSlug() {
    if (KASGIT_REPO_OVERRIDE) return KASGIT_REPO_OVERRIDE;
    const match = window.location.hostname.match(/^([^.]+)\.github\.io$/i);
    if (!match) return null;
    const owner = match[1];
    const repo = window.location.pathname.split("/").filter(Boolean)[0];
    return repo ? `${owner}/${repo}` : null;
  }

  function exportRepoJson() {
    const rows = filteredRepos({ query: state.repoQuery }).map(repo => {
      const metrics = repoMetrics(repo, state.range);
      return {
        repository: repo.name,
        url: repo.url,
        category: repo.category,
        status: effectiveRepoStatus(repo).label,
        selectedRangeDays: state.range,
        // isLive: true means commits/prs/issues/releases/activeDevs came from the
        // real GitHub API ingestion, not the deterministic modeled generator —
        // see data/activity/README.md. starsLive covers the stars field
        // independently, since a repo's activity and star data can be live on
        // different schedules.
        isLive: metrics.isLive,
        starsLive: metrics.starsLive,
        metrics: {
          commits: metrics.commits,
          prs: metrics.prs,
          issues: metrics.issues,
          releases: metrics.releases,
          activeDevs: metrics.activeDevs,
          stars: metrics.stars
        }
      };
    });

    const blob = new Blob([JSON.stringify(rows, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "kaspa-phase1-repositories.json";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  }

  function bindEvents() {
    document.addEventListener("click", event => {
      const tabButton = event.target.closest("[data-tab-button]");
      if (tabButton) {
        switchView(tabButton.dataset.tabButton);
        return;
      }

      const gotoButton = event.target.closest("[data-goto]");
      if (gotoButton) {
        switchView(gotoButton.dataset.goto);
        return;
      }

      const rangeButton = event.target.closest("[data-range]");
      if (rangeButton) {
        state.range = Number(rangeButton.dataset.range);
        updateDashboard();
        return;
      }

      const categoryChip = event.target.closest("[data-category]");
      if (categoryChip) {
        state.category = categoryChip.dataset.category;
        updateDashboard();
      }
    });

    document.getElementById("repoCategorySelect")?.addEventListener("change", event => {
      state.category = event.target.value;
      updateDashboard();
    });

    document.getElementById("repoSort")?.addEventListener("change", event => {
      state.repoSort = event.target.value;
      renderRepoTable();
    });

    document.getElementById("repoSearch")?.addEventListener("input", debounce(event => {
      state.repoQuery = event.target.value;
      renderRepoTable();
    }));

    document.getElementById("contributorSearch")?.addEventListener("input", debounce(event => {
      state.contributorQuery = event.target.value;
      renderContributorTable();
    }));

    document.getElementById("registrySearch")?.addEventListener("input", debounce(event => {
      state.registryQuery = event.target.value;
      renderRegistryTable();
    }));

    document.getElementById("registryStatus")?.addEventListener("change", event => {
      state.registryStatus = event.target.value;
      renderRegistryTable();
    });

    document.getElementById("exportReposBtn")?.addEventListener("click", exportRepoJson);

    document.getElementById("refreshBtn")?.addEventListener("click", () => {
      const icon = document.getElementById("refreshIcon");
      if (icon) {
        icon.classList.remove("spin-once");
        void icon.offsetWidth;
        icon.classList.add("spin-once");
      }

      state.nonce += 1;
      setTimeout(updateDashboard, 180);
    });

    window.addEventListener("resize", () => {
      activityChart?.resize();
      categoryChart?.resize();
    });
  }

  function initCharts() {
    if (!echartsAvailable) {
      ["activityChart", "categoryChart"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = `<div class="h-full w-full flex items-center justify-center text-xs text-slate-500">Chart library failed to load — check your network connection.</div>`;
      });
      return;
    }
    activityChart = echarts.init(document.getElementById("activityChart"), null, { renderer: "canvas" });
    categoryChart = echarts.init(document.getElementById("categoryChart"), null, { renderer: "canvas" });
  }

  async function boot() {
    renderTabs();
    initCharts();
    bindEvents();
    switchView("overview");
    updateDashboard(); // paint immediately with modeled fallback data

    await Promise.all([loadLiveActivity(), loadReleaseFeed(), loadLiveContributors(), loadIdeas()]);
    updateDashboard(); // repaint with any live-ingested data merged in
  }

  boot().catch(err => {
    console.error("Dashboard failed to initialize:", err);
    showFatalError("Something went wrong while rendering the dashboard. Check the browser console for details.");
  });

  function showFatalError(message) {
    const banner = document.getElementById("loadErrorBanner");
    if (banner) {
      banner.textContent = message;
      banner.classList.remove("hidden");
    } else {
      console.error(message);
    }
  }
})();
