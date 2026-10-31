"""The opportunity-report pilot on synthetic data: discovery with a fake Venice, verification with
fake web pages, the scored-file checks, and the report. Nothing here touches the network or a
database. Every URL uses a reserved .example domain."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gtm.pilot.common import DEFAULT_CRITERIA, load_criteria, read_costs, read_json
from gtm.pilot.discover import open_run, plan, run_discovery
from gtm.pilot.models import Criteria
from gtm.pilot.report import PHONE_RE, build_report, check_scored, scrub
from gtm.pilot.verify import FetchResult, page_date, verify_run

TODAY = date(2026, 10, 30)
CRITERIA = load_criteria(DEFAULT_CRITERIA)
USAGE = {"prompt_tokens": 10_000, "completion_tokens": 1_000}
USAGE_USD = 10_000 * 0.33 / 1e6 + 1_000 * 1.32 / 1e6 + 0.01


def url(name: str) -> str:
    return f"https://fsbo.example/listing/{name}"


def quote(name: str) -> str:
    return f"For sale by owner, no agents please: listing {name} in Torrance."


def cand(
    name: str,
    *,
    city: str = "Torrance",
    address: str | None = None,
    link: str | None = None,
    summary: str | None = None,
    representation: str = "no_agent_stated",
) -> dict[str, Any]:
    return {
        "signal": "fsbo_listing",
        "summary": summary or f"Owner is selling home {name} without an agent.",
        "property": {
            "address": address or f"{name} Ocean Ave",
            "city": city,
            "property_type": "single_family",
        },
        "representation": representation,
        "evidence": [{"url": link or url(name), "quote": quote(name), "date": None}],
        "missing_info": ["asking price"],
    }


def cite(name: str, date_: str | None = None) -> dict[str, Any]:
    return {
        "url": url(name),
        "title": f"Listing {name}",
        "date": date_,
        "content": f"... {quote(name)} ...",
    }


def body(
    cands: list[dict[str, Any]], cites: list[dict[str, Any]], usage: dict[str, int] | None = None
) -> dict[str, Any]:
    return {
        "id": "req-test",
        "model": CRITERIA.model,
        "usage": usage or USAGE,
        "choices": [{"message": {"content": json.dumps({"candidates": cands})}}],
        "venice_parameters": {"web_search_citations": cites},
    }


class FakeVenice:
    def __init__(self, *responses: tuple[int, dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, endpoint: str, payload: dict[str, Any], key: str
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append(payload)
        return self.responses.pop(0) if self.responses else (200, body([], [cite("filler")]))


class Interrupted(BaseException):
    """The program stops mid-request; nothing in the pilot can catch it."""


def discover(tmp: Path, venice: Any, label: str = "test", **kw: Any) -> Any:
    kw.setdefault("max_usd", 0.50)
    return run_discovery(
        CRITERIA,
        DEFAULT_CRITERIA.read_text(),
        tmp,
        label,
        execute=True,
        today=TODAY,
        post=venice,
        api_key="test-key",
        **kw,
    )


def page(text: str, published: str | None = "2026-10-20") -> FetchResult:
    meta = f'<meta property="article:published_time" content="{published}">' if published else ""
    return FetchResult(
        200,
        "final",
        f"<html><head>{meta}<script>ignored()</script></head><body><p>{text}</p></body></html>",
    )


class FakeWeb:
    def __init__(self, pages: dict[str, FetchResult]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    def __call__(self, link: str) -> FetchResult:
        self.fetched.append(link)
        return self.pages.get(link, FetchResult(404, link, "", "http_404"))


# --- discovery -----------------------------------------------------------------------------


def test_dry_run_sends_nothing_and_creates_no_folder(tmp_path: Path) -> None:
    venice = FakeVenice()
    result = run_discovery(
        CRITERIA, "", tmp_path, "dry", execute=False, max_usd=0.5, today=TODAY, post=venice
    )
    assert venice.calls == [] and list(tmp_path.iterdir()) == []
    assert [p.task.id for p in result.planned] == [
        "fsbo_south_bay",
        "fsbo_la_county",
        "owner_plans_to_sell",
        "business_relocating",
    ]  # the buyer search is off
    assert sum(p.worst_case_usd for p in result.planned) < 0.15


def test_requests_turn_on_web_search_and_citations(tmp_path: Path) -> None:
    venice = FakeVenice((200, body([], [cite("a")])))
    discover(tmp_path, venice, only=["fsbo_south_bay"])
    sent = venice.calls[0]
    assert sent["model"] == "deepseek-v4-1-flash-uncensored"
    assert sent["venice_parameters"] == {
        "enable_web_search": "on",
        "enable_web_citations": True,
        "include_venice_system_prompt": False,
    }
    assert sent["response_format"]["type"] == "json_schema"
    assert (
        "Torrance" in sent["messages"][0]["content"] and "{{" not in sent["messages"][0]["content"]
    )


def test_discovery_records_sources_costs_and_raw_files(tmp_path: Path) -> None:
    invented = cand("b", link="https://made-up.example/x")
    venice = FakeVenice((200, body([cand("a"), invented], [cite("a", "2026-10-21")])))
    result = discover(tmp_path, venice, only=["fsbo_south_bay"])
    run = result.run_dir
    records = read_json(run / "discovered.json")
    assert [r["discovery_checks"][0]["url_in_search_results"] for r in records] == [True, False]
    assert records[0]["discovery_checks"][0]["quote_in_search_result"] is True
    assert records[0]["discovery_checks"][0]["search_result_date"] == "2026-10-21"
    assert records[0]["provenance"]["request_usd"] == pytest.approx(USAGE_USD)
    [cost] = read_costs(run)
    assert cost["usd_basis"] == "reported_usage" and cost["usd"] == pytest.approx(USAGE_USD)
    assert (run / "raw/discovery-fsbo_south_bay-1.request.json").exists()
    assert read_json(run / "raw/discovery-fsbo_south_bay-1.json")["http_status"] == 200
    assert result.pending == ["fsbo_la_county", "owner_plans_to_sell", "business_relocating"]


def test_discovery_stops_when_no_web_sources_come_back(tmp_path: Path) -> None:
    venice = FakeVenice((200, body([cand("a")], [])))
    result = discover(tmp_path, venice)
    assert result.stopped_reason == "no_web_sources" and len(venice.calls) == 1
    assert read_json(result.run_dir / "discovered.json") == []


def test_budget_stops_before_a_request_could_pass_the_cap(tmp_path: Path) -> None:
    venice = FakeVenice()
    result = discover(tmp_path, venice, max_usd=0.03)
    assert len(venice.calls) == 1 and result.stopped_reason == "budget"


def test_resume_reuses_saved_responses_and_retries_failures(tmp_path: Path) -> None:
    first = FakeVenice((200, body([cand("a")], [cite("a")])), (500, {"error": "busy"}))
    run = discover(tmp_path, first, only=["fsbo_south_bay", "fsbo_la_county"]).run_dir
    again = FakeVenice((200, body([cand("b")], [cite("b")])))
    result = discover(tmp_path, again, resume=run)
    assert len(again.calls) == 3  # fsbo_la_county retried, two new searches; fsbo_south_bay reused
    assert result.reused == 1 and result.pending == []
    assert (run / "raw/discovery-fsbo_la_county-2.json").exists()
    assert [r["property"]["address"] for r in read_json(run / "discovered.json")] == [
        "a Ocean Ave",
        "b Ocean Ave",
    ]
    assert len(read_costs(run)) == 5 and result.spent_usd == pytest.approx(
        sum(c["usd"] for c in read_costs(run))
    )


def test_a_request_without_a_response_counts_as_spent(tmp_path: Path) -> None:
    def stops(endpoint: str, payload: dict[str, Any], key: str) -> tuple[int, dict[str, Any]]:
        raise Interrupted

    with pytest.raises(Interrupted):
        discover(tmp_path, stops, label="cut")
    [run] = tmp_path.glob("*/*-cut")
    discover(tmp_path, FakeVenice(), resume=run, only=["fsbo_south_bay"])
    costs = read_costs(run)
    assert costs[0]["usd_basis"] == "worst_case_estimate_no_response" and costs[0]["usd"] > 0.02
    assert (run / "raw/discovery-fsbo_south_bay-2.json").exists()


def test_resume_is_refused_once_verification_started(tmp_path: Path) -> None:
    run = discover(tmp_path, FakeVenice(), only=["fsbo_south_bay"]).run_dir
    verify_run(run, CRITERIA, TODAY, FakeWeb({}))
    with pytest.raises(SystemExit, match="verified already"):
        open_run(run)


def test_criteria_rules() -> None:
    raw = CRITERIA.model_dump()
    with pytest.raises(ValidationError, match="priority_areas"):
        Criteria.model_validate({**raw, "priority_areas": ["San Diego"]})
    with pytest.raises(ValueError, match="unknown search ids"):
        plan(CRITERIA, TODAY, only=["nope"])


# --- verification --------------------------------------------------------------------------

OUTCOMES = {
    # name: (candidate, page, expected status, expected reason or warning)
    "ok": (cand("ok"), page(quote("ok")), "verified", None),
    "noquote": (
        cand("noquote"),
        page("A different page entirely."),
        "rejected",
        "quote_not_found_on_source",
    ),
    "blocked": (
        cand("blocked"),
        FetchResult(403, None, "", "http_403"),
        "unverified",
        "source_unreachable_or_blocked",
    ),
    "gone": (cand("gone"), FetchResult(404, None, "", "http_404"), "rejected", "source_not_found"),
    "stale": (
        cand("stale"),
        page(quote("stale"), "2026-06-01"),
        "rejected",
        "source_older_than_lookback",
    ),
    "far": (
        cand("far", city="San Diego"),
        page(quote("far")),
        "rejected",
        "outside_configured_areas",
    ),
    "invented": (
        cand("invented", link="not-a-url"),
        None,
        "rejected",
        "evidence_not_from_search_results",
    ),
    "dup": (
        cand("dup", link=url("ok")),
        None,
        "duplicate",
        "duplicate_in_this_run",
    ),  # same page as "ok"
    "pend": (
        cand("pend"),
        page(quote("pend") + " Sale pending."),
        "verified",
        "source_mentions_sold_or_pending",
    ),
    "brok": (
        cand("brok"),
        page(quote("brok") + " Listed by Example Realty."),
        "verified",
        "source_mentions_listing_agent",
    ),
    "repped": (
        cand("repped", representation="agent_involved"),
        page(quote("repped")),
        "rejected",
        "already_has_agent",
    ),
    "chap": (
        cand("chap", summary="Owner selling a home near a church."),
        page(quote("chap")),
        "rejected",
        "protected_characteristic_language",
    ),
}


def verified_outcomes(tmp: Path) -> tuple[Path, dict[str, Any], FakeWeb]:
    names = list(OUTCOMES)
    cites = [cite(n) for n in names if n != "invented"]
    venice = FakeVenice(
        (200, body([OUTCOMES[n][0] for n in names[:6]], cites)),
        (200, body([OUTCOMES[n][0] for n in names[6:]], cites)),
    )
    run = discover(tmp, venice, label="outcomes", only=["fsbo_south_bay", "fsbo_la_county"]).run_dir
    web = FakeWeb({url(n): p for n, (_, p, _, _) in OUTCOMES.items() if p})
    results = verify_run(run, CRITERIA, TODAY, web)
    by_name = {
        r.candidate.property.address.split()[0]: r for r in results if r.candidate.property.address
    }
    return run, by_name, web


def test_verification_outcomes(tmp_path: Path) -> None:
    run, by_name, web = verified_outcomes(tmp_path)
    for name, (_, _, status, note) in OUTCOMES.items():
        got = by_name[name]
        assert got.status == status, (name, got.reasons)
        if note:
            assert note in got.reasons + got.warnings, (name, got.reasons, got.warnings)
    assert by_name["ok"].reasons == [] and by_name["ok"].warnings == []
    assert by_name["ok"].newest_source_date == "2026-10-20"
    assert "not-a-url" not in web.fetched  # a URL the model made up is never fetched
    snapshot = by_name["ok"].checks[0].snapshot_file
    assert (
        snapshot
        and quote("ok") in (run / snapshot).read_text()
        and "ignored()" not in (run / snapshot).read_text()
    )


def test_an_opportunity_from_an_earlier_run_is_a_duplicate(tmp_path: Path) -> None:
    first = discover(
        tmp_path,
        FakeVenice((200, body([cand("ok")], [cite("ok")]))),
        label="a-first",
        only=["fsbo_south_bay"],
    ).run_dir
    verify_run(first, CRITERIA, TODAY, FakeWeb({url("ok"): page(quote("ok"))}))
    second = discover(
        tmp_path,
        FakeVenice((200, body([cand("ok")], [cite("ok")]))),
        label="b-second",
        only=["fsbo_south_bay"],
    ).run_dir
    [again] = verify_run(second, CRITERIA, TODAY, FakeWeb({url("ok"): page(quote("ok"))}))
    assert again.status == "duplicate" and again.reasons == ["found_in_earlier_run"]
    assert again.duplicate_of == f"{first.name}/{again.opportunity_id}"


def test_page_dates_come_from_metadata_json_ld_or_time_tags() -> None:
    assert page_date('<meta name="date" content="2026-10-01">') == date(2026, 10, 1)
    assert page_date('<script>{"datePosted": "2026-10-02T09:00:00Z"}</script>') == date(2026, 10, 2)
    assert page_date(
        '<time datetime="2026-09-30"></time><time datetime="2026-10-03"></time>'
    ) == date(2026, 10, 3)
    assert page_date("<p>October 4, 2026</p>") is None  # body text alone isn't a publication date


# --- scored.json checks and the report -----------------------------------------------------


def scored_run(tmp: Path) -> tuple[Path, dict[str, str]]:
    phone = cand("ok", summary="Torrance home for sale by owner; call 310-555-0134 for showings.")
    names = ["ok", "pend", "blocked"]
    venice = FakeVenice(
        (200, body([phone, cand("pend"), cand("blocked")], [cite(n) for n in names]))
    )
    run = discover(tmp, venice, label="scored", only=["fsbo_south_bay"]).run_dir
    pages = {
        url("ok"): page(quote("ok")),
        url("pend"): page(quote("pend") + " Sale pending."),
        url("blocked"): FetchResult(403, None, "", "http_403"),
    }
    ids = {
        r.candidate.property.address.split()[0]: r.opportunity_id
        for r in verify_run(run, CRITERIA, TODAY, FakeWeb(pages))
    }
    return run, ids


def score(oid: str, name: str, **changes: Any) -> dict[str, Any]:
    record = {
        "schema_version": "scored.v1",
        "opportunity_id": oid,
        "score": 82,
        "dimensions": {"intent": 36, "evidence": 20, "relevance": 16, "recency": 10},
        "explanation": "Dated FSBO listing in Torrance with the quote verified on the page.",
        "facts": [
            {"statement": "The listing says it is for sale by owner.", "source_url": url(name)}
        ],
        "inferences": ["The owner may consider representation."],
        "contradictions": [],
        "missing_info": ["Asking price"],
        "confidence": "high",
        "recommendation": "include",
        "next_step": "Contact the owner through the listing's contact form.",
        "scored_by": "test",
    }
    return {**record, **changes}


def write_scored(run: Path, records: list[dict[str, Any]]) -> None:
    (run / "scored.json").write_text(json.dumps(records))


def test_report_from_a_valid_scored_file(tmp_path: Path) -> None:
    run, ids = scored_run(tmp_path)
    pend = score(
        ids["pend"],
        "pend",
        score=55,
        dimensions={"intent": 20, "evidence": 15, "relevance": 10, "recency": 10},
        recommendation="research_more",
        confidence="medium",
        contradictions=["The page says 'Sale pending'; confirm it is still for sale."],
    )
    write_scored(run, [score(ids["ok"], "ok"), pend])
    assert check_scored(run) == []
    meta = build_report(run)
    assert meta["shown"] == 2 and meta["check_by_hand"] == 1
    page_html = (run / "report.html").read_text()
    csv_text = (run / "report.csv").read_text()
    for text in (page_html, csv_text):
        assert "310-555-0134" not in text and "[phone removed]" in text
        assert url("blocked") in text  # listed to check by hand, unscored
    assert "Couldn't check automatically" in page_html and "Sale pending" in page_html
    assert csv_text.splitlines()[0].endswith("agent_verdict,agent_notes")
    with pytest.raises(FileExistsError):
        build_report(run)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"score": 90}, "sum of the four dimensions"),
        (
            {
                "facts": [
                    {"statement": "Made-up fact here.", "source_url": "https://other.example/"}
                ]
            },
            "wasn't verified",
        ),
        ({"next_step": "Call 310-555-0199 today."}, "phone number or email"),
        (
            {"explanation": "Great for a Hispanic family moving to Torrance."},
            "protected personal characteristic",
        ),
        ({"confidence": "low"}, "needs medium or high confidence"),
    ],
)
def test_check_scored_catches_rule_breaks(
    tmp_path: Path, change: dict[str, Any], problem: str
) -> None:
    run, ids = scored_run(tmp_path)
    pend = score(ids["pend"], "pend", recommendation="reject", contradictions=["Sale pending."])
    write_scored(run, [score(ids["ok"], "ok", **change), pend])
    problems = check_scored(run)
    assert any(problem in p for p in problems), problems
    with pytest.raises(ValueError):
        build_report(run)


def test_check_scored_requires_contradictions_and_every_verified_record(tmp_path: Path) -> None:
    run, ids = scored_run(tmp_path)
    write_scored(run, [score(ids["pend"], "pend"), score(ids["blocked"], "blocked")])
    problems = " | ".join(check_scored(run))
    assert "source_mentions_sold_or_pending" in problems  # include with the warning unaddressed
    assert f"{ids['blocked']}: not a verified opportunity" in problems  # unverified can't be scored
    assert f"{ids['ok']}: verified but not scored" in problems


def test_phone_scrub_leaves_dates_and_prices_alone() -> None:
    assert (
        scrub("Call (310) 555-0134 or +1 310 555 0134") == "Call [phone removed] or [phone removed]"
    )
    for kept in ("Listed 2026-10-30", "ZIP 90501-1234", "$1,250,000", "DRE #01234567"):
        assert not PHONE_RE.search(kept), kept
