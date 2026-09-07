#!/usr/bin/env python3
"""Deterministic offline fixtures shared by regression and adversarial tests."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_plan as bp  # noqa: E402
import validate_analysis as va  # noqa: E402
from pipeline_common import SCHEMA_VERSION, STANDARD_DISCLAIMER, atomic_write_json  # noqa: E402


def run(command: list[str], expected: int | None = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if expected is not None and result.returncode != expected:
        raise AssertionError(
            f"command returned {result.returncode}, expected {expected}\n"
            f"COMMAND: {' '.join(command)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain an object")
    return value


def write(path: Path, value: Any) -> None:
    atomic_write_json(path, value)


def current_test_day() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc).date())


def make_daily_frame() -> pd.DataFrame:
    """Range fixture with a recent Spring and a later low-volume retest."""
    n = 252
    rng = np.random.default_rng(7)
    close = 105 + 3 * np.sin(np.arange(n) / 5) + rng.normal(0, 0.25, n)
    open_ = close + rng.normal(0, 0.15, n)
    high = np.maximum(open_, close) + rng.uniform(0.3, 0.6, n)
    low = np.minimum(open_, close) - rng.uniform(0.3, 0.6, n)
    volume = rng.integers(900, 1200, n).astype(float)

    # Spring candidate and deterministic follow-through/retest near the end.
    close[242], open_[242], low[242], high[242], volume[242] = 102.2, 101.0, 98.8, 102.8, 850.0
    for index in range(243, 247):
        open_[index], high[index], low[index], close[index], volume[index] = 104.4, 105.0, 103.9, 104.5, 1300.0
    close[247], open_[247], low[247], high[247], volume[247] = 102.4, 102.1, 100.6, 102.9, 600.0
    tail = [
        (104.9, 105.4, 104.6, 105.0),
        (105.0, 105.5, 104.7, 105.1),
        (105.1, 105.6, 104.8, 105.2),
        (105.2, 105.7, 104.9, 105.3),
    ]
    for index, values in enumerate(tail, start=248):
        open_[index], high[index], low[index], close[index] = values
        volume[index] = 1000.0

    dates = pd.date_range(end=current_test_day(), periods=n, freq="D")
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def make_weekly_frame(direction: str = "up", *, end: str | None = None) -> pd.DataFrame:
    n = 260
    rng = np.random.default_rng(19 if direction == "up" else 23)
    if direction == "up":
        center = np.linspace(50, 130, n)
    elif direction == "down":
        center = np.linspace(130, 50, n)
    else:
        center = 90 + 6 * np.sin(np.arange(n) / 7)
    close = center + rng.normal(0, 0.25, n)
    open_ = close + rng.normal(0, 0.12, n)
    high = np.maximum(open_, close) + 0.5
    low = np.minimum(open_, close) - 0.5
    volume = np.full(n, 1000.0)
    dates = pd.date_range(end=pd.Timestamp(end) if end else current_test_day(), periods=n, freq="W-FRI")
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def make_trend_frame(n: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    close = np.linspace(50, 130, n) + rng.normal(0, 0.2, n)
    open_ = close + rng.normal(0, 0.1, n)
    high = np.maximum(open_, close) + 0.5
    low = np.minimum(open_, close) - 0.5
    volume = np.full(n, 1000.0)
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"date": dates.strftime("%Y-%m-%d"), "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )


def write_meta(run_dir: Path, frame: pd.DataFrame, *, symbol: str, interval: str, symbol_in: str | None = None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "schema_version": SCHEMA_VERSION,
        "symbol_in": symbol_in or symbol,
        "symbol": symbol,
        "interval": interval,
        "requested_bars": int(len(frame)),
        "received_bars": int(len(frame)),
        "rows": int(len(frame)),
        "coverage_ratio": 1.0,
        "coverage_status": "complete",
        "start": str(frame.iloc[0]["date"]),
        "end": str(frame.iloc[-1]["date"]),
        "low": float(frame["low"].min()),
        "high": float(frame["high"].max()),
        "last": float(frame.iloc[-1]["close"]),
        "csv": str(run_dir / "ohlcv.csv"),
        "data_quality": {
            "status": "ok",
            "ohlc_valid": True,
            "volume_coverage": float((frame["volume"] > 0).mean()),
            "missing_or_zero_volume_rows": int((frame["volume"] <= 0).sum()),
            "source_latest_bar_complete": True,
            "current_bar_complete": True,
            "dropped_incomplete_bars": 0,
            "trade_analysis_eligible": True,
            "skipped_missing_ohlc": 0,
            "duplicates_removed": 0,
        },
        "source": {"source": "synthetic-test-fixture", "split_adjusted": True, "split_events": []},
    }
    frame.to_csv(run_dir / "ohlcv.csv", index=False)
    write(run_dir / "meta.json", meta)


def compute_run(run_dir: Path) -> None:
    run(
        [
            sys.executable,
            str(SCRIPTS / "compute_vpa.py"),
            "--csv",
            str(run_dir / "ohlcv.csv"),
            "--meta",
            str(run_dir / "meta.json"),
            "--out-dir",
            str(run_dir),
        ]
    )


def build_runs(root: Path, *, symbol: str = "SPY", weekly_direction: str = "up") -> tuple[Path, Path]:
    daily_dir = root / "daily"
    weekly_dir = root / "weekly"
    daily = make_daily_frame()
    weekly = make_weekly_frame(weekly_direction)
    write_meta(daily_dir, daily, symbol=symbol, interval="1d")
    write_meta(weekly_dir, weekly, symbol=symbol, interval="1wk")
    compute_run(daily_dir)
    compute_run(weekly_dir)
    return daily_dir, weekly_dir


def _confirmed(canonical: dict[str, Any], why: str) -> dict[str, Any]:
    return {
        "id": canonical["id"],
        "evidence_hash": canonical["evidence_hash"],
        "date": canonical["date"],
        "type": canonical["type"],
        "bar_index": canonical["bar_index"],
        "price": canonical["price"],
        "extreme_price": canonical["extreme_price"],
        "boundary": canonical.get("boundary"),
        "related_event_id": canonical.get("related_event_id"),
        "why": why,
    }


def _rejected(canonical: dict[str, Any], reason: str = "Not selected after canonical review.") -> dict[str, Any]:
    return {
        "id": canonical["id"],
        "evidence_hash": canonical["evidence_hash"],
        "date": canonical["date"],
        "type": canonical["type"],
        "reason": reason,
    }


def reviewable_events(candidates: dict[str, Any]) -> list[dict[str, Any]]:
    bars = int(candidates.get("bars") or 0)
    horizon_start = max(0, bars - 80)
    return [
        event
        for event in candidates.get("events") or []
        if isinstance(event, dict)
        and event.get("type") in va.REVIEW_EVENT_TYPES
        and event.get("confirmable") is True
        and int(event.get("bar_index") or -1) >= horizon_start
    ]


def latest_pair(candidates: dict[str, Any], *, setup: str | None = None) -> dict[str, Any]:
    pairs = va.event_pair_index(candidates)
    if setup:
        pairs = [item for item in pairs if item["setup"] == setup]
    if not pairs:
        raise AssertionError(f"fixture produced no complete event pair for {setup or 'any setup'}")
    return pairs[-1]


def build_plan_file(daily_dir: Path, weekly_dir: Path, *, requested_symbol: str = "SPY") -> dict[str, Any]:
    run(
        [
            sys.executable,
            str(SCRIPTS / "build_plan.py"),
            "--candidates",
            str(daily_dir / "candidates.json"),
            "--weekly-candidates",
            str(weekly_dir / "candidates.json"),
            "--symbol",
            requested_symbol,
            "--out-dir",
            str(daily_dir),
        ]
    )
    return load(daily_dir / "plan_skeleton.json")


def populate_trade_analysis(
    daily_dir: Path,
    weekly_dir: Path,
    *,
    requested_symbol: str = "SPY",
    actual_tradable_symbol: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = load(daily_dir / "candidates.json")
    analysis = build_plan_file(daily_dir, weekly_dir, requested_symbol=requested_symbol)
    pair = latest_pair(candidates, setup="spring_retest")
    core, retest = pair["core"], pair["retest"]
    hint = candidates["structure_hint"]
    reviewable = reviewable_events(candidates)

    confirmed_ids = {core["id"], retest["id"]}
    analysis["reference_levels"]["candidate_range_confirmed"] = True
    analysis["structure"].update(
        {
            "cycle": "range",
            "phase": "D",
            "has_trading_range": True,
            "range_high": hint["range_high"],
            "range_low": hint["range_low"],
            "range_start": str((pd.Timestamp(core["date"]) - pd.Timedelta(days=70)).date()),
            "range_end": candidates["last_bar"],
            "events_confirmed": [
                _confirmed(core, "Spring reclaimed the canonical range boundary."),
                _confirmed(retest, "Later low-volume test held above the Spring extreme."),
            ],
            "events_rejected": [_rejected(event) for event in reviewable if event["id"] not in confirmed_ids],
            "narrative": "A reproducible range and a post-Spring low-volume retest are present.",
        }
    )
    evidence = {
        "core_event_id": core["id"],
        "core_event_hash": core["evidence_hash"],
        "retest_id": retest["id"],
        "retest_hash": retest["evidence_hash"],
        "reviewed_event_ids": sorted(event["id"] for event in reviewable),
    }
    analysis["structure"]["setup_evidence"] = deepcopy(evidence)
    analysis["main_plan"]["setup_evidence"] = deepcopy(evidence)
    analysis["decision"] = {
        "recommendation": "long",
        "watch_bias": "long",
        "ready_to_execute": False,
        "reason": "Latest canonical Spring-retest pair meets the pre-validation conditions.",
    }

    core_atr = float(core["atr"])
    boundary = float(retest["boundary"])
    stop = round(float(core["extreme_price"]) - 0.4 * core_atr, 8)
    entry_low = round(boundary + 0.15 * core_atr, 8)
    entry_high = round(boundary + 0.45 * core_atr, 8)
    t1 = round(float(hint["range_high"]), 8)
    t2 = round(t1 + 0.50 * (float(hint["range_high"]) - float(hint["range_low"])), 8)
    analysis["main_plan"].update(
        {
            "action": "enter_long",
            "direction": "long",
            "setup": "spring_retest",
            "when": None,
            "entry_zone": [entry_low, entry_high],
            "unavailable_reason": "",
            "stop": stop,
            "t1": t1,
            "t2": t2,
            "rr_t1": None,
        }
    )
    analysis["main_plan"]["trigger"] = {
        "text": "日线回测后收盘守住箱体下沿且相对量不高于 1.0，才执行主方案。",
        "mode": "retest_close_volume",
        "bar_date": retest["date"],
        "price_rule": {"field": "close", "operator": ">=", "value": boundary},
        "volume_rule": {"field": "vol_rel", "operator": "<=", "value": 1.0},
        "confirmed": True,
        "evidence_hash": None,
    }

    alt_mid = boundary - 0.05 * core_atr
    analysis["alt_plan"].update(
        {
            "action": "wait",
            "direction": "short",
            "setup": "sow_lpsy",
            "when": "仅当未来出现 SOW 后 LPSY 且缩量反抽失败时，才考虑反向。",
            "entry_zone": [round(alt_mid - 0.15 * core_atr, 8), round(alt_mid + 0.15 * core_atr, 8)],
            "unavailable_reason": "Opposite SOW-LPSY sequence has not been confirmed.",
            "stop": round(alt_mid + 1.0 * core_atr, 8),
            "t1": round(alt_mid - 1.8 * core_atr, 8),
            "t2": round(alt_mid - 3.0 * core_atr, 8),
            "rr_t1": None,
        }
    )
    analysis["alt_plan"]["trigger"] = {
        "text": "未来收盘跌破下沿后，缩量反抽下沿不过才评估反向。",
        "mode": "retest_close_volume",
        "bar_date": None,
        "price_rule": {"field": "close", "operator": "<=", "value": boundary},
        "volume_rule": {"field": "vol_rel", "operator": "<=", "value": 1.0},
        "confirmed": False,
        "evidence_hash": None,
    }
    analysis["alt_plan"]["p_t1_before_stop"]["status"] = "pending_independent_validation"
    analysis["invalidation"].update(
        {
            "hard_stop_beyond": stop,
            "structure_break": "日线收盘击穿 Spring 极值外的硬止损，主方案立即作废。",
        }
    )
    analysis["invalidation"]["time_stop"]["rule"] = "触发后 8 根日线未向 T1 推进则退出。"
    analysis["risk"]["correlation_notes"] = ["同一风险桶的总风险不得超过账户权益 1.5%。"]
    analysis["risk"]["market_context_conflicts"] = []
    analysis["disclaimer"] = STANDARD_DISCLAIMER
    write(daily_dir / "analysis.json", analysis)

    risk_config = {
        "schema_version": SCHEMA_VERSION,
        "account_equity": 100000.0,
        "account_currency": "USD",
        "risk_pct_equity": 0.75,
        "fx_rate_account_to_quote": 1.0,
        "estimated_cost_per_unit": 0.02,
        "current_correlated_risk_pct_equity": 0.0,
        "current_correlated_positions": 0,
        "actual_tradable_symbol": actual_tradable_symbol or candidates["symbol"],
        "override_acknowledgement": None,
    }
    write(daily_dir / "risk_config.json", risk_config)
    return analysis, risk_config


def populate_no_trade_analysis(daily_dir: Path, weekly_dir: Path, *, requested_symbol: str = "SPY") -> dict[str, Any]:
    candidates = load(daily_dir / "candidates.json")
    analysis = build_plan_file(daily_dir, weekly_dir, requested_symbol=requested_symbol)
    reviewable = reviewable_events(candidates)
    analysis["structure"]["events_rejected"] = [
        _rejected(event, "Candidate reviewed but no executable sequence was selected.") for event in reviewable
    ]
    analysis["structure"]["narrative"] = "候选事件已审查，但没有满足全部开仓闸门的方案。"
    analysis["decision"] = {
        "recommendation": "no_trade",
        "watch_bias": "neutral",
        "ready_to_execute": False,
        "reason": "No validated setup; remain flat and wait for a later retest.",
    }
    analysis["alt_plan"]["when"] = "未来出现相反方向的完整事件和回测后再评估。"
    analysis["invalidation"]["structure_break"] = "当前无有效交易方案；任何预填价位均作废。"
    analysis["invalidation"]["time_stop"]["rule"] = "无触发时不启动时间止损。"
    analysis["risk"]["correlation_notes"] = ["空仓状态不新增相关风险。"]
    analysis["disclaimer"] = STANDARD_DISCLAIMER
    write(daily_dir / "analysis.json", analysis)
    return analysis


def validate_fixture(
    daily_dir: Path,
    weekly_dir: Path,
    *,
    risk_config: Path | None = None,
    expected: int | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    out = output_dir or daily_dir
    command = [
        sys.executable,
        str(SCRIPTS / "validate_analysis.py"),
        "--analysis",
        str(daily_dir / "analysis.json"),
        "--run-dir",
        str(daily_dir),
        "--weekly-dir",
        str(weekly_dir),
        "--out-dir",
        str(out),
    ]
    if risk_config:
        command.extend(["--risk-config", str(risk_config)])
    run(command, expected=expected)
    return load(out / "validation.json")


def clone_fixture(source: Path, destination: Path) -> tuple[Path, Path]:
    shutil.copytree(source, destination)
    return destination / "daily", destination / "weekly"
