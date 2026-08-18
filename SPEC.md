# Market Feed Pipeline — Build Spec v1.0

Trader-oriented financial news digest, delivered every 4 hours to a single chat.
Audience: APAC equity vol trader (HK/JP/KR + US focus). News-driven — NO price
scanners, NO technical indicators, NO position overlay.

## Deliverables (all Python 3.11, stdlib + `requests` only — no heavy deps)

| File | Runs on | Purpose |
|---|---|---|
| `scripts/collect.py` | this host, every 4h | Fetch + normalize news items from sources, dedupe, label trust |
| `scripts/digest.py` | this host, every 4h | Build the digest markdown from collected items + calendar |
| `scripts/bbg_export.py` | USER'S Bloomberg machine, nightly ~20:00 HKT | blpapi → 4 CSVs into `bbg_exports/` |
| `data/watchlist.json` | both | 197-name universe (already provided) |
| `data/state.json` | this host | Dedup: keep seen-item ids from last 48h |
| `data/bbg_exports/*.csv` | this host | Nightly Bloomberg calendar data, ingested by digest.py |

Run contract: `python3 scripts/collect.py && python3 scripts/digest.py` prints ONE
markdown digest to stdout (Hermes cron captures it for delivery). Non-trivial
modules get ONE small runnable self-check (assert-based `__main__` demo), no test
frameworks.

## Digest template (exact shape)

```
📊 MARKET DIGEST — <HH:00 HKT> · window <prev 4h>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🌐 MACRO & POLICY — econ prints, CB decisions: actual vs consensus [CONFIRMED]
🏢 CORPORATE — earnings, M&A, guidance, unusual-move news; watchlist names first
📅 UPCOMING — next-day earnings w/ consensus · this week's dividends · splits [OFFICIAL]
🔍 UNVERIFIED — single-source leads, labeled, with link
```

- ≤1,500 words; top-5 items get a one-line "why it matters"
- Every item: **source + link + timestamp**
- Ticker names rendered from `watchlist.json` (e.g. `00175.HK` → GEELY AUTO)

## Truth rules (guardrails)

1. `CONFIRMED` = ≥2 independent sources, or 1 official source
2. `OFFICIAL` = corporate actions (earnings dates, dividends, splits) ONLY from
   `bbg_exports/` or official disclosures (SEC EDGAR full-text search API, HKEX,
   JPX, KRX) — never from third-party calendars
3. `UNVERIFIED` = single-source lead (X/ZeroHedge/SeekingAlpha op-eds): include,
   labeled, with link, but never as a market-moving fact
4. Numbers must carry source; cross-check consensus vs actual when both exist

## Sources (free access only; no Reuters/WSJ)

- T1 (official): SEC EDGAR full-text search (efts.sec.gov/LATEST/search-index),
  HKEX, JPX, KRX disclosure feeds, official stats bureaus/central banks
- T2: Benzinga RSS, MarketBeat (dividends/splits), FT (user has subscription —
  use ft.com RSS), ZeroHedge RSS, SeekingAlpha free RSS, Nikkei Asia, SCMP,
  Korea Herald
- T3 (leads): X — verified accounts only, via `xurl` CLI IF installed; otherwise
  skip silently (v1: log warning, continue)

Sources are fetched via RSS where available (stdlib `feedparser` is NOT allowed —
parse XML with stdlib `xml.etree`), or minimal HTTP scrapes. Prefer RSS.

## Watchlist

`data/watchlist.json` — groups: hscei(50), hstech(30), nasdaq15, spx50, nikkei50,
extra(Samsung 005930.KS, SK Hynix 000660.KS). Ticker formats: `00175.HK`,
`6857.T`, `005930.KS`, `NVDA`. Include a `_sources` dict (already there).

## Bloomberg exporter (`scripts/bbg_export.py` — runs on USER's machine)

Uses `blpapi` (Bloomberg Python API, installed on user's machine, NOT here).
Universe: all watchlist names mapped to BBG format:
`00175.HK`→`175 HK Equity`, `6857.T`→`6857 JT Equity`, `005930.KS`→`005930 KS
Equity`, `NVDA`→`NVDA US Equity`.

One `ReferenceDataRequest` per calendar, output CSV per file:
- `earnings.csv`: EARN_ANN_DT (next 10 days), EARN_ANN_TIME, BEST_EPS (consensus),
  BEST_REV (consensus revenue), CURRENCY
- `dividends.csv`: EX_DIV_DT, DVD_AMT, DVD_YLD, DVD_PAY_DT, CURRENCY
- `splits.csv`: SPLIT_DT, SPLIT_RATIO (next 30 days)
- `econ.csv`: major global releases next 7 days (US/JP/CN/KR/HK/EU): release
  date/time, event name, consensus (via ECO fields on a fixed econ ticker list —
  include a small default list of econ tickers, e.g. CPI, FOMC, NFP equivalents)

Skip gracefully when a field is unavailable (blank → NaN, never crash). Print a
one-line summary per file. The user runs it manually nightly; files land in
`bbg_exports/` (gitignored).

## Scheduling (NOT built here)

Hermes cron runs `collect.py && digest.py` at 08/12/16/20/00/04 HKT and delivers
stdout to the chat. Digest window = last 4h (UTC conversion: HKT = UTC+8).

## Hard rules

- No price/quote fetching (no market-data dependency intraday)
- No new pip dependencies beyond `requests` (blpapi only imported inside
  bbg_export.py)
- Fail loudly on network errors (exit non-zero), never emit an empty digest
- Date handling in UTC internally, HKT for display
