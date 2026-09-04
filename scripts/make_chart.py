#!/usr/bin/env python3
"""Embed kline + vpa + analysis into a standalone HTML chart."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True, help="analysis directory")
    p.add_argument("--template", default=None)
    args = p.parse_args()

    d = Path(args.dir)
    skill_root = Path(__file__).resolve().parent.parent
    template = Path(args.template) if args.template else skill_root / "assets" / "chart_template.html"
    html = template.read_text(encoding="utf-8")

    kline_path = d / "kline.csv" if (d / "kline.csv").exists() else d / "ohlcv.csv"
    kline = pd.read_csv(kline_path)
    kline.columns = [c.lower() for c in kline.columns]
    if "date" not in kline.columns:
        kline = kline.reset_index().rename(columns={kline.index.name or "index": "date"})
        kline.columns = [c.lower() for c in kline.columns]

    vpa_path = d / "vpa.csv"
    vpa = pd.read_csv(vpa_path) if vpa_path.exists() else pd.DataFrame()

    analysis = load_json(d / "analysis.json")
    summary = load_json(d / "vpa_summary.json")

    k_records = kline[["date", "open", "high", "low", "close", "volume"]].to_dict(orient="records")
    v_records = []
    if len(vpa):
        cols = []
        for src, alias in [
            ("date", "date"),
            ("vol_ratio", "rvol"),
            ("rvol", "rvol"),
            ("vsa_residual", "effort_z"),
            ("effort_z", "effort_z"),
        ]:
            if src in vpa.columns and alias not in cols:
                vpa[alias] = vpa[src] if alias not in vpa.columns else vpa[alias]
        keep = [c for c in ["date", "rvol", "effort_z", "flag", "ma50", "ma200", "vol_ratio", "vsa_residual"] if c in vpa.columns]
        v_records = vpa[keep].to_dict(orient="records")

    html = html.replace("__KLINE_JSON__", json.dumps(k_records, ensure_ascii=False))
    html = html.replace("__VPA_JSON__", json.dumps(v_records, ensure_ascii=False))
    html = html.replace("__ANALYSIS_JSON__", json.dumps(analysis, ensure_ascii=False))
    html = html.replace("__SUMMARY_JSON__", json.dumps(summary, ensure_ascii=False))

    out = d / "chart.html"
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
