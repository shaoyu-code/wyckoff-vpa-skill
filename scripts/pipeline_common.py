#!/usr/bin/env python3
"""Shared deterministic utilities for the Wyckoff VPA pipeline."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.2.0"
CANONICAL_COMPUTE_PARAMETERS = {
    "vsa_lookback": 60,
    "wave_reversal_atr": 0.8,
    "wave_reversal_pct": 0.015,
    "event_review_horizon_bars": 80,
}
STANDARD_DISCLAIMER = "研究与情景预案，不是代客下单，不保证盈利。"
DEFAULT_RISK_PCT = 0.75
MAX_RISK_WITH_EXPLICIT_OVERRIDE_PCT = 1.0
MAX_CORRELATED_RISK_PCT = 1.5
MAX_CORRELATED_POSITIONS = 2
RISK_OVERRIDE_ACK = "I_ACCEPT_RISK_ABOVE_0.75_PERCENT"
DEFAULT_SKIP_IF = [
    "daily or weekly evidence lineage is invalid",
    "daily or weekly data quality is not eligible",
    "no reproducible trading range",
    "no confirmed post-event retest",
    "a newer contradictory sequence exists",
    "final confluence < 6",
    "actual RR(T1) < 1.6",
    "higher timeframe is opposed or stale",
    "position unit or contract multiplier is unknown",
]

FUTURES_MONTH_CODES = "FGHJKMNQUVXZ"
GC_CONTRACT_RE = re.compile(rf"^GC[{FUTURES_MONTH_CODES}]\d{{1,2}}$")
MGC_CONTRACT_RE = re.compile(rf"^MGC[{FUTURES_MONTH_CODES}]\d{{1,2}}$")


def load_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    value = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{p} must contain a JSON object")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def analysis_instrument_profile(symbol: str) -> dict[str, Any]:
    code = text(symbol).upper()
    if code == "GC=F":
        return {
            "instrument_type": "continuous_futures_reference",
            "position_unit": None,
            "contract_multiplier": None,
            "quote_currency": "USD",
            "sizing_requires_actual_contract": True,
            "note": "GC=F is a continuous analysis reference; select an actual GC or MGC contract before sizing.",
        }
    if code == "MGC=F":
        return {
            "instrument_type": "continuous_micro_futures_reference",
            "position_unit": None,
            "contract_multiplier": None,
            "quote_currency": "USD",
            "sizing_requires_actual_contract": True,
            "note": "MGC=F is a continuous analysis reference; select an actual MGC contract before sizing.",
        }
    if code.endswith("-USD"):
        return {
            "instrument_type": "spot_reference",
            "position_unit": code.split("-")[0],
            "contract_multiplier": 1.0,
            "quote_currency": "USD",
            "sizing_requires_actual_contract": False,
            "note": "Sizing is for unlevered spot units only; derivatives require another profile.",
        }
    if code.startswith("^"):
        return {
            "instrument_type": "index_reference",
            "position_unit": None,
            "contract_multiplier": None,
            "quote_currency": "USD",
            "sizing_requires_actual_contract": True,
            "note": "Index symbols are analytical references and are not directly executable.",
        }
    return {
        "instrument_type": "equity_or_etf",
        "position_unit": "shares",
        "contract_multiplier": 1.0,
        "quote_currency": "USD",
        "sizing_requires_actual_contract": False,
        "note": "Assumes unlevered shares. Options and leveraged derivatives need a separate profile.",
    }


def executable_instrument_profile(reference_symbol: str, actual_symbol: str | None) -> tuple[dict[str, Any] | None, str | None]:
    reference = text(reference_symbol).upper()
    actual = text(actual_symbol).upper()
    base = analysis_instrument_profile(reference)

    if reference in {"GC=F", "MGC=F"}:
        if not actual:
            return None, "continuous gold futures reference requires an actual GC or MGC delivery contract"
        if GC_CONTRACT_RE.fullmatch(actual):
            return {
                **base,
                "instrument_type": "gold_futures_contract",
                "actual_tradable_symbol": actual,
                "position_unit": "contracts",
                "contract_multiplier": 100.0,
                "sizing_requires_actual_contract": False,
            }, None
        if MGC_CONTRACT_RE.fullmatch(actual):
            return {
                **base,
                "instrument_type": "micro_gold_futures_contract",
                "actual_tradable_symbol": actual,
                "position_unit": "contracts",
                "contract_multiplier": 10.0,
                "sizing_requires_actual_contract": False,
            }, None
        return None, "actual gold contract must match GC<month><year> or MGC<month><year>"

    if reference.startswith("^"):
        return None, (
            "index reference is analysis-only; rerun the full pipeline on the actual tradable "
            "instrument instead of sizing an ETF or futures product from index price geometry"
        )

    if reference.endswith("-USD"):
        if actual and actual != reference:
            return None, "spot-reference sizing only supports the same spot symbol"
        return {**base, "actual_tradable_symbol": reference}, None

    if actual and actual != reference:
        return None, "equity/ETF actual_tradable_symbol must equal the analyzed symbol"
    return {**base, "actual_tradable_symbol": reference}, None


def derive_run_id(symbol: str, interval: str, ohlcv_sha256: str, parameters: dict[str, Any]) -> str:
    seed = {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "interval": interval,
        "ohlcv_sha256": ohlcv_sha256,
        "parameters": parameters,
    }
    return sha256_json(seed)[:24]
