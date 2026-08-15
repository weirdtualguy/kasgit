"""Regression tests for the two correctness bugs fixed in
ingest_github_activity.py:

1. Ecosystem-wide activeDevs used to be max(per-repo activeDevs) instead of
   the true distinct-author count across repos (accumulate_summary_day /
   finalize_summary_series).
2. Star history backfill never distinguished a fully-walked stargazer list
   from one truncated by STARGAZER_BACKFILL_MAX_PAGES or a mid-walk error
   (backfill_star_history's returned quality).

No network access — SESSION.get is monkeypatched with fake responses.
"""
from collections import defaultdict

import pytest

import ingest_github_activity as ing


# ---------------------------------------------------------------------
# activeDevs: ecosystem-wide union, not max-of-repos
# ---------------------------------------------------------------------

def new_summary_days():
    return defaultdict(lambda: {"commits": 0, "prs": 0, "issues": 0, "releases": 0, "_authors": set()})


def test_accumulate_summary_day_unions_authors_across_repos():
    summary_days = new_summary_days()

    # Repo A: 5 distinct authors active on 2026-01-01.
    ing.accumulate_summary_day(
        summary_days, "2026-01-01",
        {"commits": 5, "prs": 0, "issues": 0, "releases": 0},
        {"alice", "bob", "carol", "dave", "erin"},
    )
    # Repo B: 7 distinct authors the same day, 2 of whom (alice, bob) also
    # committed to repo A that day.
    ing.accumulate_summary_day(
        summary_days, "2026-01-01",
        {"commits": 7, "prs": 0, "issues": 0, "releases": 0},
        {"alice", "bob", "frank", "grace", "heidi", "ivan", "judy"},
    )

    series = ing.finalize_summary_series(summary_days)
    assert len(series) == 1
    day = series[0]

    # Union of the two sets above has 10 distinct members, not
    # max(5, 7) = 7 (the old bug) and not 5 + 7 = 12 (naive sum, which
    # would double-count alice and bob).
    assert day["activeDevs"] == 10
    assert day["commits"] == 12


def test_accumulate_summary_day_totals_are_additive_not_unioned():
    """Only activeDevs is a union — commits/prs/issues/releases are plain
    sums, and this test guards against accidentally applying union logic
    to the wrong fields."""
    summary_days = new_summary_days()
    ing.accumulate_summary_day(
        summary_days, "2026-01-01",
        {"commits": 3, "prs": 1, "issues": 2, "releases": 0}, {"alice"},
    )
    ing.accumulate_summary_day(
        summary_days, "2026-01-01",
        {"commits": 4, "prs": 2, "issues": 0, "releases": 1}, {"alice"},
    )
    day = ing.finalize_summary_series(summary_days)[0]
    assert day == {
        "date": "2026-01-01", "commits": 7, "prs": 3, "issues": 2,
        "releases": 1, "activeDevs": 1,
    }


def test_finalize_summary_series_sorted_by_date():
    summary_days = new_summary_days()
    ing.accumulate_summary_day(summary_days, "2026-01-02", {"commits": 1, "prs": 0, "issues": 0, "releases": 0}, {"x"})
    ing.accumulate_summary_day(summary_days, "2026-01-01", {"commits": 1, "prs": 0, "issues": 0, "releases": 0}, {"y"})
    series = ing.finalize_summary_series(summary_days)
    assert [d["date"] for d in series] == ["2026-01-01", "2026-01-02"]


def test_finalize_summary_series_empty_input():
    assert ing.finalize_summary_series(new_summary_days()) == []


# ---------------------------------------------------------------------
# Star history backfill quality (complete vs. partial)
# ---------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, payload, link_header=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"Link": link_header} if link_header else {}

    def json(self):
        return self._payload


def star_entry(day):
    return {"starred_at": f"{day}T00:00:00Z"}


@pytest.fixture
def fake_session(monkeypatch):
    calls = {"n": 0}

    def install(responses):
        def fake_get(url, params=None, headers=None, timeout=None):
            i = calls["n"]
            calls["n"] += 1
            return responses[min(i, len(responses) - 1)]
        monkeypatch.setattr(ing.SESSION, "get", fake_get)
        return calls

    return install


def test_backfill_complete_when_last_page_has_no_next_link(fake_session):
    """Fewer stargazers than the page cap: the last page comes back with
    no Link: rel="next" header, so the walk ends naturally -> complete."""
    responses = [
        FakeResponse(200, [star_entry("2025-01-01"), star_entry("2025-01-02")]),
    ]
    fake_session(responses)
    history, quality = ing.backfill_star_history("owner", "repo")
    assert quality == "complete"
    assert history[-1]["stars"] == 2


def test_backfill_partial_when_page_cap_hit_with_more_available(fake_session):
    """Every page up to STARGAZER_BACKFILL_MAX_PAGES comes back full with a
    Link: rel="next" header still pointing further — the walk was cut off
    with stargazers still unfetched -> partial."""
    link = '<https://api.github.com/repos/owner/repo/stargazers?page=2>; rel="next"'
    responses = [
        FakeResponse(200, [star_entry("2025-01-01")], link_header=link)
        for _ in range(ing.STARGAZER_BACKFILL_MAX_PAGES)
    ]
    fake_session(responses)
    history, quality = ing.backfill_star_history("owner", "repo")
    assert quality == "partial"
    assert len(history) >= 1


