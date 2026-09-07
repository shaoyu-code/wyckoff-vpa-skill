#!/usr/bin/env python3
"""Compute VPA features and rule-based *candidate* Wyckoff events.

This script never confirms Phase A-E and never declares a setup tradeable.  It
writes compact evidence for an agent and a later deterministic validator:

- vpa.csv
- data_quality.json
- structure_hint.json
- candidates.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from pipeline_common import (
    CANONICAL_COMPUTE_PARAMETERS,
    SCHEMA_VERSION,
    atomic_write_json,
    derive_run_id,
    sha256_file,
    sha256_json,
    utc_now_iso,
)

CORE_EVENT_TYPES = {"SC", "BC", "SPRING", "UT", "SOS", "SOW"}
VPA_OUTPUT_COLUMNS = [
    "date", "open", "high", "low", "close", "volume", "atr", "vol_rel", "range_rel",
    "vsa_residual", "sma50", "sma200", "wave_id", "wave_direction", "wave_volume",
    "wave_price_span", "wave_result", "wave_efficiency", "wave_volume_rel",
]


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    previous = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous).abs(),
            (df["low"] - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(n, min_periods=n).mean()


def rolling_linreg_residual(y: np.ndarray, x: np.ndarray, lookback: int) -> np.ndarray:
    """Residual of normalized range (y) on normalized volume (x)."""
    length = len(y)
    output = np.full(length, np.nan)
    for index in range(lookback * 2, length):
        window = slice(index - lookback + 1, index + 1)
        xx = x[window]
        yy = y[window]
        mask = np.isfinite(xx) & np.isfinite(yy)
        if mask.sum() < max(20, lookback // 3):
            continue
        xx = xx[mask]
        yy = yy[mask]
        variance = np.var(xx)
        if variance < 1e-12:
            continue
        slope = np.cov(xx, yy, ddof=0)[0, 1] / variance
        intercept = yy.mean() - slope * xx.mean()
        correlation = np.corrcoef(xx, yy)[0, 1]
        if not np.isfinite(correlation) or correlation < 0.15 or slope <= 0:
            output[index] = 0.0
            continue
        output[index] = yy[-1] - (intercept + slope * xx[-1])
    return output


def load_ohlcv(csv_path: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    frame = pd.read_csv(csv_path)
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"csv missing columns: {sorted(missing)}")

    input_rows = len(frame)
    parsed_dates = pd.to_datetime(frame["date"], errors="coerce", utc=True)
    if parsed_dates.isna().any():
        raise ValueError(f"invalid dates: {int(parsed_dates.isna().sum())}")
    frame = frame.copy()
    frame["_parsed_date"] = parsed_dates

    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if frame[["open", "high", "low", "close"]].isna().any().any():
        counts = frame[["open", "high", "low", "close"]].isna().sum().to_dict()
        raise ValueError(f"missing/non-numeric OHLC values: {counts}")

    values = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("non-finite OHLC values")
    finite_volume = frame["volume"].dropna().to_numpy(dtype=float)
    if finite_volume.size and (not np.isfinite(finite_volume).all() or (finite_volume < 0).any()):
        raise ValueError("non-finite or negative volume values")

    frame = frame.sort_values("_parsed_date", kind="mergesort")
    duplicates_removed = int(frame.duplicated("_parsed_date", keep="last").sum())
    frame = frame.drop_duplicates("_parsed_date", keep="last").reset_index(drop=True)

    invalid_ohlc = (
        (frame["low"] > frame["high"])
        | (frame[["open", "close"]].min(axis=1) < frame["low"])
        | (frame[["open", "close"]].max(axis=1) > frame["high"])
    )
    if invalid_ohlc.any():
        dates = frame.loc[invalid_ohlc, "_parsed_date"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist()
        raise ValueError(f"impossible OHLC rows: {dates[:5]}")

    had_time = frame["date"].astype(str).str.contains("T| ", regex=True).any()
    if had_time:
        frame["date"] = frame["_parsed_date"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        frame["date"] = frame["_parsed_date"].dt.strftime("%Y-%m-%d")
    frame = frame.drop(columns=["_parsed_date"])

    return frame, {
        "input_rows": int(input_rows),
        "rows_after_cleaning": int(len(frame)),
        "duplicates_removed": duplicates_removed,
        "ohlc_valid": True,
    }


def load_meta(csv_path: str, explicit_meta: str | None) -> dict[str, Any]:
    candidate = Path(explicit_meta) if explicit_meta else Path(csv_path).with_name("meta.json")
    if not candidate.exists():
        return {}
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"meta_read_error": str(exc)}
    return value if isinstance(value, dict) else {"meta_read_error": "meta root is not an object"}


def data_quality(
    frame: pd.DataFrame,
    *,
    vsa_lookback: int,
    base: dict[str, Any],
    fetch_meta: dict[str, Any],
) -> dict[str, Any]:
    minimum_feature_bars = max(80, 2 * vsa_lookback + 1)
    positive_volume = frame["volume"].notna() & (frame["volume"] > 0)
    volume_coverage = float(positive_volume.mean()) if len(frame) else 0.0
    fetch_quality = fetch_meta.get("data_quality") if isinstance(fetch_meta.get("data_quality"), dict) else {}
    current_bar_complete = bool(fetch_quality.get("current_bar_complete", True))
    fetch_coverage_status = fetch_meta.get("coverage_status")

    reasons: list[str] = []
    if len(frame) < minimum_feature_bars:
        reasons.append(f"need at least {minimum_feature_bars} bars, got {len(frame)}")
    if volume_coverage < 0.95:
        reasons.append(f"positive volume coverage {volume_coverage:.1%} is below 95%")
    if not current_bar_complete:
        reasons.append("newest bar may be incomplete")
    if fetch_coverage_status == "insufficient":
        reasons.append("fetch coverage is below 75% of requested bars")
    if fetch_meta.get("meta_read_error"):
        reasons.append(f"meta unreadable: {fetch_meta['meta_read_error']}")

    if len(frame) < 40:
        status = "insufficient_bars"
    elif volume_coverage < 0.95:
        status = "volume_unavailable"
    elif not current_bar_complete:
        status = "incomplete_latest_bar"
    elif fetch_coverage_status == "insufficient":
        status = "insufficient_fetch_coverage"
    elif len(frame) < minimum_feature_bars:
        status = "insufficient_feature_depth"
    else:
        status = "ok"

    return {
        "status": status,
        **base,
        "minimum_feature_bars": minimum_feature_bars,
        "volume_coverage": round(volume_coverage, 4),
        "missing_or_zero_volume_rows": int(len(frame) - positive_volume.sum()),
        "current_bar_complete": current_bar_complete,
        "trade_analysis_eligible": status == "ok",
        "reasons": reasons,
        "fetch_coverage_status": fetch_coverage_status,
    }


def threshold_waves(
    frame: pd.DataFrame,
    *,
    reversal_atr: float = 0.8,
    reversal_pct: float = 0.015,
) -> pd.DataFrame:
    """ATR/percentage-threshold directional waves, not one-bar sign flips."""
    length = len(frame)
    columns = [
        "wave_id",
        "wave_direction",
        "wave_volume",
        "wave_price_span",
        "wave_result",
        "wave_efficiency",
        "wave_volume_rel",
    ]
    if length == 0:
        return pd.DataFrame(columns=columns)

    close = frame["close"].to_numpy(dtype=float)
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    volume = frame["volume"].fillna(0.0).to_numpy(dtype=float)
    atr_values = frame["atr"].to_numpy(dtype=float)

    def threshold(index: int, price: float) -> float:
        atr_value = atr_values[index]
        atr_component = reversal_atr * atr_value if np.isfinite(atr_value) else 0.0
        pct_component = reversal_pct * abs(price)
        return max(atr_component, pct_component, 1e-12)

    segments: list[tuple[int, int, int]] = []
    start = 0
    pivot_price = close[0]
    extreme_price = close[0]
    extreme_index = 0
    direction = 0

    for index in range(1, length):
        price = close[index]
        if direction == 0:
            change = price - pivot_price
            if abs(change) >= threshold(index, pivot_price):
                direction = 1 if change > 0 else -1
                extreme_price = price
                extreme_index = index
            elif abs(price - pivot_price) > abs(extreme_price - pivot_price):
                extreme_price = price
                extreme_index = index
            continue

        if direction > 0:
            if price >= extreme_price:
                extreme_price = price
                extreme_index = index
            elif extreme_price - price >= threshold(index, extreme_price):
                segments.append((start, extreme_index, 1))
                start = min(extreme_index + 1, index)
                direction = -1
                pivot_price = extreme_price
                extreme_price = price
                extreme_index = index
        else:
            if price <= extreme_price:
                extreme_price = price
                extreme_index = index
            elif price - extreme_price >= threshold(index, extreme_price):
                segments.append((start, extreme_index, -1))
                start = min(extreme_index + 1, index)
                direction = 1
                pivot_price = extreme_price
                extreme_price = price
                extreme_index = index

    final_direction = direction
    if final_direction == 0:
        final_direction = 1 if close[-1] >= close[0] else -1
    segments.append((start, length - 1, final_direction))

    wave_id = np.zeros(length, dtype=int)
    wave_direction = np.zeros(length, dtype=int)
    wave_volume = np.zeros(length, dtype=float)
    wave_span = np.zeros(length, dtype=float)
    wave_result = np.zeros(length, dtype=float)
    wave_efficiency = np.zeros(length, dtype=float)
    segment_volumes: list[float] = []
    segment_records: list[tuple[int, int, int, float]] = []

    for identifier, (left, right, side) in enumerate(segments, start=1):
        if right < left:
            continue
        selector = slice(left, right + 1)
        total_volume = float(volume[selector].sum())
        span = float(high[selector].max() - low[selector].min())
        result = float(close[right] - close[left])
        efficiency = abs(result) / span if span > 0 else 0.0
        wave_id[selector] = identifier
        wave_direction[selector] = side
        wave_volume[selector] = total_volume
        wave_span[selector] = span
        wave_result[selector] = result
        wave_efficiency[selector] = efficiency
        segment_volumes.append(total_volume)
        segment_records.append((left, right, identifier, total_volume))

    wave_volume_rel = np.full(length, np.nan)
    for idx, (left, right, _identifier, total_volume) in enumerate(segment_records):
        prior = [value for value in segment_volumes[max(0, idx - 10) : idx] if value > 0]
        if prior:
            median = float(np.median(prior))
            if median > 0:
                wave_volume_rel[left : right + 1] = total_volume / median

    return pd.DataFrame(
        {
            "wave_id": wave_id,
            "wave_direction": wave_direction,
            "wave_volume": wave_volume,
            "wave_price_span": wave_span,
            "wave_result": wave_result,
            "wave_efficiency": wave_efficiency,
            "wave_volume_rel": wave_volume_rel,
        }
    )


def structure_hint(frame: pd.DataFrame) -> dict[str, Any]:
    tail = frame.tail(80).reset_index(drop=True)
    if len(tail) < 40:
        return {"has_range": False, "reason": "not enough bars", "lookback_bars": int(len(tail))}

    atr_last = float(tail["atr"].iloc[-1])
    if not math.isfinite(atr_last) or atr_last <= 0:
        return {"has_range": False, "reason": "ATR unavailable", "lookback_bars": int(len(tail))}

    range_high = float(tail["high"].quantile(0.95))
    range_low = float(tail["low"].quantile(0.05))
    width = range_high - range_low
    if width <= 0:
        return {"has_range": False, "reason": "non-positive candidate width", "lookback_bars": int(len(tail))}

    closes = tail["close"].to_numpy(dtype=float)
    x = np.arange(len(closes), dtype=float)
    slope, intercept = np.polyfit(x, closes, 1)
    fitted = intercept + slope * x
    ss_total = float(np.sum((closes - closes.mean()) ** 2))
    ss_resid = float(np.sum((closes - fitted) ** 2))
    r_squared = 0.0 if ss_total <= 1e-12 else max(0.0, 1.0 - ss_resid / ss_total)
    net_move_ratio = abs(closes[-1] - closes[0]) / width
    directional_span_ratio = abs(slope) * len(closes) / width
    trendish = (net_move_ratio > 0.70 and r_squared > 0.35) or (r_squared > 0.65 and directional_span_ratio > 0.60)

    tolerance = 0.55 * atr_last
    upper_touches = int((tail["high"] >= range_high - tolerance).sum())
    lower_touches = int((tail["low"] <= range_low + tolerance).sum())
    width_atr = width / atr_last
    has_range = width_atr >= 3.0 and upper_touches >= 2 and lower_touches >= 2 and not trendish

    last = float(tail["close"].iloc[-1])
    return {
        "has_range": bool(has_range),
        "lookback_bars": int(len(tail)),
        "range_high": round(range_high, 8),
        "range_low": round(range_low, 8),
        "range_mid": round((range_high + range_low) / 2.0, 8),
        "width_atr": round(width_atr, 3),
        "upper_touches": upper_touches,
        "lower_touches": lower_touches,
        "close_location_in_range": round((last - range_low) / width, 3),
        "net_move_vs_width": round(net_move_ratio, 3),
        "linear_r_squared": round(r_squared, 3),
        "directional_span_vs_width": round(directional_span_ratio, 3),
        "trendish": bool(trendish),
        "last": round(last, 8),
        "atr": round(atr_last, 8),
        "note": "candidate only; an agent must confirm a reproducible trading range",
    }


def trend_filter(frame: pd.DataFrame) -> dict[str, Any]:
    if not len(frame):
        return {"direction": "unavailable"}
    last = frame.iloc[-1]
    sma50 = last.get("sma50")
    sma200 = last.get("sma200")
    close = float(last["close"])
    if pd.isna(sma50) or pd.isna(sma200):
        return {"direction": "unavailable", "reason": "SMA depth unavailable"}
    sma50 = float(sma50)
    sma200 = float(sma200)
    if close > sma50 > sma200:
        direction = "up"
    elif close < sma50 < sma200:
        direction = "down"
    else:
        direction = "mixed"
    return {
        "direction": direction,
        "close": round(close, 8),
        "sma50": round(sma50, 8),
        "sma200": round(sma200, 8),
        "note": "same-timeframe trend filter; only a separately computed weekly file is HTF evidence",
    }


def _safe_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not math.isfinite(float(value)) else round(float(value), 8)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def compact_rows(frame: pd.DataFrame, left: int, right: int) -> list[dict[str, Any]]:
    columns = [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "atr",
        "vol_rel",
        "range_rel",
        "vsa_residual",
        "wave_id",
        "wave_direction",
        "wave_volume_rel",
        "wave_efficiency",
    ]
    records = frame.iloc[max(0, left) : min(len(frame), right)][columns].to_dict(orient="records")
    return [{key: _safe_value(value) for key, value in record.items()} for record in records]


def _candidate(
    frame: pd.DataFrame,
    index: int,
    event_type: str,
    rule: str,
    *,
    boundary: float | None = None,
    followthrough_bars: int = 3,
    related_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = frame.iloc[index]
    downside_types = {"SC", "SPRING", "SOW", "TEST_AFTER_SPRING", "LPS"}
    extreme = float(row["low"] if event_type in downside_types else row["high"])
    confirmable = index + followthrough_bars < len(frame)
    event = {
        "id": f"{event_type}:{row['date']}",
        "date": str(row["date"]),
        "price": round(float(row["close"]), 8),
        "extreme_price": round(extreme, 8),
        "atr": _safe_value(row.get("atr")),
        "type": event_type,
        "bar_index": int(index),
        "vol_rel": _safe_value(row.get("vol_rel")),
        "range_rel": _safe_value(row.get("range_rel")),
        "vsa_residual": _safe_value(row.get("vsa_residual")),
        "wave_volume_rel": _safe_value(row.get("wave_volume_rel")),
        "wave_efficiency": _safe_value(row.get("wave_efficiency")),
        "boundary": None if boundary is None else round(float(boundary), 8),
        "rule": rule,
        "status": "candidate" if confirmable else "pending_followthrough",
        "confirmable": confirmable,
        "related_event_id": related_event.get("id") if related_event else None,
        "context_bars": compact_rows(frame, index - 3, index + followthrough_bars + 1),
    }
    event["evidence_hash"] = sha256_json(event)
    return event


def _last_event(events: Iterable[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
    return next((event for event in reversed(list(events)) if event["type"] == event_type), None)


def detect_candidates(frame: pd.DataFrame, hint: dict[str, Any]) -> list[dict[str, Any]]:
    if len(frame) < 40:
        return []
    events: list[dict[str, Any]] = []
    start = max(20, len(frame) - 160)
    range_event_start = max(20, len(frame) - int(hint.get("lookback_bars") or 80))
    range_high = hint.get("range_high")
    range_low = hint.get("range_low")
    has_range = bool(hint.get("has_range"))

    for index in range(start, len(frame)):
        row = frame.iloc[index]
        atr_value = row.get("atr")
        vol_rel = row.get("vol_rel")
        range_rel = row.get("range_rel")
        if pd.isna(atr_value) or pd.isna(vol_rel) or pd.isna(range_rel):
            continue
        atr_value = float(atr_value)
        vol_rel = float(vol_rel)
        range_rel = float(range_rel)
        if atr_value <= 0:
            continue

        bar_range = float(row["high"] - row["low"])
        close_location = 0.5 if bar_range <= 0 else float((row["close"] - row["low"]) / bar_range)
        prior_index = max(0, index - 10)
        prior_move = float(frame["close"].iloc[index - 1] - frame["close"].iloc[prior_index]) if index else 0.0

        if vol_rel >= 2.0 and range_rel >= 1.7:
            if prior_move <= -atr_value and close_location <= 0.6:
                event_type = "SC"
            elif prior_move >= atr_value and close_location >= 0.4:
                event_type = "BC"
            else:
                event_type = "CLIMAX"
            last_same = _last_event(events, event_type)
            if last_same is None or index - int(last_same["bar_index"]) >= 5:
                events.append(
                    _candidate(
                        frame,
                        index,
                        event_type,
                        "high-volume wide-range candidate with prior directional movement",
                        followthrough_bars=5,
                    )
                )

        residual = row.get("vsa_residual")
        residual_value = float(residual) if pd.notna(residual) else math.nan
        if vol_rel >= 1.8 and (range_rel <= 0.85 or (math.isfinite(residual_value) and residual_value <= -0.4)):
            last_effort = _last_event(events, "EFFORT_NO_RESULT")
            if last_effort is None or index - int(last_effort["bar_index"]) >= 3:
                events.append(
                    _candidate(
                        frame,
                        index,
                        "EFFORT_NO_RESULT",
                        "high relative volume produced compressed or below-regression range",
                        followthrough_bars=1,
                    )
                )

        if index < range_event_start or not has_range or range_high is None or range_low is None:
            continue
        range_high_f = float(range_high)
        range_low_f = float(range_low)

        if row["low"] < range_low_f - 0.15 * atr_value and row["close"] > range_low_f and vol_rel < 1.4:
            last_same = _last_event(events, "SPRING")
            if last_same is None or index - int(last_same["bar_index"]) >= 8:
                events.append(
                    _candidate(
                        frame,
                        index,
                        "SPRING",
                        "pierced candidate range low and closed back inside on non-expanding volume",
                        boundary=range_low_f,
                        followthrough_bars=3,
                    )
                )

        if row["high"] > range_high_f + 0.15 * atr_value and row["close"] < range_high_f and vol_rel < 1.4:
            last_same = _last_event(events, "UT")
            if last_same is None or index - int(last_same["bar_index"]) >= 8:
                events.append(
                    _candidate(
                        frame,
                        index,
                        "UT",
                        "pierced candidate range high and closed back inside on non-expanding volume",
                        boundary=range_high_f,
                        followthrough_bars=3,
                    )
                )

        if row["close"] > range_high_f + 0.10 * atr_value and vol_rel >= 1.2:
            last_same = _last_event(events, "SOS")
            if last_same is None or index - int(last_same["bar_index"]) >= 5:
                events.append(
                    _candidate(
                        frame,
                        index,
                        "SOS",
                        "closed above candidate range high with expanding volume",
                        boundary=range_high_f,
                        followthrough_bars=2,
                    )
                )

        if row["close"] < range_low_f - 0.10 * atr_value and vol_rel >= 1.2:
            last_same = _last_event(events, "SOW")
            if last_same is None or index - int(last_same["bar_index"]) >= 5:
                events.append(
                    _candidate(
                        frame,
                        index,
                        "SOW",
                        "closed below candidate range low with expanding volume",
                        boundary=range_low_f,
                        followthrough_bars=2,
                    )
                )

    # Add explicit retest candidates after core events.  These are still candidates.
    core_events = [event for event in events if event["type"] in {"SPRING", "UT", "SOS", "SOW"}]
    for core in core_events:
        core_index = int(core["bar_index"])
        boundary = float(core["boundary"])
        for index in range(core_index + 1, min(len(frame), core_index + 13)):
            row = frame.iloc[index]
            atr_value = row.get("atr")
            vol_rel = row.get("vol_rel")
            if pd.isna(atr_value) or pd.isna(vol_rel):
                continue
            atr_value = float(atr_value)
            vol_rel = float(vol_rel)
            if core["type"] == "SPRING":
                condition = (
                    row["low"] > float(core["extreme_price"])
                    and row["low"] <= boundary + 0.55 * atr_value
                    and row["close"] >= boundary
                    and vol_rel <= 1.0
                )
                retest_type = "TEST_AFTER_SPRING"
            elif core["type"] == "UT":
                condition = (
                    row["high"] < float(core["extreme_price"])
                    and row["high"] >= boundary - 0.55 * atr_value
                    and row["close"] <= boundary
                    and vol_rel <= 1.0
                )
                retest_type = "TEST_AFTER_UT"
            elif core["type"] == "SOS":
                condition = (
                    row["low"] >= boundary - 0.15 * atr_value
                    and row["low"] <= boundary + 0.55 * atr_value
                    and row["close"] >= boundary
                    and vol_rel <= 1.0
                )
                retest_type = "LPS"
            else:
                condition = (
                    row["high"] <= boundary + 0.15 * atr_value
                    and row["high"] >= boundary - 0.55 * atr_value
                    and row["close"] <= boundary
                    and vol_rel <= 1.0
                )
                retest_type = "LPSY"
            if condition:
                events.append(
                    _candidate(
                        frame,
                        index,
                        retest_type,
                        "low-volume retest after a core range event",
                        boundary=boundary,
                        followthrough_bars=1,
                        related_event=core,
                    )
                )
                break

    return compact_events(events, limit=30)


def compact_events(events: list[dict[str, Any]], limit: int = 30) -> list[dict[str, Any]]:
    """Keep recent events while retaining any core event referenced by a retest."""
    ordered = sorted(events, key=lambda event: (int(event["bar_index"]), str(event["type"])))
    by_id = {str(event.get("id")): event for event in ordered if event.get("id")}
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for event in reversed(ordered):
        event_id = str(event.get("id") or "")
        related_id = str(event.get("related_event_id") or "")
        additions = [event]
        if related_id and related_id not in selected_ids and related_id in by_id:
            additions.append(by_id[related_id])
        for addition in additions:
            addition_id = str(addition.get("id") or "")
            if addition_id and addition_id in selected_ids:
                continue
            selected.append(addition)
            if addition_id:
                selected_ids.add(addition_id)
        if len(selected) >= limit:
            break
    return sorted(selected, key=lambda event: (int(event["bar_index"]), str(event["type"])))


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    return _safe_value(value)


def continuous_contract_roll_flags(frame: pd.DataFrame, symbol: str | None, lookback: int = 80) -> list[dict[str, Any]]:
    """Flag discontinuities that can be caused by a continuous-futures roll.

    This is deliberately conservative. A flag does not assert that a roll
    occurred; it blocks a trade plan until an actual contract is checked.
    """
    code = str(symbol or "").upper()
    if code not in {"GC=F", "MGC=F"} or len(frame) < 20:
        return []
    previous_close = frame["close"].shift(1)
    previous_atr = frame["atr"].shift(1)
    opening_gap = (frame["open"] - previous_close).abs()
    pct_gap = opening_gap / previous_close.abs().replace(0, np.nan)
    threshold = pd.concat([5.0 * previous_atr, 0.07 * previous_close.abs()], axis=1).max(axis=1)
    suspicious = (opening_gap > threshold) & (pct_gap > 0.07)
    output: list[dict[str, Any]] = []
    for index in frame.index[suspicious & (frame.index >= max(0, len(frame) - lookback))]:
        output.append(
            {
                "date": str(frame.at[index, "date"]),
                "opening_gap": round(float(opening_gap.at[index]), 8),
                "gap_pct": round(float(pct_gap.at[index]), 6),
                "previous_atr": None if pd.isna(previous_atr.at[index]) else round(float(previous_atr.at[index]), 8),
            }
        )
    return output


def build_artifacts(
    frame: pd.DataFrame,
    *,
    base_quality: dict[str, Any],
    fetch_meta: dict[str, Any],
    vsa_lookback: int,
    wave_reversal_atr: float,
    wave_reversal_pct: float,
    ohlcv_sha256: str,
    meta_sha256: str | None,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return feature frame, quality, hint, candidates, and deterministic parameters."""
    working = frame.copy()
    quality = data_quality(working, vsa_lookback=vsa_lookback, base=base_quality, fetch_meta=fetch_meta)

    working["atr"] = atr(working, 14)
    working["range"] = working["high"] - working["low"]
    volume_median = working["volume"].rolling(20, min_periods=10).median().replace(0, np.nan)
    working["vol_rel"] = working["volume"] / volume_median
    working["range_rel"] = working["range"] / working["atr"].replace(0, np.nan)
    working["sma50"] = working["close"].rolling(50, min_periods=50).mean()
    working["sma200"] = working["close"].rolling(200, min_periods=200).mean()
    working["vsa_residual"] = rolling_linreg_residual(
        working["range_rel"].to_numpy(dtype=float),
        working["vol_rel"].to_numpy(dtype=float),
        vsa_lookback,
    )
    wave_frame = threshold_waves(
        working,
        reversal_atr=wave_reversal_atr,
        reversal_pct=wave_reversal_pct,
    )
    for column in wave_frame.columns:
        working[column] = wave_frame[column].to_numpy()

    symbol = fetch_meta.get("symbol")
    interval = fetch_meta.get("interval")
    identity_errors: list[str] = []
    if not isinstance(symbol, str) or not symbol.strip():
        identity_errors.append("meta.json does not provide a resolved symbol")
    if interval not in {"1h", "1d", "1wk"}:
        identity_errors.append("meta.json does not provide a supported interval")
    roll_flags = continuous_contract_roll_flags(working, symbol)
    if roll_flags:
        identity_errors.append("possible continuous gold futures roll inside the active 80-bar window")

    if identity_errors:
        quality["reasons"] = list(quality.get("reasons") or []) + identity_errors
        quality["trade_analysis_eligible"] = False
        if roll_flags:
            quality["status"] = "continuous_contract_roll_risk"
        elif quality.get("status") == "ok":
            quality["status"] = "missing_source_identity"
    quality["continuous_contract_roll_flags"] = roll_flags

    hint = structure_hint(working)
    current_trend_filter = trend_filter(working)
    events = detect_candidates(working, hint) if quality["status"] == "ok" else []
    confirmable_core = [event for event in events if event["type"] in CORE_EVENT_TYPES and event["confirmable"]]
    retests = [event for event in events if event["type"] in {"TEST_AFTER_SPRING", "TEST_AFTER_UT", "LPS", "LPSY"}]

    hard_blocks = list(quality.get("reasons") or [])
    if not hint.get("has_range"):
        hard_blocks.append("no reproducible trading-range candidate")
    if not confirmable_core:
        hard_blocks.append("no confirmable core Wyckoff event candidate")
    if not retests:
        hard_blocks.append("no rule-based post-event retest candidate")

    parameters = {
        "vsa_lookback": int(vsa_lookback),
        "wave_reversal_atr": float(wave_reversal_atr),
        "wave_reversal_pct": float(wave_reversal_pct),
        "event_review_horizon_bars": int(CANONICAL_COMPUTE_PARAMETERS["event_review_horizon_bars"]),
    }
    run_id = derive_run_id(str(symbol or "UNKNOWN"), str(interval or "UNKNOWN"), ohlcv_sha256, parameters)
    precheck = {
        "stage": "pre_llm",
        "final_confluence_available": False,
        "tradeable": False,
        "candidate_review_eligible": quality["status"] == "ok",
        "candidate_setup_present": bool(hint.get("has_range") and confirmable_core and retests),
        "confirmable_core_event_count": len(confirmable_core),
        "retest_candidate_count": len(retests),
        "hard_blocks": hard_blocks,
        "note": "candidate flags cannot authorize a trade; validate analysis.json against raw run artifacts",
    }
    candidates = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "lineage": {
            "ohlcv_sha256": ohlcv_sha256,
            "meta_sha256": meta_sha256,
            "parameters_sha256": sha256_json(parameters),
        },
        "symbol": symbol,
        "symbol_in": fetch_meta.get("symbol_in"),
        "bars": int(len(working)),
        "interval": interval,
        "last_bar": str(working["date"].iloc[-1]) if len(working) else None,
        "last_close": round(float(working["close"].iloc[-1]), 8) if len(working) else None,
        "data_quality": quality,
        "events": events,
        "recent_bars": compact_rows(working, max(0, len(working) - 12), len(working)),
        "structure_hint": hint,
        "trend_filter": current_trend_filter,
        "precheck": precheck,
    }
    return working, quality, hint, candidates, parameters


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute VPA features and candidate events")
    parser.add_argument("--csv", required=True, help="ohlcv.csv from fetch_ohlcv.py")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--meta", default=None, help="Optional meta.json; defaults to a sibling of --csv")
    parser.add_argument("--vsa-lookback", type=int, default=CANONICAL_COMPUTE_PARAMETERS["vsa_lookback"])
    parser.add_argument("--wave-reversal-atr", type=float, default=CANONICAL_COMPUTE_PARAMETERS["wave_reversal_atr"])
    parser.add_argument("--wave-reversal-pct", type=float, default=CANONICAL_COMPUTE_PARAMETERS["wave_reversal_pct"])
    args = parser.parse_args()

    supplied_parameters = {
        "vsa_lookback": int(args.vsa_lookback),
        "wave_reversal_atr": float(args.wave_reversal_atr),
        "wave_reversal_pct": float(args.wave_reversal_pct),
        "event_review_horizon_bars": int(CANONICAL_COMPUTE_PARAMETERS["event_review_horizon_bars"]),
    }
    if supplied_parameters != CANONICAL_COMPUTE_PARAMETERS:
        parser.error(
            "production validation requires the canonical compute parameters: "
            + json.dumps(CANONICAL_COMPUTE_PARAMETERS, sort_keys=True)
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = Path(args.csv).resolve()
    meta_path = Path(args.meta).resolve() if args.meta else csv_path.with_name("meta.json")
    try:
        frame, base_quality = load_ohlcv(str(csv_path))
    except ValueError as exc:
        print(f"DATA_INVALID: {exc}")
        return 2

    fetch_meta = load_meta(str(csv_path), str(meta_path) if meta_path.exists() else None)
    ohlcv_sha = sha256_file(csv_path)
    meta_sha = sha256_file(meta_path) if meta_path.exists() else None
    feature_frame, quality, hint, candidates, parameters = build_artifacts(
        frame,
        base_quality=base_quality,
        fetch_meta=fetch_meta,
        vsa_lookback=args.vsa_lookback,
        wave_reversal_atr=args.wave_reversal_atr,
        wave_reversal_pct=args.wave_reversal_pct,
        ohlcv_sha256=ohlcv_sha,
        meta_sha256=meta_sha,
    )

    vpa_path = out_dir / "vpa.csv"
    feature_frame[VPA_OUTPUT_COLUMNS].to_csv(vpa_path, index=False)
    atomic_write_json(out_dir / "data_quality.json", json_safe(quality))
    atomic_write_json(out_dir / "structure_hint.json", json_safe(hint))
    atomic_write_json(out_dir / "candidates.json", json_safe(candidates))

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "manifest_type": "wyckoff_vpa_evidence",
        "run_id": candidates["run_id"],
        "generated_at_utc": utc_now_iso(),
        "symbol": candidates.get("symbol"),
        "interval": candidates.get("interval"),
        "parameters": parameters,
        "source_files": {
            "ohlcv.csv": {"sha256": ohlcv_sha, "bytes": csv_path.stat().st_size},
            "meta.json": {
                "sha256": meta_sha,
                "bytes": meta_path.stat().st_size if meta_path.exists() else None,
                "present": meta_path.exists(),
            },
        },
        "artifacts": {
            "vpa.csv": {"sha256": sha256_file(vpa_path), "bytes": vpa_path.stat().st_size},
            "data_quality.json": {"sha256": sha256_file(out_dir / "data_quality.json")},
            "structure_hint.json": {"sha256": sha256_file(out_dir / "structure_hint.json")},
            "candidates.json": {
                "sha256": sha256_file(out_dir / "candidates.json"),
                "canonical_sha256": sha256_json(json_safe(candidates)),
            },
        },
    }
    atomic_write_json(out_dir / "run_manifest.json", manifest)

    events = candidates["events"]
    confirmable_core = [event for event in events if event["type"] in CORE_EVENT_TYPES and event["confirmable"]]
    retests = [event for event in events if event["type"] in {"TEST_AFTER_SPRING", "TEST_AFTER_UT", "LPS", "LPSY"}]
    print(
        f"OK vpa n={len(feature_frame)} quality={quality['status']} range={bool(hint.get('has_range'))} "
        f"events={len(events)} core={len(confirmable_core)} retests={len(retests)} "
        f"run_id={candidates['run_id']} tradeable=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
