#!/usr/bin/env python3
"""Collect and normalize market news from RSS/HTTP sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
WATCHLIST_PATH = DATA_DIR / "watchlist.json"
STATE_PATH = DATA_DIR / "state.json"
COLLECTED_PATH = DATA_DIR / "collected.json"
SOURCE_HEALTH_PATH = DATA_DIR / "source_health.json"
WINDOW_HOURS = 4
DEDUP_HOURS = 48
MAX_FEED_BYTES = 5_000_000


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str
    tier: str
    default_section: str  # macro | corporate | unverified


FEEDS: list[FeedSource] = [
    FeedSource(
        name="SEC EDGAR Current",
        url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&count=100&output=atom",
        tier="T1",
        default_section="corporate",
    ),
    FeedSource(
        name="Benzinga",
        url="https://www.benzinga.com/feed",
        tier="T2",
        default_section="corporate",
    ),
    FeedSource(
        name="Financial Times",
        url="https://www.ft.com/rss/home/us",
        tier="T2",
        default_section="macro",
    ),
    FeedSource(
        name="ZeroHedge",
        url="https://feeds.feedburner.com/zerohedge/feed",
        tier="T3",
        default_section="unverified",
    ),
    FeedSource(
        name="SeekingAlpha",
        url="https://seekingalpha.com/feed.xml",
        tier="T3",
        default_section="unverified",
    ),
    FeedSource(
        name="Nikkei Asia",
        url="https://asia.nikkei.com/rss/feed/nar",
        tier="T2",
        default_section="macro",
    ),
    FeedSource(
        name="SCMP",
        url="https://www.scmp.com/rss/91/feed",
        tier="T2",
        default_section="macro",
    ),
    FeedSource(
        name="Korea Herald",
        url="http://www.koreaherald.com/common_prog/rssdisp.php?ct=020000000000.xml",
        tier="T2",
        default_section="macro",
    ),
]

MACRO_TERMS = (
    "cpi",
    "pce",
    "inflation",
    "central bank",
    "fomc",
    "ecb",
    "boj",
    "pboc",
    "hkma",
    "policy rate",
    "gdp",
    "employment",
    "jobless",
    "payroll",
    "pmi",
    "trade balance",
)

STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
    "by",
    "after",
    "before",
}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None

    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (TypeError, ValueError):
        pass

    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _first_text(node: ET.Element, names: tuple[str, ...]) -> str | None:
    for child in node.iter():
        if _strip_ns(child.tag) in names and child.text:
            text = child.text.strip()
            if text:
                return text
    return None


def _parse_rss_items(xml_text: str, source: FeedSource) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    now_utc = datetime.now(UTC)
    items: list[dict[str, Any]] = []

    candidates: list[ET.Element] = []
    for elem in root.iter():
        local_tag = _strip_ns(elem.tag)
        if local_tag in {"item", "entry"}:
            candidates.append(elem)

    for item in candidates:
        title = _first_text(item, ("title",))
        if not title:
            continue

        link = None
        link_els = [child for child in item.iter() if _strip_ns(child.tag) == "link"]
        for el in link_els:
            rel = el.attrib.get("rel", "").strip().lower()
            if rel and rel != "alternate":
                continue
            href = (el.attrib.get("href") or "").strip() or (el.text or "").strip()
            if href:
                link = href
                break
        if not link:
            for el in link_els:
                href = (el.attrib.get("href") or "").strip() or (el.text or "").strip()
                if href:
                    link = href
                    break
        if not link:
            continue

        summary = _first_text(item, ("description", "summary", "content"))
        published_raw = _first_text(item, ("pubDate", "published", "updated", "date"))
        # Never fabricate timestamps: drop undated items (M1) — a "now" stamp
        # would fake freshness, defeat the 4h window, and break dedup.
        published_dt = _parse_timestamp(published_raw)
        if not published_dt:
            continue

        items.append(
            {
                "source": source.name,
                "source_tier": source.tier,
                "default_section": source.default_section,
                "title": re.sub(r"\s+", " ", title).strip(),
                "link": link,
                "summary": re.sub(r"\s+", " ", summary or "").strip(),
                "published_at_utc": published_dt.isoformat(),
            }
        )

    return items


def _fetch_feed(source: FeedSource) -> list[dict[str, Any]]:
    # SEC requires a descriptive User-Agent with contact info (their fair-access policy).
    contact = os.environ.get("SEC_CONTACT_EMAIL", "admin@localhost")
    ua = f"MarketFeed Research Digest (personal trader news aggregation; contact: {contact})"
    resp = requests.get(
        source.url,
        timeout=25,
        headers={"User-Agent": ua},
    )
    resp.raise_for_status()
    if len(resp.content) > MAX_FEED_BYTES:
        raise ValueError(f"{source.name}: feed exceeds {MAX_FEED_BYTES} bytes")
    return _parse_rss_items(resp.text, source)


def _load_watchlist() -> dict[str, str]:
    raw = _read_json(WATCHLIST_PATH, default={})
    ticker_to_name: dict[str, str] = {}
    for group, entries in raw.items():
        if group.startswith("_") or not isinstance(entries, list):
            continue
        for entry in entries:
            ticker = str(entry.get("ticker", "")).strip()
            name = str(entry.get("name", "")).strip()
            if ticker and name:
                ticker_to_name[ticker] = name
    return ticker_to_name


_NAME_STOP = {
    "GROUP", "HOLDINGS", "INC", "CORP", "CORPORATION", "LTD", "CO", "THE",
    "COMMON", "STOCK", "CLASS", "COMPANY", "PLC", "SA", "ADR",
    "OF", "AND", "FOR", "DE",
}
_SHARE_SUFFIX_RE = re.compile(r"\s*-\s*(W|SW|S|R)$")

# 2-letter tickers that genuinely appear as words in finance headlines (MU =
# Micron). Everything else short ("PM" = prime minister in prose) must not
# word-match — those names are still caught via company-name phrases.
_SHORT_TICKER_ALLOW = {"MU"}

# Watchlist entries whose stored name is a Bloomberg short name that never
# appears in headline copy; also try the name journalists actually use.
_NAME_ALIASES = {
    "09988.HK": "ALIBABA",       # BABA - W
    "09888.HK": "BAIDU",         # BIDU - SW
    "09999.HK": "NETEASE",       # NTES
    "01698.HK": "TENCENT MUSIC", # TME - SW
}


def _clean_name_tokens(name: str) -> list[str]:
    """Normalize a watchlist company name into significant tokens:
    strip share-class suffixes (-W/-SW/-S), corporate boilerplate and
    stopwords, so 'BANK OF CHINA' -> ['BANK', 'CHINA'] (C1/C2)."""
    n = _SHARE_SUFFIX_RE.sub("", name.upper())
    n = n.replace("CORP /DE/", " ")
    return [t for t in re.sub(r"[^A-Z0-9 ]+", " ", n).split() if t and t not in _NAME_STOP]


def _find_watchlist_hits(text: str, ticker_to_name: dict[str, str]) -> list[str]:
    text_orig = f" {re.sub(r'[^A-Za-z0-9 ]+', ' ', text)} "
    text_upper = text_orig.upper()
    # Stopword-strip BOTH sides so "BANK OF CHINA" aligns with name tokens
    # ["BANK","CHINA"] (C1): "BANK OF JAPAN" cleans to "BANK JAPAN", which
    # never matches the " BANK CHINA " phrase.
    text_clean = " " + " ".join(t for t in text_upper.split() if t not in _NAME_STOP) + " "
    hits: set[str] = set()

    for ticker, name in ticker_to_name.items():
        core = ticker.upper().split(".")[0]
        # Tickers appear UPPERCASE in wire headlines; case-sensitive match kills
        # false positives like "cat cafes" -> CAT (Caterpillar). HK codes may
        # appear padded ("09988") or bare ("9988"), but bare forms of short
        # codes ("20", "100", "300") collide with ordinary prose numbers — only
        # allow bare forms of 4+ digits.
        bare = core.lstrip("0")
        for form in {core, bare}:
            # Ticker words need 3+ chars unless allowlisted ("PM" in "ex-PM"
            # must never hit Philip Morris; "MU" is allowed); bare numeric
            # forms need 4+ digits ("9988" ok, "20"/"100"/"300" collide with
            # ordinary prose numbers).
            if not form:
                continue
            if len(form) < 3 and form not in _SHORT_TICKER_ALLOW:
                continue
            if form != core and len(form) < 4:
                continue
            if f" {form} " in text_orig:
                hits.add(ticker)
                break
        else:
            for cand in filter(None, (name, _NAME_ALIASES.get(ticker))):
                toks = _clean_name_tokens(cand)
                # 2+ significant tokens -> require the phrase ("BANK CHINA"), so
                # "Bank of Japan" never matches Bank of China/Bank of America.
                if len(toks) >= 2:
                    if f" {' '.join(toks[:2])} " in text_clean:
                        hits.add(ticker)
                elif len(toks) == 1 and f" {toks[0]} " in text_clean:
                    hits.add(ticker)

    return sorted(hits)


FILLER_RE = re.compile(
    r"^(\d+ )?(best|top|worst|greatest) "
    r"(tech|energy|biotech|biotech|gold|oil|solar|dividend|growth|value|penny|small-cap|large-cap|mid-cap|fintech|ai|cybersecurity|cloud|semiconductor|retail|bank|real estate|industrial|healthcare|consumer)?"
    r"\s*(stocks|funds|etfs|picks|buys|ways|reasons|gains)\b|^under \$|.{0,50}\breview$",
    re.I,
)


def _story_key(title: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", title.lower())
    filtered = [t for t in tokens if t not in STOPWORDS and len(t) > 2]
    if not filtered:
        filtered = tokens
    return " ".join(filtered[:8])


CORPORATE_TERMS = (
    "earnings", "guidance", "profit", "loss", "m&a", "merger", "acquisition",
    "buyout", "buyback", "dividend", "split", "lawsuit", "sues", "investigation",
    "ceo", "cfo", "ipo", "forecast", "warns", "layoffs", "recalls",
)


def _classify_section(item: dict[str, Any], watch_hits: list[str]) -> str:
    text = f"{item['title']} {item.get('summary', '')}".lower()
    if item["default_section"] == "unverified":
        return "unverified"
    # Corporate stories from macro-default feeds (Nikkei/SCMP/FT) must route
    # to CORPORATE, not get stuck in MACRO (m2).
    if watch_hits and any(term in text for term in CORPORATE_TERMS):
        return "corporate"
    if any(term in text for term in MACRO_TERMS):
        return "macro"
    return item["default_section"]


def _compute_item_id(item: dict[str, Any]) -> str:
    parsed = urlparse(item["link"])
    canonical = "|".join(
        (
            item["source"],
            parsed.netloc.lower(),
            parsed.path,
            item["title"].lower(),
            item["published_at_utc"],
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _load_state() -> dict[str, Any]:
    state = _read_json(STATE_PATH, default={"seen": []})
    if "seen" not in state or not isinstance(state["seen"], list):
        state = {"seen": []}
    return state


def _prune_state_entries(seen_entries: list[dict[str, str]], now_utc: datetime) -> list[dict[str, str]]:
    cutoff = now_utc - timedelta(hours=DEDUP_HOURS)
    kept: list[dict[str, str]] = []
    for row in seen_entries:
        seen_at_raw = row.get("seen_at")
        item_id = row.get("id")
        seen_at = _parse_timestamp(seen_at_raw)
        if not item_id or not seen_at:
            continue
        if seen_at >= cutoff:
            kept.append({"id": item_id, "seen_at": seen_at.isoformat()})
    return kept


def _fetch_x_leads() -> list[dict[str, Any]]:
    xurl = shutil.which("xurl")
    if not xurl:
        _log("warning: xurl not installed; skipping X leads")
        return []

    # v1 keeps this intentionally minimal and verified-account constrained by query.
    cmd = [
        xurl,
        "search",
        "from:BloombergTV OR from:CNBC earnings merger guidance macro",
        "--limit",
        "20",
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        _log(f"warning: xurl failed ({proc.returncode}); skipping X leads")
        return []

    leads: list[dict[str, Any]] = []
    now = datetime.now(UTC).isoformat()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("http"):
            leads.append(
                {
                    "source": "X",
                    "source_tier": "T3",
                    "default_section": "unverified",
                    "title": "Lead from verified X account",
                    "link": line,
                    "summary": "",
                    "published_at_utc": now,
                }
            )
    return leads


def collect_news() -> dict[str, Any]:
    now_utc = datetime.now(UTC)
    window_start = now_utc - timedelta(hours=WINDOW_HOURS)
    ticker_to_name = _load_watchlist()
    state = _load_state()
    state_seen = _prune_state_entries(state.get("seen", []), now_utc)
    seen_ids = {entry["id"] for entry in state_seen}

    network_errors: list[str] = []
    raw_items: list[dict[str, Any]] = []

    for source in FEEDS:
        try:
            parsed = _fetch_feed(source)
            raw_items.extend(parsed)
            _log(f"fetched {source.name}: {len(parsed)} items")
        except (requests.RequestException, ET.ParseError) as exc:
            network_errors.append(f"{source.name}: {exc}")

    raw_items.extend(_fetch_x_leads())

    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        published = _parse_timestamp(item["published_at_utc"])
        if not published:
            continue
        if published < window_start or published > now_utc + timedelta(minutes=5):
            continue

        item_id = _compute_item_id(item)
        if item_id in seen_ids:
            continue

        if FILLER_RE.match(item["title"].strip()):
            continue

        # EDGAR: whitelist material filings only (8-K, 6-K, 10-K/Q, S-1, DEF 14A,
        # SC 13D...); everything else (424B, FWP, Form 4/D, N-PX...) is noise.
        if item.get("source") == "SEC EDGAR Current" and not re.match(
            r"^(8-K|6-K|10-K|10-Q|S-1|S-3|DEF 14A|PRE 14A|SC 13D|SD)", item["title"].strip()
        ):
            continue

        # Sanitize against markdown/link injection from feed content (m4).
        title = re.sub(r"[\[\](){}<>]", "", item["title"]).strip()
        link = item["link"].strip()
        if not link.startswith(("http://", "https://")):
            continue

        text_blob = f"{title} {(item.get('summary') or '')[:250]}"
        watch_hits = _find_watchlist_hits(text_blob, ticker_to_name)
        # EDGAR items are only relevant when they touch the watchlist (M8).
        if item.get("source") == "SEC EDGAR Current" and not watch_hits:
            continue
        section = _classify_section(item, watch_hits)
        story_key = _story_key(title)
        normalized.append(
            {
                "id": item_id,
                "title": title,
                "link": link,
                "summary": item.get("summary", ""),
                "source": item["source"],
                "source_tier": item["source_tier"],
                "published_at_utc": published.isoformat(),
                "section": section,
                "story_key": story_key,
                "watchlist_hits": watch_hits,
                "trust": "SINGLE_SOURCE",
            }
        )
        seen_ids.add(item_id)
        state_seen.append({"id": item_id, "seen_at": now_utc.isoformat()})

    # Trust labeling: T3/unverified NEVER confirmed; T1 official; else
    # cross-source confirmation via fuzzy story-key similarity (Jaccard >= 0.6),
    # counting only OTHER independent non-T3 sources (C3, M2).
    keys = [frozenset(item["story_key"].split()) for item in normalized]

    def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    cross_sources: list[set[str]] = [set() for _ in normalized]
    for i in range(len(normalized)):
        if normalized[i]["source_tier"] == "T3":
            continue
        for j in range(len(normalized)):
            if i == j or normalized[j]["source_tier"] == "T3":
                continue
            if _jaccard(keys[i], keys[j]) >= 0.6:
                cross_sources[i].add(normalized[j]["source"])

    for i, item in enumerate(normalized):
        if item["source_tier"] == "T1":
            item["trust"] = "CONFIRMED"
        elif item["section"] == "unverified" or item["source_tier"] == "T3":
            item["trust"] = "UNVERIFIED"
        elif any(s != item["source"] for s in cross_sources[i]):
            item["trust"] = "CONFIRMED"
        else:
            item["trust"] = "SINGLE_SOURCE"

    normalized.sort(
        key=lambda row: (
            row["published_at_utc"],
            1 if row["trust"] == "CONFIRMED" else 0,
            len(row["watchlist_hits"]),
        ),
        reverse=True,
    )

    snapshot = {
        "collected_at_utc": now_utc.isoformat(),
        "window_start_utc": window_start.isoformat(),
        "items": normalized,
    }
    _write_json(COLLECTED_PATH, snapshot)
    _write_json(STATE_PATH, {"seen": state_seen})

    _log(f"wrote {len(normalized)} collected items to {COLLECTED_PATH}")
    _write_json(SOURCE_HEALTH_PATH, {"failed": network_errors, "total": len(FEEDS)})
    # A quiet window (overnight HK, weekend) legitimately yields 0 new items —
    # that's a valid digest, not a failure. Dead feeds are caught above.
    if len(network_errors) >= len(FEEDS) // 2:
        raise RuntimeError(f"{len(network_errors)}/{len(FEEDS)} feeds failed: {'; '.join(network_errors)}")
    if network_errors:
        _log(f"warning: partial collection — {len(network_errors)} source(s) failed: {'; '.join(network_errors)}")
    return snapshot


def _self_check() -> None:
    sample_rss = """<?xml version="1.0"?>
    <rss><channel>
      <item>
        <title>Test CPI headline</title>
        <link>https://example.com/a</link>
        <pubDate>Tue, 18 Aug 2026 00:00:00 GMT</pubDate>
        <description>Inflation beats consensus</description>
      </item>
    </channel></rss>"""
    src = FeedSource("Sample", "https://example.com", "T2", "macro")
    parsed = _parse_rss_items(sample_rss, src)
    assert len(parsed) == 1
    assert parsed[0]["title"] == "Test CPI headline"
    assert _story_key("The Fed and inflation outlook") == "fed inflation outlook"
    now_utc = datetime(2026, 8, 18, tzinfo=UTC)
    pruned = _prune_state_entries(
        [
            {"id": "a", "seen_at": "2026-08-17T00:00:00+00:00"},
            {"id": "b", "seen_at": "2026-08-10T00:00:00+00:00"},
        ],
        now_utc,
    )
    assert {row["id"] for row in pruned} == {"a"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect market news into data/collected.json")
    parser.add_argument("--self-check", action="store_true", help="run module self-check")
    args = parser.parse_args()

    if args.self_check:
        _self_check()
        print("collect.py self-check passed")
        return

    collect_news()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail loudly with non-zero exit
        print(f"collect.py failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
