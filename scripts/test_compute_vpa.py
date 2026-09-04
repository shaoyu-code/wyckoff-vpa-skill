#!/usr/bin/env python3
"""Synthetic checks for compute_vpa. Run: python scripts/test_compute_vpa.py"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compute_vpa as cv  # noqa: E402


def make_range_then_spring() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    n = 120
    # flat range 100-110
    close = 105 + rng.normal(0, 0.6, n).cumsum() * 0
    close = 105 + rng.normal(0, 0.8, n)
    close = np.clip(close, 101, 109)
    close[80] = 97  # pierce
    close[81] = 102  # recover
    high = close + 0.7
    low = close - 0.7
    low[80] = 96.2
    high[80] = 101
    open_ = close + rng.normal(0, 0.2, n)
    vol = np.full(n, 1000.0)
    vol[30] = 3500  # climax-ish
    high[30] = close[30] + 4
    low[30] = close[30] - 0.2
    open_[30] = close[30] + 3
    close[30] = close[30] - 1
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def test_spring_and_sc(tmp: Path) -> None:
    df = make_range_then_spring()
    df["atr"] = cv.atr(df["high"], df["low"], df["close"], 14)
    vol_med = df["volume"].rolling(20, min_periods=10).median()
    df["rel_volume"] = df["volume"] / vol_med.replace(0, np.nan)
    df["norm_range"] = (df["high"] - df["low"]) / df["atr"].replace(0, np.nan)
    df["norm_volume"] = df["rel_volume"]
    df["vsa_residual"] = cv.rolling_residual(
        df["norm_volume"].to_numpy(), df["norm_range"].to_numpy(), 40
    )
    rng = cv.detect_range(df)
    assert rng is not None, "expected a range candidate"
    cands = cv.detect_candidates(df, rng)
    types = {c["type"] for c in cands}
    assert "SPRING" in types, types
    # climax bar at 30 may or may not fire depending on ATR; not required
    csv = tmp / "ohlcv.csv"
    df[["open", "high", "low", "close", "volume"]].to_csv(csv)
    out = tmp / "out"
    import subprocess

    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "compute_vpa.py"), "--ohlcv", str(csv), "--out-dir", str(out)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "candidates" in r.stdout
    assert (out / "candidates.json").exists()


def test_too_short(tmp: Path) -> None:
    df = pd.DataFrame(
        {
            "open": [1, 2],
            "high": [2, 3],
            "low": [1, 2],
            "close": [1.5, 2.5],
            "volume": [10, 10],
        },
        index=pd.date_range("2025-01-01", periods=2, freq="D"),
    )
    csv = tmp / "short.csv"
    df.to_csv(csv)
    import subprocess

    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "compute_vpa.py"), "--ohlcv", str(csv), "--out-dir", str(tmp / "x")],
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        test_spring_and_sc(p)
        test_too_short(p)
    print("OK")
