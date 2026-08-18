#!/usr/bin/env python3
"""Build markdown digest from collected news and Bloomberg calendar exports."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
COLLECTED_PATH = DATA_DIR / "collected.json"
WATCHLIST_PATH = DATA_DIR / "watchlist.json"
BBG_EXPORT_DIR = DATA_DIR / "bbg_exports"
SOURCE_HEALTH_PATH = DATA_DIR / "source_health.json"
MAX_WORDS = 1500

HKT = timezone(timedelta(hours=8), name="HKT")


def _parse_iso_utc(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _to_hkt(dt: datetime) -> datetime:
    return dt.astimezone(HKT)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_watchlist_map() -> dict[str, str]:
    raw = _read_json(WATCHLIST_PATH)
    ticker_to_name: dict[str, str] = {}
    for group, entries in raw.items():
        if group.startswith("_") or not isinstance(entries, list):
            continue
        for entry in entries:
            ticker = str(entry.get("ticker", "")).strip()
            name = str(entry.get("name", "")).strip()
            if ticker and name and ticker not in ticker_to_name:
                ticker_to_name[ticker] = name
    return ticker_to_name


def _render_watch_hits(hits: list[str], ticker_to_name: dict[str, str]) -> str:
    if not hits:
        return ""
    rendered = [ticker_to_name.get(ticker, ticker) for ticker in hits]
    return ", ".join(rendered[:3])


def _infer_why(item: dict[str, Any]) -> str:
    text = f"{item['title']} {item.get('summary', '')}".lower()
    if any(k in text for k in ("guidance", "outlook", "forecast", "consensus")):
        return "Resets expectations and can reprice implied volatility fast."
    if any(k in text for k in ("merger", "acquisition", "buyout")):
        return "Deal terms can trigger abrupt dispersion and sector sympathy moves."
    if any(k in text for k in ("cpi", "fomc", "boj", "pboc", "ecb", "rate")):
        return "Policy path signal can shift cross-asset risk and index vol."
    if item.get("watchlist_hits"):
        return "🎯 watchlist name in play."
    return "Potential catalyst; verify before acting."


def _item_line(item: dict[str, Any], ticker_to_name: dict[str, str], include_why: bool) -> str:
    dt_hkt = _to_hkt(_parse_iso_utc(item["published_at_utc"]))
    stamp = dt_hkt.strftime("%Y-%m-%d %H:%M HKT")
    watch = _render_watch_hits(item.get("watchlist_hits", []), ticker_to_name)
    watch_suffix = f" · watchlist: {watch}" if watch else ""
    why = f" · Why it matters: {_infer_why(item)}" if include_why else ""
    return (
        f"- [{item['trust']}] {html.unescape(item['title'])} — {item['source']} "
        f"([link]({item['link']})) · {stamp}{watch_suffix}{why}"
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _parse_any_date(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _build_upcoming(now_utc: datetime, ticker_to_name: dict[str, str]) -> list[str]:
    earnings = _read_csv(BBG_EXPORT_DIR / "earnings.csv")
    dividends = _read_csv(BBG_EXPORT_DIR / "dividends.csv")
    splits = _read_csv(BBG_EXPORT_DIR / "splits.csv")
    econ = _read_csv(BBG_EXPORT_DIR / "econ.csv")

    lines: list[str] = []

    # Calendar-day boundaries are HKT days (the trader's day), not UTC (M4).
    hkt_today = _to_hkt(now_utc).date()
    tomorrow = hkt_today + timedelta(days=1)
    week_end = hkt_today + timedelta(days=7)

    earnings_lines: list[str] = []
    for row in earnings:
        dt = _parse_any_date(row.get("EARN_ANN_DT", ""))
        if not dt or dt.date() != tomorrow:
            continue
        ticker = row.get("ticker", "").strip()
        name = ticker_to_name.get(ticker, ticker or row.get("security", "").strip())
        eps = row.get("BEST_EPS", "").strip() or "n/a"
        rev = row.get("BEST_REV", "").strip() or "n/a"
        cur = row.get("CURRENCY", "").strip() or row.get("CRNCY", "").strip() or ""
        earnings_lines.append(
            f"- Earnings (next day): {name} ({ticker}) · EPS cons {eps} · Rev cons {rev} {cur}".strip()
        )

    div_lines: list[str] = []
    for row in dividends:
        dt = _parse_any_date(row.get("EX_DIV_DT", ""))
        # Same-day ex-divs count (m7): compare HKT calendar dates, not UTC now.
        if not dt or not (hkt_today <= dt.date() <= week_end):
            continue
        ticker = row.get("ticker", "").strip()
        name = ticker_to_name.get(ticker, ticker or row.get("security", "").strip())
        amt = row.get("DVD_AMT", "").strip() or "n/a"
        yld = row.get("DVD_YLD", "").strip() or "n/a"
        div_lines.append(f"- Dividend (this week): {name} ({ticker}) · amt {amt} · yield {yld}")

    split_lines: list[str] = []
    for row in splits:
        dt = _parse_any_date(row.get("SPLIT_DT", ""))
        if not dt or not (hkt_today <= dt.date() <= week_end):
            continue
        ticker = row.get("ticker", "").strip()
        name = ticker_to_name.get(ticker, ticker or row.get("security", "").strip())
        ratio = row.get("SPLIT_RATIO", "").strip() or "n/a"
        split_lines.append(f"- Split (this week): {name} ({ticker}) · ratio {ratio}")

    econ_lines: list[str] = []
    for row in econ:
        dt = _parse_any_date(
            row.get("release_datetime_local", "") or row.get("release_datetime_utc", "") or row.get("release_date", "")
        )
        if not dt or not (hkt_today <= dt.date() <= week_end):
            continue
        event = row.get("event_name", "").strip() or row.get("security", "").strip()
        cons = row.get("consensus", "").strip() or "n/a"
        econ_lines.append(f"- Econ (next 7d): {event} · consensus {cons}")

    if earnings_lines:
        lines.extend(earnings_lines[:8])
    if div_lines:
        lines.extend(div_lines[:8])
    if split_lines:
        lines.extend(split_lines[:8])
    if econ_lines:
        lines.extend(econ_lines[:8])

    if not lines:
        lines.append("- Official calendar files not present or no qualifying rows in horizon.")
    return lines


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _build_digest(collected: dict[str, Any], ticker_to_name: dict[str, str]) -> str:
    items = collected.get("items", [])

    now_utc = _parse_iso_utc(collected.get("collected_at_utc", datetime.now(UTC).isoformat()))
    now_hkt = _to_hkt(now_utc)
    window_start_hkt = now_hkt - timedelta(hours=4)

    macro = [i for i in items if i.get("section") == "macro" and i.get("trust") != "UNVERIFIED"]
    corp = [i for i in items if i.get("section") != "macro" and i.get("trust") != "UNVERIFIED"]
    unverified = [i for i in items if i.get("trust") == "UNVERIFIED"]

    def _score(row: dict[str, Any]) -> int:
        title = str(row.get("title", ""))
        s = 0
        if re.search(r"cpi|fomc|federal reserve|ecb|boj|pboc|inflation|gdp|unemployment|rate decision|central bank|yield|treasury", title, re.I):
            s += 3
        if re.search(r"earnings|m&a|acquir|merger|guidance|warn|split|dividend|buyback|lawsuit|sec |ipo|delist|forecast|profit", title, re.I):
            s += 2
        s += 2 * len(row.get("watchlist_hits", []))
        s += 2 if row.get("trust") == "CONFIRMED" else 0
        return s

    macro.sort(key=_score, reverse=True)
    corp.sort(key=_score, reverse=True)
    unverified.sort(key=_score, reverse=True)

    top_ids = {row["id"] for row in (macro + corp)[:5]}

    macro_lines = [_item_line(it, ticker_to_name, it["id"] in top_ids) for it in macro[:5]]
    corp_lines = [_item_line(it, ticker_to_name, it["id"] in top_ids) for it in corp[:8]]
    unverified_lines = [_item_line(it, ticker_to_name, False) for it in unverified[:4]]
    upcoming_lines = _build_upcoming(now_utc, ticker_to_name)

    if not macro_lines:
        macro_lines = ["- No qualifying macro headlines in this window."]
    if not corp_lines:
        corp_lines = ["- No qualifying corporate headlines in this window."]
    if not unverified_lines:
        unverified_lines = ["- No unverified single-source leads in this window."]

    def _render_header() -> list[str]:
        return [
            f"📊 MARKET DIGEST — {now_hkt.strftime('%H:00 HKT')} · window {window_start_hkt.strftime('%H:%M')}-{now_hkt.strftime('%H:%M')} HKT",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "🌐 MACRO & POLICY — econ prints, CB decisions: actual vs consensus",
            *macro_lines,
            "🏢 CORPORATE — earnings, M&A, guidance, unusual-move news; watchlist names first",
            *corp_lines,
            "📅 UPCOMING — next-day earnings w/ consensus · this week's dividends · splits [OFFICIAL]",
            *upcoming_lines,
            "🔍 UNVERIFIED — single-source leads, labeled, with link",
            *unverified_lines,
        ]

    # Source-health footer: surface degraded collection in the digest (M3).
    health = _read_json(SOURCE_HEALTH_PATH) if SOURCE_HEALTH_PATH.exists() else {}
    failed = health.get("failed") or []
    footer: list[str] = []
    if failed:
        footer.append(f"⚠️ Sources down this window: {', '.join(failed)}")

    digest = "\n".join(_render_header() + footer)

    while _word_count(digest) > MAX_WORDS and (
        len(corp_lines) > 4 or len(macro_lines) > 3 or len(unverified_lines) > 2 or len(upcoming_lines) > 6
    ):
        if len(corp_lines) > 4:
            corp_lines.pop()
        elif len(macro_lines) > 3:
            macro_lines.pop()
        elif len(unverified_lines) > 2:
            unverified_lines.pop()
        elif len(upcoming_lines) > 6:
            upcoming_lines.pop()
        digest = "\n".join(_render_header() + footer)

    return digest


def _self_check() -> None:
    ticker_map = {"00175.HK": "GEELY AUTO"}
    line = _item_line(
        {
            "id": "x",
            "title": "GEELY AUTO updates guidance",
            "summary": "",
            "source": "Benzinga",
            "link": "https://example.com",
            "published_at_utc": "2026-08-18T00:00:00+00:00",
            "watchlist_hits": ["00175.HK"],
            "trust": "CONFIRMED",
        },
        ticker_map,
        include_why=True,
    )
    assert "GEELY AUTO" in line
    assert "Why it matters" in line
    digest = _build_digest(
        {
            "collected_at_utc": "2026-08-18T04:00:00+00:00",
            "items": [
                {
                    "id": "1",
                    "title": "BoJ policy update",
                    "summary": "rate guidance",
                    "source": "Nikkei Asia",
                    "link": "https://example.com/a",
                    "source_tier": "T2",
                    "published_at_utc": "2026-08-18T03:30:00+00:00",
                    "section": "macro",
                    "watchlist_hits": [],
                    "trust": "CONFIRMED",
                }
            ],
        },
        ticker_map,
    )
    assert "📊 MARKET DIGEST" in digest
    assert "🌐 MACRO & POLICY" in digest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build digest markdown from collected items")
    parser.add_argument("--self-check", action="store_true", help="run module self-check")
    args = parser.parse_args()

    if args.self_check:
        _self_check()
        print("digest.py self-check passed")
        return

    if not COLLECTED_PATH.exists():
        raise RuntimeError(f"missing collected payload: {COLLECTED_PATH}")

    ticker_to_name = _load_watchlist_map()
    collected = _read_json(COLLECTED_PATH)
    digest = _build_digest(collected, ticker_to_name)
    print(digest)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"digest.py failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
