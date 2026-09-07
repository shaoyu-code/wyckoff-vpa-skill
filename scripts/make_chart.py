#!/usr/bin/env python3
"""Render a standalone local HTML chart from validated run artifacts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

import validate_analysis as va
from pipeline_common import SCHEMA_VERSION, sha256_json


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def safe_script_json(value: Any) -> str:
    """Encode JSON safely inside a script element."""
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def records(frame: pd.DataFrame, columns: list[str]) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    existing = [column for column in columns if column in frame.columns]
    clean = frame[existing].copy().astype(object).where(pd.notna(frame[existing]), None)
    return clean.to_dict(orient="records")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a standalone Wyckoff VPA HTML chart")
    parser.add_argument("--dir", required=True, help="Run directory")
    parser.add_argument("--template", default=None)
    args = parser.parse_args()

    directory = Path(args.dir)
    skill_root = Path(__file__).resolve().parent.parent
    template = Path(args.template) if args.template else skill_root / "assets" / "chart_template.html"
    if not template.exists():
        print(f"ERROR missing template: {template}", file=sys.stderr)
        return 2

    ohlcv_path = directory / "ohlcv.csv"
    if not ohlcv_path.exists():
        print(f"ERROR missing {ohlcv_path}", file=sys.stderr)
        return 2
    ohlcv = pd.read_csv(ohlcv_path)
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(ohlcv.columns):
        print(f"ERROR OHLCV missing columns: {sorted(required - set(ohlcv.columns))}", file=sys.stderr)
        return 2

    vpa_path = directory / "vpa.csv"
    vpa = pd.read_csv(vpa_path) if vpa_path.exists() else pd.DataFrame()
    analysis_path = directory / "analysis.validated.json"
    validation_path = directory / "validation.json"
    if not analysis_path.exists() or not validation_path.exists():
        print("ERROR chart requires analysis.validated.json and validation.json", file=sys.stderr)
        return 2
    analysis = load_json(analysis_path)
    validation = load_json(validation_path)
    run_verification = va.verify_run(directory.resolve())
    if run_verification.get("ok") is not True:
        print(
            "ERROR refusing to render unverified run evidence: "
            + "; ".join(run_verification.get("errors") or ["unknown evidence error"]),
            file=sys.stderr,
        )
        return 2
    evidence = validation.get("evidence") if isinstance(validation.get("evidence"), dict) else {}
    candidates = run_verification.get("candidates") or {}
    if validation.get("schema_version") != SCHEMA_VERSION:
        print("ERROR validation schema version does not match this renderer", file=sys.stderr)
        return 2
    if evidence.get("daily_run_id") != candidates.get("run_id"):
        print("ERROR validation is not bound to this daily run_id", file=sys.stderr)
        return 2
    if evidence.get("daily_candidates_sha256") != run_verification.get("candidates_file_sha256"):
        print("ERROR validation is not bound to this candidates.json", file=sys.stderr)
        return 2
    if validation.get("valid") is not True:
        print("ERROR refusing to render an invalid analysis", file=sys.stderr)
        return 2
    if validation.get("analysis_validated_sha256") != sha256_json(analysis):
        print("ERROR analysis.validated.json hash does not match validation.json", file=sys.stderr)
        return 2
    action = ((analysis.get("main_plan") or {}).get("action"))
    status = analysis.get("validation_status")
    if action in {"enter_long", "enter_short"}:
        if validation.get("trade_gates_pass") is not True or status != "validated_trade":
            print("ERROR refusing to render a trade action without passed trade gates", file=sys.stderr)
            return 2
    elif status != "validated_no_trade":
        print("ERROR safe/no-trade chart requires validation_status=validated_no_trade", file=sys.stderr)
        return 2
    candidates = run_verification.get("candidates") or {}

    html = template.read_text(encoding="utf-8")
    replacements = {
        "__KLINE_JSON__": safe_script_json(records(ohlcv, ["date", "open", "high", "low", "close", "volume"])),
        "__VPA_JSON__": safe_script_json(
            records(
                vpa,
                [
                    "date",
                    "vol_rel",
                    "vsa_residual",
                    "sma50",
                    "sma200",
                    "wave_id",
                    "wave_direction",
                    "wave_volume_rel",
                    "wave_efficiency",
                ],
            )
        ),
        "__ANALYSIS_JSON__": safe_script_json(analysis),
        "__VALIDATION_JSON__": safe_script_json(validation),
        "__CANDIDATES_JSON__": safe_script_json(candidates),
    }
    for placeholder, value in replacements.items():
        if placeholder not in html:
            print(f"ERROR template missing placeholder {placeholder}", file=sys.stderr)
            return 2
        html = html.replace(placeholder, value)

    output = directory / "chart.html"
    output.write_text(html, encoding="utf-8")
    print(f"OK wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
