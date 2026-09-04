#!/usr/bin/env python3
"""Compute volume-price features and *candidate* Wyckoff events.

This script is the numeric half of the skill. It does NOT label Phase A–E
and does NOT issue orders. It writes:
  vpa.csv            bar-level features
  candidates.json    rule-based event / effort flags
  structure_hint.json  coarse trading-range guess for the LLM to confirm or reject
"""
from __future__ import annotations

import argparse
import json
import math
import os
from typing import Any

import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev).abs(),
            (df["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def rolling_linreg_residual(y: np.ndarray, x: np.ndarray, lookback: int) -> np.ndarray:
    """Residual of y ~ a + b x over a trailing window. NaN until 2*lookback."""
    n = len(y)
    out = np.full(n, np.nan)
    for i in range(lookback * 2, n):
        sl = slice(i - lookback + 1, i + 1)
        xx, yy = x[sl], y[sl]
        mask = np.isfinite(xx) & np.isfinite(yy)
        if mask.sum() < max(20, lookback // 3):
            continue
        xx, yy = xx[mask], yy[mask]
        varx = np.var(xx)
        if varx < 1e-12:
            out[i] = 0.0
            continue
        b = np.cov(xx, yy, ddof=0)[0, 1] / varx
        a = yy.mean() - b * xx.mean()
        r = np.corrcoef(xx, yy)[0, 1]
        if not np.isfinite(r) or r < 0.15 or b <= 0:
            out[i] = 0.0
            continue
        out[i] = yy[-1] - (a + b * xx[-1])
    return out


def weis_waves(close: np.ndarray, volume: np.ndarray, high: np.ndarray, low: np.ndarray):
    n = len(close)
    wave_id = np.zeros(n, dtype=int)
    wave_vol = np.zeros(n)
    wave_effort = np.zeros(n)
    wave_result = np.zeros(n)
    if n < 3:
        return wave_id, wave_vol, wave_effort, wave_result
    direction = 1 if close[1] >= close[0] else -1
    start = 0
    wid = 1
    for i in range(1, n):
        moved = close[i] - close[i - 1]
        new_dir = 1 if moved > 0 else (-1 if moved < 0 else direction)
        if new_dir != direction and moved != 0:
            sl = slice(start, i)
            vol = float(volume[sl].sum())
            effort = float(high[sl].max() - low[sl].min())
            result = float(close[i - 1] - close[start])
            wave_id[sl] = wid
            wave_vol[sl] = vol
            wave_effort[sl] = effort
            wave_result[sl] = result
            wid += 1
            start = i
            direction = new_dir
        wave_id[i] = wid
    sl = slice(start, n)
    vol = float(volume[sl].sum())
    effort = float(high[sl].max() - low[sl].min()) if n > start else 0.0
    result = float(close[-1] - close[start])
    wave_vol[sl] = vol
    wave_effort[sl] = effort
    wave_result[sl] = result
    return wave_id, wave_vol, wave_effort, wave_result


def detect_candidates(df: pd.DataFrame) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    n = len(df)
    if n < 50:
        return events

    vol_rel = df["vol_rel"].to_numpy()
    rng_rel = df["range_rel"].to_numpy()
    vsa = df["vsa_residual"].to_numpy()
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    dates = df["date"].astype(str).to_numpy()

    look = 20
    for i in range(look, n):
        vr, rr = vol_rel[i], rng_rel[i]
        if not np.isfinite(vr) or not np.isfinite(rr):
            continue
        body = abs(close[i] - df["open"].iloc[i])
        bar_range = high[i] - low[i]
        close_loc = 0.5 if bar_range <= 0 else (close[i] - low[i]) / bar_range

        # Climax: extreme volume + wide range
        if vr >= 2.0 and rr >= 1.7:
            kind = "SC" if close_loc <= 0.35 else ("BC" if close_loc >= 0.65 else "CLIMAX")
            events.append(_ev(dates[i], close[i], kind, i, vr, rr, vsa[i], "wide-range high-volume bar"))

        # Effort no result: high volume, little net progress
        if vr >= 1.8 and rr <= 0.85:
            side = "UP" if close[i] >= df["open"].iloc[i] else "DOWN"
            events.append(
                _ev(dates[i], close[i], "EFFORT_NO_RESULT", i, vr, rr, vsa[i], f"high volume, compressed range ({side})")
            )

        # Spring / upthrust: meaningful pierce, close back, volume not expanding, cooldown
        atr_i = df["atr"].iloc[i]
        prior_low = float(low[i - look : i].min())
        prior_high = float(high[i - look : i].max())
        if np.isfinite(atr_i) and atr_i > 0:
            last_spring = next((e for e in reversed(events) if e["type"] == "SPRING"), None)
            last_ut = next((e for e in reversed(events) if e["type"] == "UT"), None)
            cool_s = last_spring is None or (i - last_spring["bar_index"]) >= 8
            cool_u = last_ut is None or (i - last_ut["bar_index"]) >= 8
            if cool_s and low[i] < prior_low - 0.15 * atr_i and close[i] > prior_low and vr < 1.4:
                events.append(
                    _ev(dates[i], close[i], "SPRING", i, vr, rr, vsa[i], "pierce of prior swing low, close back inside")
                )
            if cool_u and high[i] > prior_high + 0.15 * atr_i and close[i] < prior_high and vr < 1.4:
                events.append(
                    _ev(dates[i], close[i], "UT", i, vr, rr, vsa[i], "pierce of prior swing high, close back inside")
                )

    # Keep last 40 events, prefer more recent
    events = events[-40:]
    return events


def _ev(date, price, typ, idx, vr, rr, vsa, why):
    return {
        "date": date,
        "price": round(float(price), 6),
        "type": typ,
        "bar_index": int(idx),
        "vol_rel": None if not np.isfinite(vr) else round(float(vr), 3),
        "range_rel": None if not np.isfinite(rr) else round(float(rr), 3),
        "vsa_residual": None if not np.isfinite(vsa) else round(float(vsa), 3),
        "rule": why,
        "status": "candidate",
    }


def structure_hint(df: pd.DataFrame) -> dict[str, Any]:
    """Coarse trading-range guess using last 80 bars. LLM must confirm."""
    tail = df.tail(80)
    if len(tail) < 40:
        return {"has_range": False, "reason": "not enough bars"}
    atr_last = float(df["atr"].iloc[-1])
    hi = float(tail["high"].max())
    lo = float(tail["low"].min())
    mid = float(tail["close"].mean())
    width = hi - lo
    if not np.isfinite(atr_last) or atr_last <= 0:
        return {"has_range": False, "reason": "atr unavailable"}
    width_atr = width / atr_last
    # Range if the 80-bar span is not a one-way trend: close is not glued to one edge
    last = float(df["close"].iloc[-1])
    loc = (last - lo) / width if width else 0.5
    net = float(tail["close"].iloc[-1] - tail["close"].iloc[0])
    trendish = abs(net) > 0.62 * width
    has_range = width_atr >= 3.0 and not trendish
    return {
        "has_range": bool(has_range),
        "lookback_bars": int(len(tail)),
        "range_high": round(hi, 6),
        "range_low": round(lo, 6),
        "range_mid": round(mid, 6),
        "width_atr": round(width_atr, 2),
        "close_location_in_range": round(loc, 3),
        "net_move_vs_width": round(abs(net) / width if width else 0, 3),
        "trendish": bool(trendish),
        "last": round(last, 6),
        "atr": round(atr_last, 6),
        "note": "hint only — reject if you cannot draw a TR a second person would agree on",
    }


def score_setup(df: pd.DataFrame, events: list[dict], hint: dict) -> dict[str, Any]:
    """Heuristic 0–10 confluence. Maps to a probability *band*, not a backtest."""
    last = df.iloc[-1]
    recent = [e for e in events if e["bar_index"] >= len(df) - 40]

    tr_q = 0
    if hint.get("has_range"):
        tr_q = 2 if hint["width_atr"] >= 5 else 1

    types = {e["type"] for e in recent}
    seq_q = 0
    if "SC" in types or "BC" in types:
        seq_q += 1
    if "SPRING" in types or "UT" in types:
        seq_q += 1
    seq_q = min(seq_q, 2)

    vol_q = 0
    if any(e["type"] == "EFFORT_NO_RESULT" for e in recent[-8:]):
        vol_q += 1
    vsa = last.get("vsa_residual")
    if vsa is not None and not (isinstance(vsa, float) and math.isnan(vsa)):
        if abs(float(vsa)) >= 0.4:
            vol_q += 1
    vol_q = min(vol_q, 2)

    # HTF proxy: 50 vs 200 SMA
    htf_q = 0
    sma50, sma200 = last.get("sma50"), last.get("sma200")
    close = float(last["close"])
    if pd.notna(sma50) and pd.notna(sma200):
        aligned_up = close > sma50 > sma200
        aligned_dn = close < sma50 < sma200
        htf_q = 2 if aligned_up or aligned_dn else 1

    rr_q = 0
    if hint.get("has_range"):
        # hypothetical: buy spring test at range_low, stop 0.6 ATR below, target range_high
        stop_dist = 0.6 * hint["atr"]
        t1 = hint["range_high"] - hint["last"]
        t1_long = t1 if t1 > 0 else 0
        t1_short = hint["last"] - hint["range_low"]
        best = max(t1_long, t1_short)
        if stop_dist > 0 and best / stop_dist >= 2:
            rr_q = 2
        elif stop_dist > 0 and best / stop_dist >= 1.4:
            rr_q = 1

    score = tr_q + seq_q + vol_q + htf_q + rr_q  # 0-10
    # Conservative mapping. Do not claim >72%.
    p_lo = int(min(70, 34 + score * 3))
    p_hi = int(min(72, 40 + score * 3.2))
    tradeable = score >= 6
    return {
        "confluence_0_10": score,
        "parts": {
            "trading_range": tr_q,
            "event_sequence": seq_q,
            "volume_confirmation": vol_q,
            "higher_timeframe": htf_q,
            "reward_to_risk": rr_q,
        },
        "p_reach_t1_before_stop": {"low": p_lo, "high": p_hi, "unit": "percent"},
        "tradeable": tradeable,
        "caveat": "Heuristic band from checklist completeness, not a historical win-rate. Recalibrate if you collect your own journal.",
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True, help="ohlcv.csv from fetch_ohlcv.py")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--vsa-lookback", type=int, default=60)
    args = p.parse_args()

    df = pd.read_csv(args.csv)
    need = {"date", "open", "high", "low", "close", "volume"}
    missing = need - set(df.columns)
    if missing:
        raise SystemExit(f"csv missing columns: {missing}")
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    df["atr"] = atr(df, 14)
    df["range"] = df["high"] - df["low"]
    df["vol_rel"] = df["volume"] / df["volume"].rolling(20, min_periods=10).median()
    df["range_rel"] = df["range"] / df["atr"]
    df["sma50"] = df["close"].rolling(50).mean()
    df["sma200"] = df["close"].rolling(200).mean()

    nr = (df["range"] / df["atr"]).to_numpy()
    nv = df["vol_rel"].to_numpy()
    df["vsa_residual"] = rolling_linreg_residual(nr, nv, args.vsa_lookback)

    wid, wvol, weff, wres = weis_waves(
        df["close"].to_numpy(),
        df["volume"].to_numpy(),
        df["high"].to_numpy(),
        df["low"].to_numpy(),
    )
    df["wave_id"] = wid
    df["wave_volume"] = wvol
    df["wave_effort"] = weff
    df["wave_result"] = wres
    # Effort vs result on the completed wave: result / effort
    with np.errstate(divide="ignore", invalid="ignore"):
        wr = np.where(weff > 0, np.abs(wres) / weff, np.nan)
    df["wave_efficiency"] = wr

    os.makedirs(args.out_dir, exist_ok=True)
    vpa_path = os.path.join(args.out_dir, "vpa.csv")
    cols = [
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
        "sma50",
        "sma200",
        "wave_id",
        "wave_volume",
        "wave_effort",
        "wave_result",
        "wave_efficiency",
    ]
    df[cols].to_csv(vpa_path, index=False)

    events = detect_candidates(df)
    hint = structure_hint(df)
    setup = score_setup(df, events, hint)

    # Compact tail for the LLM (last 12 bars), not the whole series
    tail = df[cols].tail(12).replace({np.nan: None}).to_dict(orient="records")
    for row in tail:
        for k, v in list(row.items()):
            if isinstance(v, float):
                row[k] = round(v, 6)

    candidates = {
        "bars": int(len(df)),
        "last_bar": df["date"].iloc[-1],
        "last_close": round(float(df["close"].iloc[-1]), 6),
        "events": events,
        "recent_bars": tail,
        "structure_hint": hint,
        "setup_score": setup,
    }
    cand_path = os.path.join(args.out_dir, "candidates.json")
    with open(cand_path, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)

    print(
        f"OK vpa n={len(df)} events={len(events)} "
        f"score={setup['confluence_0_10']} "
        f"p={setup['p_reach_t1_before_stop']['low']}-{setup['p_reach_t1_before_stop']['high']}% "
        f"tradeable={setup['tradeable']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