def test_backfill_partial_on_mid_walk_error(fake_session):
    """A transient/rate-limit error mid-walk should never be reported as
    complete — we can't know whether we reached the end."""
    responses = [FakeResponse(403, {"message": "rate limited"})]
    fake_session(responses)
    history, quality = ing.backfill_star_history("owner", "repo")
    assert quality == "partial"
    assert history == []


def test_backfill_complete_with_zero_stargazers(fake_session):
    """An empty first page (repo genuinely has zero stargazers) is a
    legitimate complete walk, not a partial one."""
    responses = [FakeResponse(200, [])]
    fake_session(responses)
    history, quality = ing.backfill_star_history("owner", "repo")
    assert quality == "complete"
    assert history == []


# ---------------------------------------------------------------------
# Bus factor: minimum contributors covering 50% of identified commits
# ---------------------------------------------------------------------

def contributor(login, commits):
    return {"login": login, "commits": commits}


def test_bus_factor_single_dominant_contributor():
    contributors = {"a": contributor("a", 90), "b": contributor("b", 10)}
    assert ing.compute_bus_factor(contributors) == 1


def test_bus_factor_requires_multiple_contributors_for_half():
    # 40, 35, 15, 10 -> cumulative 40, 75 crosses 50 (of 100) at the 2nd.
    contributors = {
        "a": contributor("a", 40), "b": contributor("b", 35),
        "c": contributor("c", 15), "d": contributor("d", 10),
    }
    assert ing.compute_bus_factor(contributors) == 2


def test_bus_factor_evenly_distributed():
    # 5 contributors at 20 each: need 3 to reach >= 50 of 100.
    contributors = {c: contributor(c, 20) for c in "abcde"}
    assert ing.compute_bus_factor(contributors) == 3


def test_bus_factor_none_when_no_identified_commits():
    assert ing.compute_bus_factor({}) is None


def test_bus_factor_needs_all_contributors_to_reach_half():
    # 3 contributors, evenly split 34/33/33 out of 100: the top 2 alone
    # (34+33=67) already clear 50, so this should NOT need all 3.
    contributors = {"a": contributor("a", 34), "b": contributor("b", 33), "c": contributor("c", 33)}
    assert ing.compute_bus_factor(contributors) == 2

    # A single contributor is a trivial "needs all 1" case.
    assert ing.compute_bus_factor({"solo": contributor("solo", 42)}) == 1


# ---------------------------------------------------------------------
# Repo metadata enrichment: snapshot fields free on the repo response,
# and the separate /languages call
# ---------------------------------------------------------------------

def test_fetch_repo_snapshot_extracts_free_metadata(fake_session):
    payload = {
        "stargazers_count": 42, "forks_count": 7, "subscribers_count": 3,
        "open_issues_count": 5, "archived": False,
        "language": "Rust", "topics": ["blockchain", "kaspa"],
        "license": {"spdx_id": "MIT"}, "default_branch": "main",
        "homepage": "https://kaspa.org", "created_at": "2021-01-01T00:00:00Z",
        "pushed_at": "2026-08-01T00:00:00Z",
    }
    fake_session([FakeResponse(200, payload)])
    snap = ing.fetch_repo_snapshot("owner", "repo")
    assert snap["stars"] == 42
    assert snap["primaryLanguage"] == "Rust"
    assert snap["topics"] == ["blockchain", "kaspa"]
    assert snap["license"] == "MIT"
    assert snap["defaultBranch"] == "main"
    assert snap["homepage"] == "https://kaspa.org"


def test_fetch_repo_snapshot_treats_noassertion_license_as_none(fake_session):
    """GitHub returns spdx_id "NOASSERTION" for a repo with a LICENSE file
    GitHub couldn't confidently match to a known license — that's not a
    real license identifier and shouldn't be presented as one."""
    payload = {
        "stargazers_count": 1, "forks_count": 0, "open_issues_count": 0,
        "archived": False, "language": None, "topics": [],
        "license": {"spdx_id": "NOASSERTION"},
    }
    fake_session([FakeResponse(200, payload)])
    snap = ing.fetch_repo_snapshot("owner", "repo")
    assert snap["license"] is None


def test_fetch_repo_snapshot_handles_missing_license(fake_session):
    payload = {"stargazers_count": 1, "forks_count": 0, "open_issues_count": 0, "archived": False}
    fake_session([FakeResponse(200, payload)])
    snap = ing.fetch_repo_snapshot("owner", "repo")
    assert snap["license"] is None
    assert snap["topics"] == []


def test_fetch_repo_snapshot_404_returns_none(fake_session):
    fake_session([FakeResponse(404, {})])
    assert ing.fetch_repo_snapshot("owner", "repo") is None


def test_fetch_repo_languages_returns_byte_counts(fake_session):
    fake_session([FakeResponse(200, {"Rust": 120000, "TypeScript": 3400})])
    langs = ing.fetch_repo_languages("owner", "repo")
    assert langs == {"Rust": 120000, "TypeScript": 3400}


def test_fetch_repo_languages_returns_empty_dict_on_failure(fake_session):
    """Never None — callers sum languages.items() unconditionally."""
    fake_session([FakeResponse(404, {})])
    assert ing.fetch_repo_languages("owner", "repo") == {}

