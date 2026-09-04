#!/usr/bin/env python3
"""Turn candidates.json into a blank trade-plan skeleton.

The LLM fills narrative, confirms or rejects the TR, and writes analysis.json
using this skeleton so probability / risk fields are never omitted.
"""
from __future__ import annotations

import argparse
import json
import os


def skeleton(candidates: dict, symbol: str) -> dict:
    hint = candidates.get("structure_hint") or {}
    score = candidates.get("setup_score") or {}
    last = candidates.get("last_close")
    atr = hint.get("atr")
    hi, lo = hint.get("range_high"), hint.get("range_low")
    p = score.get("p_reach_t1_before_stop") or {}

    long_stop = None
    short_stop = None
    if last is not None and atr:
        long_stop = round(float(lo) - 0.6 * atr, 6) if lo else round(last - 1.2 * atr, 6)
        short_stop = round(float(hi) + 0.6 * atr, 6) if hi else round(last + 1.2 * atr, 6)

    return {
        "instrument": {"symbol": symbol, "last": last, "asof": candidates.get("last_bar")},
        "structure": {
            "cycle": None,
            "phase": None,
            "has_trading_range": hint.get("has_range"),
            "range_high": hi,
            "range_low": lo,
            "events_confirmed": [],
            "events_rejected": [],
            "narrative": "",
        },
        "bias": "no_trade" if not score.get("tradeable") else "undecided",
        "confluence": score,
        "main_plan": {
            "name": "主方案",
            "action": "wait" if not score.get("tradeable") else "propose",
            "setup": None,
            "direction": None,
            "trigger": "写出必须出现的量价条件，未出现则不准入场",
            "entry_zone": [lo, last] if lo and last else None,
            "stop": long_stop,
            "t1": hi,
            "t2": None,
            "position_risk_pct": 0.75,
            "rr_t1": None,
            "p_t1_before_stop": p,
            "hold_until": "T1 或结构破坏",
        },
        "alt_plan": {
            "name": "预案 A · 反向",
            "when": "主方案触发失败，且出现对立事件（例如 Spring 变成 SOW）",
            "action": "stand_aside_or_reverse",
            "trigger": None,
            "entry_zone": None,
            "stop": short_stop,
            "t1": lo,
            "p_t1_before_stop": {"low": max(30, (p.get("low") or 40) - 12), "high": max(40, (p.get("high") or 50) - 10)},
        },
        "invalidation": {
            "name": "预案 B · 作废",
            "hard_stop_beyond": long_stop,
            "structure_break": "日线收在区间外且下一根无法收回",
            "time_stop": "触发条件 8 根 K 线内未出现则取消本方案",
            "do_not": ["止损后立刻反手", "把概率当成必然", "加仓摊平"],
        },
        "manage": {
            "name": "预案 C · 持仓",
            "at_t1": "平 50%，剩余移到成本",
            "if_effort_no_result_against": "先减仓 50%",
            "if_news": "非农 / CPI / FOMC 当日不新开，已有仓把仓位砍到风险 0.4% 以内",
        },
        "risk": {
            "max_risk_pct_equity": 0.75,
            "max_correlated_positions": 2,
            "skip_if": [
                "画不出第二个人也认可的交易区间",
                "confluence < 6",
                "盈亏比 T1 < 1.6",
                "更高周期明确对着干",
            ],
        },
        "probability_notes": score.get("caveat"),
        "disclaimer": "研究与情景工具，不是投资建议，不保证盈利。",
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--candidates", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--out-dir", required=True)
    args = p.parse_args()
    with open(args.candidates, encoding="utf-8") as f:
        cand = json.load(f)
    plan = skeleton(cand, args.symbol)
    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, "plan_skeleton.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    print(f"OK wrote {path} bias={plan['bias']} score={plan['confluence'].get('confluence_0_10')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
