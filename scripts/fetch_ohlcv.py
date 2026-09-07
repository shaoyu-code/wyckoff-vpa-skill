#!/usr/bin/env python3
"""Fetch OHLCV from Yahoo Finance without an API key.

The script writes ``ohlcv.csv`` and ``meta.json`` into ``--out-dir``.  It
never prints the full series.  ``--days`` is retained for compatibility but
means "requested bars" for every interval.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from pipeline_common import SCHEMA_VERSION

ALIASES = {
    "btc": "BTC-USD",
    "bitcoin": "BTC-USD",
    "xbt": "BTC-USD",
    "gold": "GC=F",
    "xau": "GC=F",
    "xauusd": "GC=F",
    "gc": "GC=F",
    "spy": "SPY",
    "spx": "^GSPC",
    "sp500": "^GSPC",
    "ndx": "^NDX",
    "nas100": "^NDX",
    "qqq": "QQQ",
    "dxy": "DX-Y.NYB",
    "usd": "DX-Y.NYB",
    "usoil": "CL=F",
    "wti": "CL=F",
    "brent": "BZ=F",
    "tnx": "^TNX",
    "us10y": "^TNX",
    "us30y": "^TYX",
}

INTERVAL_SECONDS = {"1h": 3600, "1d": 86400, "1wk": 7 * 86400}


def resolve_symbol(raw: str) -> str:
    value = raw.strip()
    key = value.lower().replace(" ", "").replace("/", "")
    if key in ALIASES:
        return ALIASES[key]
    return value.upper() if value.isascii() else value


def calendar_span_days(requested_bars: int, interval: str) -> int:
    """Return a conservative calendar span that should contain N bars."""
    if requested_bars <= 0:
        raise ValueError("requested bars must be positive")
    factors = {"1h": 0.35, "1d": 2.2, "1wk": 8.2}
    return max(10, int(math.ceil(requested_bars * factors[interval])))


def _number(values: list[Any], index: int) -> float | None:
    if index >= len(values) or values[index] is None:
        return None
    try:
        value = float(values[index])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _format_timestamp(timestamp: int, interval: str) -> str:
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if interval == "1h" else dt.strftime("%Y-%m-%d")


def _last_bar_complete(timestamp: int, interval: str, market_meta: dict[str, Any], now_ts: int) -> bool:
    """Conservative completeness check for the newest bar."""
    if interval == "1h":
        return now_ts >= timestamp + INTERVAL_SECONDS[interval] + 120
    if interval == "1wk":
        return now_ts >= timestamp + INTERVAL_SECONDS[interval]

    instrument_type = str(market_meta.get("instrumentType") or "").upper()
    if instrument_type in {"CRYPTOCURRENCY", "CRYPTO"}:
        return now_ts >= timestamp + 86400

    market_state = str(market_meta.get("marketState") or "").upper()
    regular_market_time = market_meta.get("regularMarketTime")
    try:
        regular_market_time = int(regular_market_time)
    except (TypeError, ValueError):
        regular_market_time = None
    if market_state == "CLOSED" and regular_market_time and regular_market_time >= timestamp:
        return True
    return now_ts >= timestamp + 86400


def apply_split_adjustments(rows: list[dict[str, Any]], node: dict[str, Any]) -> list[dict[str, Any]]:
    """Idempotently back-adjust only an *unadjusted* split discontinuity.

    Yahoo commonly returns split-adjusted historical OHLC already.  Blindly
    applying the event ratio a second time would corrupt the series.  For each
    split we therefore compare the nearest pre/post closes.  Historical rows
    are adjusted only when the observed discontinuity is reasonably close to
    the reported split ratio.  The decision is recorded in ``meta.json``.
    Dividends are deliberately ignored because VPA needs traded OHLC rather
    than total-return prices.
    """
    split_node = ((node.get("events") or {}).get("splits") or {})
    splits: list[dict[str, Any]] = []
    for raw in split_node.values() if isinstance(split_node, dict) else []:
        try:
            timestamp = int(raw.get("date"))
            numerator = float(raw.get("numerator"))
            denominator = float(raw.get("denominator"))
        except (TypeError, ValueError, AttributeError):
            continue
        if numerator <= 0 or denominator <= 0:
            continue
        splits.append(
            {
                "timestamp": timestamp,
                "numerator": numerator,
                "denominator": denominator,
                "ratio": numerator / denominator,
                "splitRatio": raw.get("splitRatio"),
                "observed_pre_post_close_ratio": None,
                "adjustment_applied": False,
            }
        )
    if not splits:
        return []
    splits.sort(key=lambda item: item["timestamp"])
    ordered_rows = sorted(rows, key=lambda item: int(item["ts"]))
    for split in splits:
        before = next(
            (row for row in reversed(ordered_rows) if int(row["ts"]) < int(split["timestamp"])),
            None,
        )
        after = next(
            (row for row in ordered_rows if int(row["ts"]) >= int(split["timestamp"])),
            None,
        )
        if not before or not after:
            continue
        pre_close = float(before["close"])
        post_close = float(after["close"])
        if pre_close <= 0 or post_close <= 0:
            continue
        observed = pre_close / post_close
        reported = float(split["ratio"])
        split["observed_pre_post_close_ratio"] = round(observed, 8)
        # 35% log-distance tolerates market movement around the split while
        # clearly distinguishing a ratio such as 4:1 from an already adjusted
        # pre/post ratio near 1:1.
        split["adjustment_applied"] = abs(math.log(observed / reported)) <= math.log(1.35)

    for row in rows:
        price_factor = 1.0
        for split in splits:
            if split["adjustment_applied"] and int(row["ts"]) < split["timestamp"]:
                price_factor *= split["denominator"] / split["numerator"]
        if abs(price_factor - 1.0) <= 1e-15:
            continue
        for field in ("open", "high", "low", "close"):
            row[field] = round(float(row[field]) * price_factor, 8)
        if row.get("volume") is not None:
            row["volume"] = float(row["volume"]) / price_factor
    return splits


def fetch_yahoo(symbol: str, interval: str, requested_bars: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    now_ts = int(time.time())
    span_days = calendar_span_days(requested_bars, interval)
    period1 = now_ts - span_days * 86400
    params = urllib.parse.urlencode(
        {
            "interval": interval,
            "period1": period1,
            "period2": now_ts + 60,
            "events": "div,splits",
            "includePrePost": "false",
        }
    )
    encoded_symbol = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; wyckoff-vpa-skill/1.2)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    chart = payload.get("chart") or {}
    result = chart.get("result")
    if not result:
        raise RuntimeError(f"Yahoo returned no data for {symbol}: {chart.get('error') or 'unknown error'}")

    node = result[0]
    timestamps = node.get("timestamp") or []
    quote = (node.get("indicators", {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    rows: list[dict[str, Any]] = []
    skipped_missing_ohlc = 0
    for index, timestamp in enumerate(timestamps):
        open_ = _number(opens, index)
        high = _number(highs, index)
        low = _number(lows, index)
        close = _number(closes, index)
        volume = _number(volumes, index)
        if None in (open_, high, low, close):
            skipped_missing_ohlc += 1
            continue
        assert open_ is not None and high is not None and low is not None and close is not None
        if low > high or min(open_, close) < low or max(open_, close) > high:
            raise RuntimeError(
                f"Yahoo returned impossible OHLC for {symbol} at {_format_timestamp(int(timestamp), interval)}"
            )
        rows.append(
            {
                "date": _format_timestamp(int(timestamp), interval),
                "open": round(open_, 8),
                "high": round(high, 8),
                "low": round(low, 8),
                "close": round(close, 8),
                "volume": None if volume is None else float(volume),
                "ts": int(timestamp),
            }
        )

    split_events = apply_split_adjustments(rows, node)
    rows.sort(key=lambda item: item["ts"])
    deduplicated: dict[str, dict[str, Any]] = {row["date"]: row for row in rows}
    duplicates_removed = len(rows) - len(deduplicated)
    rows = sorted(deduplicated.values(), key=lambda item: item["ts"])

    market_meta = node.get("meta") or {}
    current_bar_complete = True
    if rows:
        current_bar_complete = _last_bar_complete(rows[-1]["ts"], interval, market_meta, now_ts)

    source_meta = {
        "source": "Yahoo Finance chart API",
        "source_url": url,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "market_state": market_meta.get("marketState"),
        "instrument_type": market_meta.get("instrumentType"),
        "exchange_name": market_meta.get("exchangeName"),
        "exchange_timezone": market_meta.get("exchangeTimezoneName"),
        "currency": market_meta.get("currency"),
        "skipped_missing_ohlc": skipped_missing_ohlc,
        "duplicates_removed": duplicates_removed,
        "current_bar_complete": current_bar_complete,
        "split_adjusted": True,
        "split_adjustment_method": "detect_pre_post_discontinuity_then_adjust_once",
        "split_events": split_events,
        "dividend_event_count": len(((node.get("events") or {}).get("dividends") or {})),
    }
    return rows, source_meta


def write_csv(path: str, rows: list[dict[str, Any]]) -> None:
    fields = ["date", "open", "high", "low", "close", "volume"]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: "" if row.get(field) is None else row.get(field) for field in fields})


def build_meta(
    *,
    symbol_in: str,
    symbol: str,
    interval: str,
    requested_bars: int,
    rows: list[dict[str, Any]],
    source_meta: dict[str, Any],
    dropped_incomplete_bars: int,
    csv_path: str,
) -> dict[str, Any]:
    volume_rows = sum(row.get("volume") is not None and float(row["volume"]) > 0 for row in rows)
    volume_coverage = volume_rows / len(rows) if rows else 0.0
    coverage_ratio = len(rows) / requested_bars if requested_bars else 0.0

    if len(rows) < 40:
        status = "insufficient_bars"
    elif volume_coverage < 0.95:
        status = "volume_unavailable"
    else:
        status = "ok"

    if coverage_ratio >= 0.95:
        coverage_status = "complete"
    elif coverage_ratio >= 0.75:
        coverage_status = "partial_but_usable"
    else:
        coverage_status = "insufficient"

    meta: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "symbol_in": symbol_in,
        "symbol": symbol,
        "interval": interval,
        "requested_bars": requested_bars,
        "received_bars": len(rows),
        "rows": len(rows),
        "coverage_ratio": round(coverage_ratio, 4),
        "coverage_status": coverage_status,
        "start": rows[0]["date"] if rows else None,
        "end": rows[-1]["date"] if rows else None,
        "low": min((row["low"] for row in rows), default=None),
        "high": max((row["high"] for row in rows), default=None),
        "last": rows[-1]["close"] if rows else None,
        "csv": csv_path,
        "data_quality": {
            "status": status,
            "ohlc_valid": True,
            "volume_coverage": round(volume_coverage, 4),
            "missing_or_zero_volume_rows": len(rows) - volume_rows,
            "source_latest_bar_complete": bool(source_meta.get("current_bar_complete", True)),
            "current_bar_complete": bool(source_meta.get("current_bar_complete", True) or dropped_incomplete_bars),
            "dropped_incomplete_bars": dropped_incomplete_bars,
            "trade_analysis_eligible": status == "ok",
            "skipped_missing_ohlc": source_meta.get("skipped_missing_ohlc", 0),
            "duplicates_removed": source_meta.get("duplicates_removed", 0),
        },
        "source": {key: value for key, value in source_meta.items() if key not in {"source_url"}},
    }
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch OHLCV via Yahoo Finance without an API key")
    parser.add_argument("symbol", help="BTC-USD, GC=F, SPY, AAPL, or alias btc/gold/spx")
    parser.add_argument("--days", type=int, default=750, help="Requested number of bars (legacy option name)")
    parser.add_argument("--interval", default="1d", choices=sorted(INTERVAL_SECONDS))
    parser.add_argument("--out-dir", required=True)
    parser.add_argument(
        "--include-incomplete-bar",
        action="store_true",
        help="Keep a possibly incomplete newest bar; meta.json will still flag it",
    )
    args = parser.parse_args()

    if args.days <= 0:
        parser.error("--days must be positive")

    os.makedirs(args.out_dir, exist_ok=True)
    symbol = resolve_symbol(args.symbol)
    rows: list[dict[str, Any]] | None = None
    source_meta: dict[str, Any] | None = None
    last_error: Exception | None = None

    for attempt in range(3):
        try:
            rows, source_meta = fetch_yahoo(symbol, args.interval, args.days)
            break
        except Exception as exc:  # noqa: BLE001 - retry boundary
            last_error = exc
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))

    if rows is None or source_meta is None:
        print(f"ERROR fetch failed: {last_error}", file=sys.stderr)
        return 1

    dropped_incomplete = 0
    if rows and not source_meta.get("current_bar_complete", True) and not args.include_incomplete_bar:
        rows = rows[:-1]
        dropped_incomplete = 1

    if len(rows) > args.days:
        rows = rows[-args.days :]

    csv_path = os.path.join(args.out_dir, "ohlcv.csv")
    write_csv(csv_path, rows)
    meta = build_meta(
        symbol_in=args.symbol,
        symbol=symbol,
        interval=args.interval,
        requested_bars=args.days,
        rows=rows,
        source_meta=source_meta,
        dropped_incomplete_bars=dropped_incomplete,
        csv_path=csv_path,
    )
    with open(os.path.join(args.out_dir, "meta.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2, allow_nan=False)

    print(
        f"OK {symbol} {meta['start']}→{meta['end']} n={meta['rows']}/{meta['requested_bars']} "
        f"quality={meta['data_quality']['status']} volume={meta['data_quality']['volume_coverage']:.1%} "
        f"last={meta['last']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
