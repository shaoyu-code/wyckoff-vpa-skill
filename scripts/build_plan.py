#!/usr/bin/env python3
"""Build a non-executable analysis skeleton from immutable candidate evidence.

The skeleton contains reference identifiers and empty plan geometry.  It cannot
become executable until ``validate_analysis.py`` recomputes the evidence from
raw OHLCV, evaluates a structured trigger, and applies a trusted risk config.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pipeline_common import (
    DEFAULT_RISK_PCT,
    DEFAULT_SKIP_IF,
    MAX_CORRELATED_POSITIONS,
    MAX_CORRELATED_RISK_PCT,
    SCHEMA_VERSION,
    STANDARD_DISCLAIMER,
    analysis_instrument_profile,
    atomic_write_json,
    load_json,
    sha256_file,
)


def interval_time_stop(interval: str | None) -> int:
    return {"1h": 12, "4h": 12, "1d": 8, "1wk": 4}.get(interval or "1d", 8)


def setup_evidence_template() -> dict[str, Any]:
    return {
        "core_event_id": None,
        "core_event_hash": None,
        "retest_id": None,
        "retest_hash": None,
        "reviewed_event_ids": [],
    }


def trigger_template() -> dict[str, Any]:
    return {
        "text": "等待事件后回测收盘与成交量条件",
        "mode": "retest_close_volume",
        "bar_date": None,
        "price_rule": {"field": "close", "operator": None, "value": None},
        "volume_rule": {"field": "vol_rel", "operator": "<=", "value": 1.0},
        "confirmed": False,
        "evidence_hash": None,
    }


def probability_template(status: str) -> dict[str, Any]:
    return {
        "method": "heuristic_uncalibrated",
        "event": "reach_t1_before_stop",
        "status": status,
        "base_low": None,
        "base_high": None,
        "adjustments": [],
        "adjustment_total": 0,
        "low": None,
        "high": None,
        "unit": "percent",
        "cap": 72,
        "historical_win_rate": False,
        "conditional_on": [
            "structured trigger is true on a canonical bar",
            "the validated trading range remains intact",
            "no newer contradictory setup supersedes the selected pair",
        ],
    }


def position_template() -> dict[str, Any]:
    return {
        "source": "validator_risk_config",
        "risk_pct_equity": None,
        "account_equity": None,
        "account_currency": None,
        "fx_rate_account_to_quote": None,
        "risk_amount": None,
        "risk_amount_quote": None,
        "estimated_cost_per_unit": None,
        "actual_tradable_symbol": None,
        "contract_multiplier": None,
        "position_unit": None,
        "formula": "units = risk_amount_quote / (abs(entry_mid - stop) * contract_multiplier + estimated_cost_per_unit)",
        "units": None,
    }


def plan_template(name: str, unavailable_reason: str) -> dict[str, Any]:
    return {
        "name": name,
        "action": "wait",
        "direction": None,
        "setup": None,
        "when": "" if name.startswith("预案 A") else None,
        "setup_evidence": setup_evidence_template(),
        "trigger": trigger_template(),
        "entry_zone": None,
        "unavailable_reason": unavailable_reason,
        "stop": None,
        "t1": None,
        "t2": None,
        "rr_t1": None,
        "position": position_template() if name == "主方案" else None,
        "p_t1_before_stop": probability_template("pending_final_validation"),
    }


def skeleton(
    candidates: dict[str, Any],
    weekly: dict[str, Any],
    symbol: str,
    *,
    daily_candidates_sha256: str | None = None,
    weekly_candidates_sha256: str | None = None,
) -> dict[str, Any]:
    hint = candidates.get("structure_hint") if isinstance(candidates.get("structure_hint"), dict) else {}
    daily_quality = candidates.get("data_quality") if isinstance(candidates.get("data_quality"), dict) else {}
    weekly_quality = weekly.get("data_quality") if isinstance(weekly.get("data_quality"), dict) else {}
    interval = str(candidates.get("interval") or "1d")
    resolved_symbol = str(candidates.get("symbol") or symbol)
    profile = analysis_instrument_profile(resolved_symbol)

    main = plan_template("主方案", "No confirmed directional setup or validated trigger yet.")
    alt = plan_template("预案 A · 反向", "No independently validated reverse setup yet.")
    alt.pop("position", None)
    alt["p_t1_before_stop"]["status"] = "pending_independent_validation"

    return {
        "schema_version": SCHEMA_VERSION,
        "lineage": {
            "daily_run_id": candidates.get("run_id"),
            "daily_candidates_sha256": daily_candidates_sha256,
            "weekly_run_id": weekly.get("run_id"),
            "weekly_candidates_sha256": weekly_candidates_sha256,
        },
        "instrument": {
            "symbol": resolved_symbol,
            "requested_symbol": symbol,
            "last": candidates.get("last_close"),
            "asof": candidates.get("last_bar"),
            "interval": interval,
            **profile,
            "actual_tradable_symbol": None,
        },
        "data_quality": {
            "daily": daily_quality,
            "weekly": weekly_quality or {"status": "unavailable", "trade_analysis_eligible": False},
        },
        "reference_levels": {
            "candidate_range_high": hint.get("range_high"),
            "candidate_range_low": hint.get("range_low"),
            "candidate_range_width_atr": hint.get("width_atr"),
            "candidate_range_confirmed": False,
            "note": "Reference levels are not executable until deterministic validation succeeds.",
        },
        "structure": {
            "cycle": "unclear",
            "phase": None,
            "has_trading_range": False,
            "range_high": None,
            "range_low": None,
            "range_start": None,
            "range_end": None,
            "events_confirmed": [],
            "events_rejected": [],
            "setup_evidence": setup_evidence_template(),
            "narrative": "",
        },
        "decision": {
            "recommendation": "no_trade",
            "watch_bias": "neutral",
            "ready_to_execute": False,
            "reason": "Pending raw-evidence, trigger, risk, and schema validation.",
        },
        "confluence": {
            "stage": "pending_final_validation",
            "final_score_0_10": None,
            "parts": {
                "trading_range": None,
                "event_sequence": None,
                "volume_confirmation": None,
                "higher_timeframe": None,
                "reward_to_risk": None,
            },
        },
        "main_plan": main,
        "alt_plan": alt,
        "invalidation": {
            "name": "预案 B · 作废",
            "hard_stop_beyond": None,
            "structure_break": "",
            "time_stop": {
                "bars": interval_time_stop(interval),
                "starts_after_trigger": True,
                "rule": "",
            },
            "do_not": ["止损后立即反手", "加仓摊平", "把概率当成必然"],
        },
        "manage": {
            "name": "预案 C · 持仓",
            "at_t1": "到达 T1 平 50%，剩余仓位止损移到成本附近",
            "if_effort_no_result_against": "出现反向努力无结果恶化时先减仓 50%",
            "if_news": "重大宏观事件前不新开；已有仓按预案降风险，不临时扩大止损",
            "additional_rules": [],
        },
        "risk": {
            "default_risk_pct_equity": DEFAULT_RISK_PCT,
            "allowed_without_user_override_pct": [0.5, DEFAULT_RISK_PCT],
            "max_correlated_risk_pct_equity": MAX_CORRELATED_RISK_PCT,
            "max_correlated_positions": MAX_CORRELATED_POSITIONS,
            "skip_if": list(DEFAULT_SKIP_IF),
            "correlation_notes": [],
            "market_context_conflicts": [],
        },
        "probability": probability_template("pending_final_validation"),
        "validation_status": "unvalidated",
        "disclaimer": STANDARD_DISCLAIMER,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a non-executable plan skeleton")
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--weekly-candidates", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    try:
        candidates = load_json(args.candidates)
        weekly = load_json(args.weekly_candidates)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 2

    plan = skeleton(
        candidates,
        weekly,
        args.symbol,
        daily_candidates_sha256=sha256_file(args.candidates),
        weekly_candidates_sha256=sha256_file(args.weekly_candidates),
    )
    output = Path(args.out_dir, "plan_skeleton.json")
    atomic_write_json(output, plan)
    print(
        f"OK wrote {output} action=wait daily_run={plan['lineage']['daily_run_id']} "
        f"weekly_run={plan['lineage']['weekly_run_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
