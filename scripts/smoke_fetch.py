#!/usr/bin/env python3
"""Network smoke test for the public Yahoo fetch/compute path.

This script is intentionally excluded from offline CI.  It never contacts a
broker and never writes an order; it only verifies that BTC-USD, GC=F, and SPY
produce usable daily and weekly artifacts in the current network environment.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline_common import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test public Yahoo OHLCV fetching")
    parser.add_argument("--symbols", nargs="+", default=["BTC-USD", "GC=F", "SPY"])
    parser.add_argument("--daily-bars", type=int, default=250)
    parser.add_argument("--weekly-bars", type=int, default=220)
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_root = Path(args.out_dir) if args.out_dir else ROOT / "smoke-runs" / stamp
    output_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    failed = False

    for symbol in args.symbols:
        safe = "".join(char if char.isalnum() or char in "._=-" else "_" for char in symbol)
        symbol_result: dict[str, Any] = {"symbol": symbol, "intervals": {}}
        for interval, bars in [("1d", args.daily_bars), ("1wk", args.weekly_bars)]:
            directory = output_root / safe / interval
            directory.mkdir(parents=True, exist_ok=True)
            fetch = run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "fetch_ohlcv.py"),
                    symbol,
                    "--days",
                    str(bars),
                    "--interval",
                    interval,
                    "--out-dir",
                    str(directory),
                ]
            )
            record: dict[str, Any] = {
                "fetch_returncode": fetch.returncode,
                "fetch_summary": fetch.stdout.strip() or fetch.stderr.strip(),
            }
            if fetch.returncode != 0 or not (directory / "ohlcv.csv").exists():
                record["status"] = "fetch_failed"
                failed = True
                symbol_result["intervals"][interval] = record
                continue

            compute = run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "compute_vpa.py"),
                    "--csv",
                    str(directory / "ohlcv.csv"),
                    "--out-dir",
                    str(directory),
                ]
            )
            record["compute_returncode"] = compute.returncode
            record["compute_summary"] = compute.stdout.strip() or compute.stderr.strip()
            try:
                meta = load_object(directory / "meta.json")
                candidates = load_object(directory / "candidates.json")
                quality = candidates.get("data_quality") or {}
                record.update(
                    {
                        "resolved_symbol": meta.get("symbol"),
                        "rows": meta.get("rows"),
                        "start": meta.get("start"),
                        "end": meta.get("end"),
                        "coverage_status": meta.get("coverage_status"),
                        "data_quality_status": quality.get("status"),
                        "trade_analysis_eligible": quality.get("trade_analysis_eligible"),
                    }
                )
                record["status"] = (
                    "ok"
                    if compute.returncode == 0
                    and quality.get("status") == "ok"
                    and quality.get("trade_analysis_eligible") is True
                    else "degraded"
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                record["status"] = "artifact_invalid"
                record["error"] = str(exc)
            if record["status"] != "ok":
                failed = True
            symbol_result["intervals"][interval] = record
        results.append(symbol_result)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "out_dir": str(output_root),
        "valid": not failed,
        "results": results,
    }
    manifest_path = output_root / "smoke_results.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"SMOKE_VALID={str(not failed).lower()} manifest={manifest_path}")
    for item in results:
        states = ", ".join(
            f"{interval}:{details.get('status')}" for interval, details in item["intervals"].items()
        )
        print(f"  {item['symbol']} {states}")
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
