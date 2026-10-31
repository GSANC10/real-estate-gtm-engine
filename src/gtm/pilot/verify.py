"""Verification: free, deterministic checks of every discovered candidate.

For each evidence URL that came from the search results, fetch the page (a plain HTTP GET with
an honest user agent; no paid scraping), save a text snapshot, and check that the quote is on
the page, that the source is recent enough, and that the property is in a configured area.
Pages that say the property sold or name a listing agent get a warning the scorer must address.
Duplicates, within this run or of an opportunity verified in an earlier run, are marked and not
scored again. Sites that block automated access are marked ``unverified`` for a person to check
by hand, never silently accepted.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

from gtm.pilot.common import norm_text, norm_url, parse_date, read_json, write_new, write_new_json
from gtm.pilot.models import Criteria, DateBasis, Discovered, SourceCheck, Verified, VerifyStatus
from gtm.util import sha256_hex
from gtm.validation import PROTECTED

USER_AGENT = "gtm-pilot-verifier/0.1 (checks that a public page contains a quoted statement)"
MAX_BYTES = 3_000_000
GONE = {404, 410}
# Words on a page that may contradict "for sale now" or "no agent". Warnings only: aggregator
# pages often mention nearby sold homes, so the scorer reads the snapshot and decides.
SOLD_OR_PENDING = re.compile(
    r"\b(?:sold|in escrow|sale pending|pending sale|under contract|contingent|no longer available|"
    r"off[- ]market|listing (?:has been )?(?:removed|expired))\b",
    re.I,
)
LISTING_AGENT = re.compile(
    r"\b(?:listing agent|listed by|listing courtesy of|brokered by|dre\s*#?\s*\d{6,8})\b", re.I
)


@dataclass(frozen=True)
class FetchResult:
    status: int | None
    final_url: str | None
    html: str
    error: str | None = None


Fetch = Callable[[str], FetchResult]


def http_fetch(url: str) -> FetchResult:
    if not re.match(r"^https?://", url, flags=re.I):
        return FetchResult(None, None, "", "not_http_url")
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(MAX_BYTES)
            charset = response.headers.get_content_charset() or "utf-8"
            return FetchResult(
                int(response.status), response.geturl(), raw.decode(charset, errors="replace")
            )
    except urllib.error.HTTPError as exc:
        return FetchResult(int(exc.code), url, "", f"http_{exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return FetchResult(None, None, "", type(exc).__name__.lower())


class _TextExtractor(HTMLParser):
    SKIP: ClassVar[frozenset[str]] = frozenset({"script", "style", "noscript", "template", "svg"})

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return " ".join(parser.parts)


_META_DATE_NAMES = {
    "article:published_time",
    "og:published_time",
    "datepublished",
    "dateposted",
    "date",
    "pubdate",
    "publishdate",
    "dc.date.issued",
    "article:modified_time",
    "og:updated_time",
    "datemodified",
}


def page_date(html: str) -> date | None:
    """The publication date a page states in its metadata, JSON-LD, or <time> tags."""
    for tag in re.findall(r"<meta\b[^>]*>", html, flags=re.I):
        name = re.search(r'(?:property|name|itemprop)\s*=\s*["\']([^"\']+)', tag, flags=re.I)
        content = re.search(r'content\s*=\s*["\']([^"\']+)', tag, flags=re.I)
        if name and content and name.group(1).casefold() in _META_DATE_NAMES:
            found = parse_date(content.group(1))
            if found:
                return found
    for key in ("datePublished", "datePosted", "dateModified"):
        match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', html)
        if match and (found := parse_date(match.group(1))):
            return found
    times = [
        d
        for d in (
            parse_date(v)
            for v in re.findall(r'<time\b[^>]*datetime\s*=\s*["\']([^"\']+)', html, flags=re.I)
        )
        if d
    ]
    return max(times) if times else None


def city_in_areas(city: str, areas: list[str]) -> bool:
    city_n = norm_text(city)
    return any(re.search(rf"\b{re.escape(norm_text(a))}\b", city_n) for a in areas)


def verify_candidate(
    cand: Discovered, criteria: Criteria, today: date, fetch: Fetch, run_dir: Path
) -> Verified:
    reasons: list[str] = []
    warnings: list[str] = []
    checks: list[SourceCheck] = []
    dates: list[date] = []
    gone = 0

    for ev, found_in_search in zip(cand.evidence, cand.discovery_checks, strict=True):
        if not found_in_search.url_in_search_results:
            # Never fetch a URL the model produced itself; it isn't evidence.
            checks.append(
                SourceCheck(
                    url=ev.url,
                    fetched=False,
                    http_status=None,
                    final_url=None,
                    snapshot_file=None,
                    quote_found=False,
                    source_date=None,
                    date_basis="none",
                    area_on_page=False,
                )
            )
            continue
        page = fetch(ev.url)
        fetched = page.error is None and page.status is not None and page.status < 400
        if page.status in GONE:
            gone += 1
        snapshot = None
        quote_found = area_on_page = False
        found_date: date | None = None
        basis: DateBasis = "none"
        if fetched:
            text = html_to_text(page.html)
            snapshot = f"sources/{sha256_hex(norm_url(ev.url))[:16]}.txt"
            if not (run_dir / snapshot).exists():
                write_new(
                    run_dir / snapshot,
                    f"url: {ev.url}\nfinal_url: {page.final_url}\nhttp_status: {page.status}\n\n{text}\n",
                )
            page_text = norm_text(text)
            quote_found = norm_text(ev.quote) in page_text
            area_on_page = any(norm_text(a) in page_text for a in criteria.areas)
            if quote_found and SOLD_OR_PENDING.search(text):
                warnings.append("source_mentions_sold_or_pending")
            if quote_found and cand.signal == "fsbo_listing" and LISTING_AGENT.search(text):
                warnings.append("source_mentions_listing_agent")
            found_date = page_date(page.html)
            basis = "page_metadata" if found_date else "none"
        if found_date is None and (d := parse_date(found_in_search.search_result_date)):
            found_date, basis = d, "search_result"
        if found_date is None and (d := parse_date(ev.date)):
            found_date, basis = d, "model_stated"
        if quote_found and found_date:
            dates.append(found_date)
            if basis == "model_stated":
                warnings.append("date_only_from_model")
        checks.append(
            SourceCheck(
                url=ev.url,
                fetched=fetched,
                http_status=page.status,
                final_url=page.final_url,
                snapshot_file=snapshot,
                quote_found=quote_found,
                source_date=found_date.isoformat() if found_date else None,
                date_basis=basis,
                area_on_page=area_on_page,
            )
        )

    from_search = [
        c for c, d in zip(checks, cand.discovery_checks, strict=True) if d.url_in_search_results
    ]
    if not from_search:
        reasons.append("evidence_not_from_search_results")
    elif gone == len(from_search):
        reasons.append("source_not_found")
    elif not any(c.fetched for c in from_search):
        reasons.append("source_unreachable_or_blocked")
    elif not any(c.quote_found for c in from_search):
        reasons.append("quote_not_found_on_source")

    newest = max(dates) if dates else None
    if newest and newest < today - timedelta(days=criteria.lookback_days):
        reasons.append("source_older_than_lookback")
    if newest is None and not reasons:
        warnings.append("date_unconfirmed")
    if not city_in_areas(cand.property.city, criteria.areas):
        reasons.append("outside_configured_areas")
    elif not any(c.area_on_page for c in checks if c.quote_found):
        warnings.append("area_not_stated_on_source")
    if not criteria.signals[cand.signal].enabled:
        reasons.append("signal_type_not_enabled")
    if cand.representation == "agent_involved":
        reasons.append("already_has_agent")
    model_text = " ".join(
        [
            cand.summary,
            *cand.missing_info,
            *(v for v in cand.property.model_dump().values() if isinstance(v, str)),
        ]
    )
    if PROTECTED.search(model_text):
        reasons.append("protected_characteristic_language")

    final: VerifyStatus
    if reasons == ["source_unreachable_or_blocked"]:
        final = "unverified"
    else:
        final = "rejected" if reasons else "verified"
    return Verified(
        opportunity_id=cand.opportunity_id,
        status=final,
        reasons=reasons,
        warnings=sorted(set(warnings)),
        duplicate_of=None,
        newest_source_date=newest.isoformat() if newest else None,
        checks=checks,
        candidate=cand,
    )


def _dedupe_key(cand: Discovered) -> str:
    if cand.property.address:
        return f"addr:{norm_text(cand.property.address)}|{norm_text(cand.property.city)}"
    return f"url:{norm_url(cand.evidence[0].url)}"


def _keys(cand: Discovered) -> list[str]:
    return [_dedupe_key(cand), *(f"url:{norm_url(e.url)}" for e in cand.evidence)]


def earlier_opportunities(run_dir: Path) -> dict[str, str]:
    """Dedupe keys of opportunities verified in earlier runs under the same data folder."""
    seen: dict[str, str] = {}
    for path in sorted(run_dir.parents[1].glob("*/*/verified.json")):
        other = path.parent
        if other.name >= run_dir.name:
            continue
        for row in read_json(path):
            v = Verified.model_validate(row)
            if v.status == "verified":
                for k in _keys(v.candidate):
                    seen.setdefault(k, f"{other.name}/{v.opportunity_id}")
    return seen


def verify_run(
    run_dir: Path, criteria: Criteria, today: date, fetch: Fetch = http_fetch
) -> list[Verified]:
    candidates = [Discovered.model_validate(r) for r in read_json(run_dir / "discovered.json")]
    earlier = earlier_opportunities(run_dir)
    seen: dict[str, str] = {}
    out: list[Verified] = []
    for cand in candidates:
        keys = _keys(cand)
        first = next((seen[k] for k in keys if k in seen), None)
        before = next((earlier[k] for k in keys if k in earlier), None)
        if first or before:
            reason = "duplicate_in_this_run" if first else "found_in_earlier_run"
            out.append(
                Verified(
                    opportunity_id=cand.opportunity_id,
                    status="duplicate",
                    reasons=[reason],
                    warnings=[],
                    duplicate_of=first or before,
                    newest_source_date=None,
                    checks=[],
                    candidate=cand,
                )
            )
            continue
        for k in keys:
            seen.setdefault(k, cand.opportunity_id)
        out.append(verify_candidate(cand, criteria, today, fetch, run_dir))
    write_new_json(run_dir / "verified.json", [v.model_dump(mode="json") for v in out])
    return out
