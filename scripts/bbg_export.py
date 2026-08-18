#!/usr/bin/env python3
"""Bloomberg calendar exporter (run on Bloomberg-enabled machine)."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
WATCHLIST_PATH = DATA_DIR / "watchlist.json"
EXPORT_DIR = DATA_DIR / "bbg_exports"

ECON_TICKERS = [
    # Verify against a terminal on the first live run; Bloomberg econ tickers
    # drift. "CPI YOY Index" / "FDTR Index" / "JNCPIYOY Index" are the current
    # US CPI, Fed target, and Japan CPI names (M7).
    "CPI YOY Index",
    "NFP TCH Index",
    "FDTR Index",
    "JNCPIYOY Index",
    "ECCNCPI YOY Index",
    "ECKRCPI YOY Index",
    "ECHKCPI YOY Index",
    "ECCPEMU YOY Index",
]


def _load_watchlist_tickers() -> list[str]:
    raw = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    tickers: list[str] = []
    for key, entries in raw.items():
        if key.startswith("_") or not isinstance(entries, list):
            continue
        for row in entries:
            ticker = str(row.get("ticker", "")).strip()
            if ticker and ticker not in tickers:
                tickers.append(ticker)
    return tickers


def ticker_to_bbg(ticker: str) -> str:
    ticker = ticker.strip().upper()
    if ticker.endswith(".HK"):
        numeric = str(int(ticker[:-3]))
        return f"{numeric} HK Equity"
    if ticker.endswith(".T"):
        return f"{ticker[:-2]} JT Equity"
    if ticker.endswith(".KS"):
        return f"{ticker[:-3]} KS Equity"
    # US: Bloomberg wants BRK/B, not BRK.B (M5).
    return f"{ticker.replace('.', '/')} US Equity"


def _safe_elem_value(elem: Any, field: str) -> str:
    if not elem.hasElement(field):
        return ""
    val = elem.getElement(field).getValue()
    if val is None:
        return ""
    return str(val)


def _date_in_horizon(raw: str, days_ahead: int) -> bool:
    if not raw:
        return False
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            dt = datetime.strptime(raw[:10], fmt).replace(tzinfo=UTC)
            break
        except ValueError:
            continue
    else:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        dt = dt.astimezone(UTC)

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    end = today + timedelta(days=days_ahead)
    return today <= dt <= end


def _collect_reference_rows(
    session: Any,
    securities: list[str],
    fields: list[str],
) -> list[dict[str, str]]:
    service = session.getService("//blp/refdata")
    request = service.createRequest("ReferenceDataRequest")
    for sec in securities:
        request.getElement("securities").appendValue(sec)
    for field in fields:
        request.getElement("fields").appendValue(field)
    session.sendRequest(request)

    rows: list[dict[str, str]] = []
    while True:
        # Timeout guard: a stalled session must bail, not hang forever (M6).
        event = session.nextEvent(30_000)
        if event.eventType() == event.TIMEOUT:
            raise RuntimeError("Bloomberg request timed out after 30s")
        for msg in event:
            if not msg.hasElement("securityData"):
                continue
            sec_data = msg.getElement("securityData")
            for idx in range(sec_data.numValues()):
                row = sec_data.getValueAsElement(idx)
                # Errored/unknown securities carry no fieldData — skip, don't
                # crash the whole nightly export (M6).
                if not row.hasElement("fieldData"):
                    continue
                security = row.getElementAsString("security")
                field_data = row.getElement("fieldData")
                out: dict[str, str] = {"security": security}
                for field in fields:
                    out[field] = _safe_elem_value(field_data, field)
                rows.append(out)
        if event.eventType() == event.RESPONSE:
            break
    return rows


def _write_csv(filename: str, rows: list[dict[str, str]], columns: list[str]) -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / filename
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})
    print(f"{filename}: {len(rows)} rows")


def export_bbg() -> None:
    try:
        import blpapi  # type: ignore
    except ImportError as exc:
        raise RuntimeError("blpapi not installed; run this script on Bloomberg machine") from exc

    watchlist = _load_watchlist_tickers()
    security_to_ticker = {ticker_to_bbg(t): t for t in watchlist}
    securities = list(security_to_ticker.keys())

    options = blpapi.SessionOptions()
    options.setServerHost("localhost")
    options.setServerPort(8194)
    session = blpapi.Session(options)
    if not session.start():
        raise RuntimeError("failed to start Bloomberg session")
    if not session.openService("//blp/refdata"):
        raise RuntimeError("failed to open Bloomberg refdata service")

    try:
        earn_fields = ["EARN_ANN_DT", "EARN_ANN_TIME", "BEST_EPS", "BEST_REV", "CRNCY"]
        earn_rows = _collect_reference_rows(session, securities, earn_fields)
        earn_out: list[dict[str, str]] = []
        for row in earn_rows:
            if not _date_in_horizon(row.get("EARN_ANN_DT", ""), days_ahead=10):
                continue
            earn_out.append(
                {
                    "security": row.get("security", ""),
                    "ticker": security_to_ticker.get(row.get("security", ""), ""),
                    "EARN_ANN_DT": row.get("EARN_ANN_DT", ""),
                    "EARN_ANN_TIME": row.get("EARN_ANN_TIME", ""),
                    "BEST_EPS": row.get("BEST_EPS", ""),
                    "BEST_REV": row.get("BEST_REV", ""),
                    "CURRENCY": row.get("CRNCY", ""),
                }
            )
        _write_csv(
            "earnings.csv",
            earn_out,
            ["security", "ticker", "EARN_ANN_DT", "EARN_ANN_TIME", "BEST_EPS", "BEST_REV", "CURRENCY"],
        )

        # EX_DIV_DT returns the most recent (usually past) ex-date; projected
        # fields are the reliable "upcoming" source (M7). Fall back per-field.
        div_fields = ["DVD_PROJ_DT", "DVD_PROJ_AMT", "DVD_PROJ_YLD", "DVD_PAY_DT", "CRNCY", "EX_DIV_DT"]
        div_rows = _collect_reference_rows(session, securities, div_fields)
        div_out: list[dict[str, str]] = []
        for row in div_rows:
            ex_dt = row.get("DVD_PROJ_DT", "") or row.get("EX_DIV_DT", "")
            if not _date_in_horizon(ex_dt, days_ahead=14):
                continue
            div_out.append(
                {
                    "security": row.get("security", ""),
                    "ticker": security_to_ticker.get(row.get("security", ""), ""),
                    "EX_DIV_DT": ex_dt,
                    "DVD_AMT": row.get("DVD_PROJ_AMT", "") or row.get("DVD_AMT", ""),
                    "DVD_YLD": row.get("DVD_PROJ_YLD", "") or row.get("DVD_YLD", ""),
                    "DVD_PAY_DT": row.get("DVD_PAY_DT", ""),
                    "CURRENCY": row.get("CRNCY", ""),
                }
            )
        _write_csv(
            "dividends.csv",
            div_out,
            ["security", "ticker", "EX_DIV_DT", "DVD_AMT", "DVD_YLD", "DVD_PAY_DT", "CURRENCY"],
        )

        split_fields = ["SPLIT_DT", "SPLIT_RATIO"]
        split_rows = _collect_reference_rows(session, securities, split_fields)
        split_out: list[dict[str, str]] = []
        for row in split_rows:
            if not _date_in_horizon(row.get("SPLIT_DT", ""), days_ahead=30):
                continue
            split_out.append(
                {
                    "security": row.get("security", ""),
                    "ticker": security_to_ticker.get(row.get("security", ""), ""),
                    "SPLIT_DT": row.get("SPLIT_DT", ""),
                    "SPLIT_RATIO": row.get("SPLIT_RATIO", ""),
                }
            )
        _write_csv(
            "splits.csv",
            split_out,
            ["security", "ticker", "SPLIT_DT", "SPLIT_RATIO"],
        )

        # BN_SURVEY_MEDIAN is the consensus field; ECO_FCAST is a fallback (M7).
        econ_fields = ["ECO_RELEASE_DT", "ECO_RELEASE_TIME", "BN_SURVEY_MEDIAN", "ECO_FCAST", "NAME"]
        econ_rows = _collect_reference_rows(session, ECON_TICKERS, econ_fields)
        econ_out: list[dict[str, str]] = []
        for row in econ_rows:
            if not _date_in_horizon(row.get("ECO_RELEASE_DT", ""), days_ahead=7):
                continue
            event_name = row.get("NAME", "") or row.get("security", "")
            date_raw = row.get("ECO_RELEASE_DT", "")
            time_raw = row.get("ECO_RELEASE_TIME", "")
            release_stamp = f"{date_raw} {time_raw}".strip()
            econ_out.append(
                {
                    "security": row.get("security", ""),
                    "event_name": event_name,
                    "release_date": date_raw,
                    "release_time": time_raw,
                    # Bloomberg returns LOCAL release times; never label them UTC (m8).
                    "release_datetime_local": release_stamp,
                    "consensus": row.get("BN_SURVEY_MEDIAN", "") or row.get("ECO_FCAST", ""),
                }
            )
        _write_csv(
            "econ.csv",
            econ_out,
            ["security", "event_name", "release_date", "release_time", "release_datetime_local", "consensus"],
        )
    finally:
        session.stop()


def _self_check() -> None:
    assert ticker_to_bbg("00175.HK") == "175 HK Equity"
    assert ticker_to_bbg("6857.T") == "6857 JT Equity"
    assert ticker_to_bbg("005930.KS") == "005930 KS Equity"
    assert ticker_to_bbg("NVDA") == "NVDA US Equity"
    assert _date_in_horizon("2099-01-01", 1) is False


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Bloomberg calendar fields to CSV")
    parser.add_argument("--self-check", action="store_true", help="run module self-check")
    args = parser.parse_args()

    if args.self_check:
        _self_check()
        print("bbg_export.py self-check passed")
        return

    export_bbg()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"bbg_export.py failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
