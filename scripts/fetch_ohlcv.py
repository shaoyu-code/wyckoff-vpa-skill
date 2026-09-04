#!/usr/bin/env python3
"""Fetch daily/weekly OHLCV for US stocks, BTC, gold, and common aliases.

Uses Yahoo Finance chart API (no API key). Writes ohlcv.csv next to --out-dir.
Never prints the full series — only a one-line summary — so agent context stays small.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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

RANGE_FOR_DAYS = (
    (30, "1mo"),
    (90, "3mo"),
    (180, "6mo"),
    (370, "1y"),
    (750, "2y"),
    (1200, "5y"),
    (10_000, "max"),
)


def resolve_symbol(raw: str) -> str:
    s = raw.strip()
    key = s.lower().replace(" ", "").replace("/", "")
    if key in ALIASES:
        return ALIASES[key]
    return s.upper() if s.isascii() else s


def yahoo_range(days: int) -> str:
    for limit, label in RANGE_FOR_DAYS:
        if days <= limit:
            return label
    return "max"


def fetch_yahoo(symbol: str, interval: str, days: int) -> list[dict]:
    rng = yahoo_range(days)
    qs = urllib.parse.urlencode({"interval": interval, "range": rng, "events": "div,splits"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; wyckoff-vpa-skill/1.0)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    result = payload.get("chart", {}).get("result")
    if not result:
        err = payload.get("chart", {}).get("error") or payload
        raise RuntimeError(f"Yahoo returned no data for {symbol}: {err}")
    node = result[0]
    ts = node.get("timestamp") or []
    quote = (node.get("indicators", {}).get("quote") or [{}])[0]
    opens, highs, lows, closes, volumes = (
        quote.get("open") or [],
        quote.get("high") or [],
        quote.get("low") or [],
        quote.get("close") or [],
        quote.get("volume") or [],
    )
    rows = []
    for i, t in enumerate(ts):
        o, h, l, c = _f(opens, i), _f(highs, i), _f(lows, i), _f(closes, i)
        v = _f(volumes, i)
        if None in (o, h, l, c):
            continue
        rows.append(
            {
                "date": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
                "open": round(o, 6),
                "high": round(h, 6),
                "low": round(l, 6),
                "close": round(c, 6),
                "volume": 0.0 if v is None else float(v),
                "ts": t,
            }
        )
    if days and len(rows) > days:
        rows = rows[-days:]
    if len(rows) < 40:
        raise RuntimeError(f"Not enough bars for {symbol}: {len(rows)}")
    return rows


def _f(arr, i):
    if i >= len(arr):
        return None
    x = arr[i]
    if x is None:
        return None
    return float(x)


def write_csv(path: str, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})


def main() -> int:
    p = argparse.ArgumentParser(description="Fetch OHLCV via Yahoo Finance")
    p.add_argument("symbol", help="BTC-USD, GC=F, SPY, AAPL, or alias btc/gold/spx")
    p.add_argument("--days", type=int, default=750)
    p.add_argument("--interval", default="1d", choices=["1d", "1wk", "1h"])
    p.add_argument("--out-dir", required=True)
    args = p.parse_args()

    import os

    os.makedirs(args.out_dir, exist_ok=True)
    symbol = resolve_symbol(args.symbol)
    last_err = None
    rows = None
    for attempt in range(3):
        try:
            rows = fetch_yahoo(symbol, args.interval, args.days)
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.2 * (attempt + 1))
    if rows is None:
        print(f"ERROR fetch failed: {last_err}", file=sys.stderr)
        return 1

    csv_path = os.path.join(args.out_dir, "ohlcv.csv")
    write_csv(csv_path, rows)
    meta = {
        "symbol_in": args.symbol,
        "symbol": symbol,
        "interval": args.interval,
        "rows": len(rows),
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "low": min(r["low"] for r in rows),
        "high": max(r["high"] for r in rows),
        "last": rows[-1]["close"],
        "csv": csv_path,
    }
    with open(os.path.join(args.out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(
        f"OK {symbol} {meta['start']}→{meta['end']} n={meta['rows']} "
        f"px={meta['low']:.4g}–{meta['high']:.4g} last={meta['last']:.4g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
