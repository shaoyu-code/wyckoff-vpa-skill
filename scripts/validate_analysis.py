#!/usr/bin/env python3
"""Deterministically validate an Agent-produced Wyckoff VPA analysis.

Trust model
-----------
* Raw OHLCV + meta.json are the numerical source of truth.
* candidates.json is recomputed and byte/hash checked before use.
* The Agent may choose a candidate pair and write narrative, but may not
  self-assert event identity, effort/result support, trigger truth, instrument
  multipliers, position size, probability, or validation status.
* Any failure produces a fail-closed ``analysis.validated.json`` whose decision
  is ``no_trade`` and whose executable price fields are removed.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import re
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from jsonschema import Draft202012Validator
except ImportError as exc:  # production dependency, not optional
    raise SystemExit("jsonschema is required; install requirements.txt") from exc

import build_plan as bp
import compute_vpa as cv
from pipeline_common import (
    CANONICAL_COMPUTE_PARAMETERS,
    DEFAULT_RISK_PCT,
    DEFAULT_SKIP_IF,
    MAX_CORRELATED_POSITIONS,
    MAX_CORRELATED_RISK_PCT,
    MAX_RISK_WITH_EXPLICIT_OVERRIDE_PCT,
    RISK_OVERRIDE_ACK,
    SCHEMA_VERSION,
    STANDARD_DISCLAIMER,
    analysis_instrument_profile,
    atomic_write_json,
    executable_instrument_profile,
    finite_number,
    load_json,
    parse_timestamp,
    sha256_bytes,
    sha256_file,
    sha256_json,
    text,
)

ALLOWED_SETUPS = {
    "spring_retest": {"direction": "long", "core": "SPRING", "retests": {"TEST_AFTER_SPRING"}},
    "ut_retest": {"direction": "short", "core": "UT", "retests": {"TEST_AFTER_UT"}},
    "sos_lps": {"direction": "long", "core": "SOS", "retests": {"LPS"}},
    "sow_lpsy": {"direction": "short", "core": "SOW", "retests": {"LPSY"}},
}
REVIEW_EVENT_TYPES = {
    "SPRING", "UT", "SOS", "SOW", "TEST_AFTER_SPRING", "TEST_AFTER_UT", "LPS", "LPSY"
}
TRADE_ACTIONS = {"enter_long", "enter_short"}
SAFE_ACTIONS = {"wait", "stand_aside"}
UNSAFE_PATTERNS = [
    r"(?:现在|立即|立刻).{0,8}(?:市价)?(?:买入|卖出|做多|做空|满仓|梭哈)",
    r"(?:满仓|梭哈|all[ -]?in)",
    r"(?:保证盈利|稳赚|必涨|必赢|guaranteed\s+profit)",
    r"(?:代客操盘|代客下单)",
    r"market\s+(?:buy|sell)\s+now",
]


def schema_issues(analysis: dict[str, Any], root: Path) -> list[str]:
    schema = load_json(root / "references" / "analysis.schema.json")
    validator = Draft202012Validator(schema)
    issues: list[str] = []
    for error in sorted(validator.iter_errors(analysis), key=lambda item: list(item.absolute_path)):
        path = ".".join(str(item) for item in error.absolute_path) or "$"
        issues.append(f"schema {path}: {error.message}")
    return issues


def exact_number(left: Any, right: Any, tolerance: float = 1e-8) -> bool:
    a = finite_number(left)
    b = finite_number(right)
    return a is not None and b is not None and abs(a - b) <= tolerance


def event_identity_matches(submitted: dict[str, Any], canonical: dict[str, Any], *, rejected: bool) -> list[str]:
    errors: list[str] = []
    string_fields = ["id", "evidence_hash", "date", "type"]
    if not rejected:
        string_fields.append("related_event_id")
    for field in string_fields:
        if (submitted.get(field) or None) != (canonical.get(field) or None):
            errors.append(f"event {submitted.get('id')!r} field {field} does not match canonical evidence")
    if not rejected:
        if int(finite_number(submitted.get("bar_index")) or -1) != int(canonical.get("bar_index") or -2):
            errors.append(f"event {submitted.get('id')!r} bar_index does not match canonical evidence")
        for field in ["price", "extreme_price", "boundary"]:
            left, right = submitted.get(field), canonical.get(field)
            if left is None and right is None:
                continue
            if not exact_number(left, right):
                errors.append(f"event {submitted.get('id')!r} field {field} does not match canonical evidence")
        if not text(submitted.get("why")):
            errors.append(f"confirmed event {submitted.get('id')!r} requires why")
    elif not text(submitted.get("reason")):
        errors.append(f"rejected event {submitted.get('id')!r} requires reason")
    return errors


def verify_run(run_dir: Path) -> dict[str, Any]:
    """Verify hashes and recompute canonical candidates from raw run files."""
    errors: list[str] = []
    required = {
        "ohlcv": run_dir / "ohlcv.csv",
        "meta": run_dir / "meta.json",
        "vpa": run_dir / "vpa.csv",
        "quality": run_dir / "data_quality.json",
        "hint": run_dir / "structure_hint.json",
        "candidates": run_dir / "candidates.json",
        "manifest": run_dir / "run_manifest.json",
    }
    for name, path in required.items():
        if not path.exists():
            errors.append(f"{run_dir}: missing {name} file {path.name}")
    if errors:
        return {"ok": False, "errors": errors, "run_dir": str(run_dir)}

    try:
        manifest = load_json(required["manifest"])
        submitted_candidates = load_json(required["candidates"])
        meta = load_json(required["meta"])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"{run_dir}: unreadable run artifact: {exc}"], "run_dir": str(run_dir)}

    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("manifest_type") != "wyckoff_vpa_evidence":
        errors.append(f"{run_dir}: invalid evidence manifest version/type")
    if meta.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{run_dir}: meta.json schema version does not match the production contract")
    if submitted_candidates.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{run_dir}: candidates.json schema version does not match the production contract")
    source_files = manifest.get("source_files") if isinstance(manifest.get("source_files"), dict) else {}
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    expected_hashes = {
        required["ohlcv"]: ((source_files.get("ohlcv.csv") or {}).get("sha256")),
        required["meta"]: ((source_files.get("meta.json") or {}).get("sha256")),
        required["vpa"]: ((artifacts.get("vpa.csv") or {}).get("sha256")),
        required["quality"]: ((artifacts.get("data_quality.json") or {}).get("sha256")),
        required["hint"]: ((artifacts.get("structure_hint.json") or {}).get("sha256")),
        required["candidates"]: ((artifacts.get("candidates.json") or {}).get("sha256")),
    }
    for path, expected in expected_hashes.items():
        actual = sha256_file(path)
        if not isinstance(expected, str) or actual != expected:
            errors.append(f"{run_dir}: hash mismatch for {path.name}")

    parameters = manifest.get("parameters") if isinstance(manifest.get("parameters"), dict) else {}
    if parameters != CANONICAL_COMPUTE_PARAMETERS:
        errors.append(f"{run_dir}: non-canonical compute parameters are not accepted by the production validator")
    try:
        frame, base_quality = cv.load_ohlcv(str(required["ohlcv"]))
        recomputed_frame, quality, hint, recomputed_candidates, _ = cv.build_artifacts(
            frame,
            base_quality=base_quality,
            fetch_meta=meta,
            vsa_lookback=int(CANONICAL_COMPUTE_PARAMETERS["vsa_lookback"]),
            wave_reversal_atr=float(CANONICAL_COMPUTE_PARAMETERS["wave_reversal_atr"]),
            wave_reversal_pct=float(CANONICAL_COMPUTE_PARAMETERS["wave_reversal_pct"]),
            ohlcv_sha256=sha256_file(required["ohlcv"]),
            meta_sha256=sha256_file(required["meta"]),
        )
    except (ValueError, TypeError, KeyError) as exc:
        errors.append(f"{run_dir}: canonical recomputation failed: {exc}")
        return {"ok": False, "errors": errors, "run_dir": str(run_dir)}

    canonical_expected = ((artifacts.get("candidates.json") or {}).get("canonical_sha256"))
    recomputed_hash = sha256_json(cv.json_safe(recomputed_candidates))
    submitted_hash = sha256_json(submitted_candidates)
    if canonical_expected != recomputed_hash:
        errors.append(f"{run_dir}: manifest canonical candidates hash does not match raw-data recomputation")
    if submitted_hash != recomputed_hash:
        errors.append(f"{run_dir}: candidates.json differs from raw-data recomputation")
    if manifest.get("run_id") != recomputed_candidates.get("run_id"):
        errors.append(f"{run_dir}: run_id differs from raw-data recomputation")
    if manifest.get("symbol") != recomputed_candidates.get("symbol"):
        errors.append(f"{run_dir}: manifest symbol differs from canonical candidates")
    if manifest.get("interval") != recomputed_candidates.get("interval"):
        errors.append(f"{run_dir}: manifest interval differs from canonical candidates")

    try:
        submitted_quality = load_json(required["quality"])
        submitted_hint = load_json(required["hint"])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"{run_dir}: unreadable derived JSON artifact: {exc}")
    else:
        if sha256_json(submitted_quality) != sha256_json(cv.json_safe(quality)):
            errors.append(f"{run_dir}: data_quality.json differs from raw-data recomputation")
        if sha256_json(submitted_hint) != sha256_json(cv.json_safe(hint)):
            errors.append(f"{run_dir}: structure_hint.json differs from raw-data recomputation")

    vpa_buffer = io.StringIO()
    recomputed_frame[cv.VPA_OUTPUT_COLUMNS].to_csv(vpa_buffer, index=False)
    if sha256_bytes(vpa_buffer.getvalue().encode("utf-8")) != sha256_file(required["vpa"]):
        errors.append(f"{run_dir}: vpa.csv differs from raw-data recomputation")

    return {
        "ok": not errors,
        "errors": errors,
        "run_dir": str(run_dir),
        "manifest": manifest,
        "meta": meta,
        "candidates": recomputed_candidates,
        "frame": recomputed_frame,
        "quality": quality,
        "hint": hint,
        "candidates_file_sha256": sha256_file(required["candidates"]),
    }


def resolve_run_dirs(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.run_dir:
        daily = Path(args.run_dir)
    elif args.candidates:
        daily = Path(args.candidates).resolve().parent
    else:
        raise ValueError("provide --run-dir or --candidates")
    if args.weekly_dir:
        weekly = Path(args.weekly_dir)
    elif args.weekly_candidates:
        weekly = Path(args.weekly_candidates).resolve().parent
    else:
        weekly = daily / "weekly"
    return daily.resolve(), weekly.resolve()


def bar_by_date(frame: Any, date: str) -> dict[str, Any] | None:
    rows = frame.loc[frame["date"].astype(str) == str(date)]
    if rows.empty:
        return None
    row = rows.iloc[-1]
    output: dict[str, Any] = {}
    for key, value in row.items():
        if key == "date":
            output[key] = str(value)
            continue
        number = finite_number(value)
        output[key] = number
    return output


def structured_trigger(
    plan: dict[str, Any],
    *,
    direction: str,
    retest: dict[str, Any],
    frame: Any,
) -> tuple[bool, list[str], dict[str, Any]]:
    errors: list[str] = []
    trigger = plan.get("trigger") if isinstance(plan.get("trigger"), dict) else {}
    if trigger.get("mode") != "retest_close_volume":
        errors.append("trigger.mode must be retest_close_volume")
    bar_date = text(trigger.get("bar_date"))
    if bar_date != str(retest.get("date")):
        errors.append("trigger.bar_date must equal the selected canonical retest date")
    row = bar_by_date(frame, bar_date) if bar_date else None
    if row is None:
        errors.append("trigger bar is absent from canonical VPA data")
        return False, errors, {"confirmed": False}

    price_rule = trigger.get("price_rule") if isinstance(trigger.get("price_rule"), dict) else {}
    volume_rule = trigger.get("volume_rule") if isinstance(trigger.get("volume_rule"), dict) else {}
    expected_price_operator = ">=" if direction == "long" else "<="
    if price_rule.get("field") != "close" or price_rule.get("operator") != expected_price_operator:
        errors.append(f"trigger price rule must be close {expected_price_operator} boundary")
    price_value = finite_number(price_rule.get("value"))
    boundary = finite_number(retest.get("boundary"))
    atr_value = finite_number(retest.get("atr"))
    if price_value is None or boundary is None or atr_value is None or atr_value <= 0:
        errors.append("trigger price rule, boundary, or ATR is unavailable")
    elif abs(price_value - boundary) > max(0.10 * atr_value, 0.0025 * abs(boundary)):
        errors.append("trigger price level is not tied to the canonical range boundary")

    if volume_rule.get("field") != "vol_rel" or volume_rule.get("operator") != "<=":
        errors.append("trigger volume rule must be vol_rel <= threshold")
    volume_value = finite_number(volume_rule.get("value"))
    if volume_value is None or not 0.5 <= volume_value <= 1.0:
        errors.append("trigger volume threshold must be between 0.5 and 1.0")

    close = finite_number(row.get("close"))
    vol_rel = finite_number(row.get("vol_rel"))
    price_true = bool(
        close is not None
        and price_value is not None
        and ((direction == "long" and close >= price_value) or (direction == "short" and close <= price_value))
    )
    volume_true = bool(vol_rel is not None and volume_value is not None and vol_rel <= volume_value)
    if not price_true:
        errors.append("canonical trigger bar does not satisfy the price rule")
    if not volume_true:
        errors.append("canonical trigger bar does not satisfy the volume rule")
    if trigger.get("confirmed") is not True:
        errors.append("trade action requires trigger.confirmed=true before deterministic re-evaluation")

    evidence = {
        "mode": "retest_close_volume",
        "bar_date": bar_date or None,
        "retest_id": retest.get("id"),
        "canonical_close": close,
        "canonical_vol_rel": vol_rel,
        "price_rule": {"field": "close", "operator": expected_price_operator, "value": boundary},
        "volume_rule": {"field": "vol_rel", "operator": "<=", "value": min(1.0, volume_value or 1.0)},
        "price_true": price_true,
        "volume_true": volume_true,
    }
    evidence["evidence_hash"] = sha256_json(evidence)
    supplied_hash = text(trigger.get("evidence_hash"))
    if supplied_hash and supplied_hash != evidence["evidence_hash"]:
        errors.append("trigger.evidence_hash does not match deterministic trigger evidence")
    evidence["confirmed"] = not errors
    evidence["text"] = text(trigger.get("text"))
    return not errors, errors, evidence


def event_pair_index(candidates: dict[str, Any]) -> list[dict[str, Any]]:
    events = [item for item in candidates.get("events") or [] if isinstance(item, dict)]
    by_id = {str(item.get("id")): item for item in events if item.get("id")}
    pairs: list[dict[str, Any]] = []
    for retest in events:
        related = str(retest.get("related_event_id") or "")
        core = by_id.get(related)
        if not core:
            continue
        setup = next(
            (
                name
                for name, contract in ALLOWED_SETUPS.items()
                if core.get("type") == contract["core"] and retest.get("type") in contract["retests"]
            ),
            None,
        )
        if not setup:
            continue
        if core.get("status") != "candidate" or retest.get("status") != "candidate":
            continue
        if core.get("confirmable") is not True or retest.get("confirmable") is not True:
            continue
        pairs.append({"setup": setup, "core": core, "retest": retest})
    return sorted(pairs, key=lambda item: int(item["retest"].get("bar_index") or -1))


def validate_review_and_pair(
    analysis: dict[str, Any], candidates: dict[str, Any], *, claims_trade: bool
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    events = [item for item in candidates.get("events") or [] if isinstance(item, dict)]
    by_id = {str(item.get("id")): item for item in events if item.get("id")}
    bars = int(candidates.get("bars") or 0)
    horizon_start = max(0, bars - int(CANONICAL_COMPUTE_PARAMETERS["event_review_horizon_bars"]))
    reviewable = {
        str(item["id"]): item
        for item in events
        if item.get("id")
        and item.get("type") in REVIEW_EVENT_TYPES
        and item.get("confirmable") is True
        and int(item.get("bar_index") or -1) >= horizon_start
    }

    structure = analysis.get("structure") if isinstance(analysis.get("structure"), dict) else {}
    confirmed = structure.get("events_confirmed") if isinstance(structure.get("events_confirmed"), list) else []
    rejected = structure.get("events_rejected") if isinstance(structure.get("events_rejected"), list) else []
    submitted_ids: list[str] = []
    confirmed_ids: set[str] = set()
    normalized_confirmed: list[dict[str, Any]] = []
    normalized_rejected: list[dict[str, Any]] = []

    for submitted in confirmed:
        if not isinstance(submitted, dict):
            errors.append("events_confirmed contains a non-object")
            continue
        event_id = text(submitted.get("id"))
        submitted_ids.append(event_id)
        canonical = reviewable.get(event_id)
        if not canonical:
            errors.append(f"confirmed event {event_id!r} is not a reviewable canonical candidate")
            continue
        errors.extend(event_identity_matches(submitted, canonical, rejected=False))
        confirmed_ids.add(event_id)
        normalized_confirmed.append(
            {
                "id": canonical["id"],
                "evidence_hash": canonical["evidence_hash"],
                "date": canonical["date"],
                "type": canonical["type"],
                "bar_index": canonical["bar_index"],
                "price": canonical["price"],
                "extreme_price": canonical["extreme_price"],
                "boundary": canonical.get("boundary"),
                "related_event_id": canonical.get("related_event_id"),
                "why": text(submitted.get("why")),
            }
        )

    rejected_ids: set[str] = set()
    for submitted in rejected:
        if not isinstance(submitted, dict):
            errors.append("events_rejected contains a non-object")
            continue
        event_id = text(submitted.get("id"))
        submitted_ids.append(event_id)
        canonical = reviewable.get(event_id)
        if not canonical:
            errors.append(f"rejected event {event_id!r} is not a reviewable canonical candidate")
            continue
        errors.extend(event_identity_matches(submitted, canonical, rejected=True))
        rejected_ids.add(event_id)
        normalized_rejected.append(
            {
                "id": canonical["id"],
                "evidence_hash": canonical["evidence_hash"],
                "date": canonical["date"],
                "type": canonical["type"],
                "reason": text(submitted.get("reason")),
            }
        )

    if len(submitted_ids) != len(set(submitted_ids)):
        errors.append("candidate review contains duplicate event IDs")
    covered = confirmed_ids | rejected_ids
    missing_review = sorted(set(reviewable) - covered)
    extra_review = sorted(covered - set(reviewable))
    if missing_review:
        errors.append(f"candidate review is incomplete; missing {missing_review}")
    if extra_review:
        errors.append(f"candidate review contains non-reviewable IDs {extra_review}")

    evidence = structure.get("setup_evidence") if isinstance(structure.get("setup_evidence"), dict) else {}
    main_plan = analysis.get("main_plan") if isinstance(analysis.get("main_plan"), dict) else {}
    main_evidence = main_plan.get("setup_evidence") if isinstance(main_plan.get("setup_evidence"), dict) else {}
    if claims_trade and main_evidence != evidence:
        errors.append("main_plan.setup_evidence must exactly match structure.setup_evidence")
    core_id = text(evidence.get("core_event_id"))
    retest_id = text(evidence.get("retest_id"))
    core = by_id.get(core_id)
    retest = by_id.get(retest_id)
    if claims_trade:
        if not core or not retest:
            errors.append("trade action requires canonical core and retest IDs")
        else:
            if evidence.get("core_event_hash") != core.get("evidence_hash"):
                errors.append("setup_evidence.core_event_hash mismatch")
            if evidence.get("retest_hash") != retest.get("evidence_hash"):
                errors.append("setup_evidence.retest_hash mismatch")
            if core_id not in confirmed_ids or retest_id not in confirmed_ids:
                errors.append("selected core and retest must be in events_confirmed")
        reviewed_ids = evidence.get("reviewed_event_ids") if isinstance(evidence.get("reviewed_event_ids"), list) else []
        if sorted(reviewed_ids) != sorted(reviewable):
            errors.append("setup_evidence.reviewed_event_ids must exactly cover the canonical review set")

    pairs = event_pair_index(candidates)
    selected_pair = next(
        (item for item in pairs if item["core"].get("id") == core_id and item["retest"].get("id") == retest_id),
        None,
    )
    latest_pair = pairs[-1] if pairs else None
    if claims_trade:
        if selected_pair is None:
            errors.append("selected core/retest is not a canonical complete pair")
        if latest_pair and selected_pair and latest_pair["retest"].get("id") != selected_pair["retest"].get("id"):
            errors.append("selected setup is stale; a newer complete canonical pair exists")
        if selected_pair:
            max_age = {"1h": 72, "1d": 20}.get(str(candidates.get("interval")), 20)
            age = bars - 1 - int(selected_pair["retest"].get("bar_index") or -1)
            if age < 0 or age > max_age:
                errors.append(f"selected retest is stale ({age} bars old; maximum {max_age})")
            newer_core = [
                item
                for item in events
                if item.get("type") in {"SPRING", "UT", "SOS", "SOW"}
                and int(item.get("bar_index") or -1) > int(selected_pair["retest"].get("bar_index") or -1)
            ]
            if newer_core:
                errors.append("a newer unresolved core event exists after the selected retest")

    return core, retest, {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "reviewable_ids": sorted(reviewable),
        "normalized_confirmed": normalized_confirmed,
        "normalized_rejected": normalized_rejected,
        "selected_pair": selected_pair,
        "latest_pair": latest_pair,
    }


def range_validation(
    analysis: dict[str, Any], candidates: dict[str, Any], core: dict[str, Any] | None, retest: dict[str, Any] | None
) -> dict[str, Any]:
    errors: list[str] = []
    hint = candidates.get("structure_hint") if isinstance(candidates.get("structure_hint"), dict) else {}
    structure = analysis.get("structure") if isinstance(analysis.get("structure"), dict) else {}
    reference = analysis.get("reference_levels") if isinstance(analysis.get("reference_levels"), dict) else {}
    canonical_high = finite_number(hint.get("range_high"))
    canonical_low = finite_number(hint.get("range_low"))
    atr_value = finite_number(hint.get("atr"))
    if hint.get("has_range") is not True or canonical_high is None or canonical_low is None or atr_value is None:
        errors.append("canonical evidence has no reproducible trading range")
        return {"ok": False, "errors": errors, "score": 0}

    for field, canonical in [
        ("candidate_range_high", canonical_high),
        ("candidate_range_low", canonical_low),
        ("candidate_range_width_atr", finite_number(hint.get("width_atr"))),
    ]:
        submitted = finite_number(reference.get(field))
        if canonical is None or submitted is None or abs(submitted - canonical) > 1e-6:
            errors.append(f"reference_levels.{field} differs from canonical evidence")
    if reference.get("candidate_range_confirmed") is not True:
        errors.append("candidate range is not confirmed")
    if structure.get("has_trading_range") is not True:
        errors.append("structure.has_trading_range must be true for a trade")

    submitted_high = finite_number(structure.get("range_high"))
    submitted_low = finite_number(structure.get("range_low"))
    tolerance = 0.25 * atr_value
    if submitted_high is None or abs(submitted_high - canonical_high) > tolerance:
        errors.append("structure.range_high is not tied to the canonical range")
    if submitted_low is None or abs(submitted_low - canonical_low) > tolerance:
        errors.append("structure.range_low is not tied to the canonical range")
    if submitted_low is not None and submitted_high is not None and submitted_low >= submitted_high:
        errors.append("structure range is inverted")

    start = parse_timestamp(structure.get("range_start"))
    end = parse_timestamp(structure.get("range_end"))
    first = parse_timestamp(candidates.get("recent_bars", [{}])[0].get("date")) if candidates.get("recent_bars") else None
    asof = parse_timestamp(candidates.get("last_bar"))
    core_date = parse_timestamp(core.get("date")) if core else None
    retest_date = parse_timestamp(retest.get("date")) if retest else None
    if not start or not end or not asof:
        errors.append("range_start/range_end/asof must be valid dates")
    elif start > end or end > asof + timedelta(days=1):
        errors.append("range dates are outside the canonical analysis window")
    if start and core_date and start > core_date:
        errors.append("range_start must not be after the core event")
    if end and retest_date and end < retest_date:
        errors.append("range_end must include the selected retest")
    if first and start and start > asof:
        errors.append("range_start is after the analysis date")

    width_atr = finite_number(hint.get("width_atr")) or 0.0
    touches_ok = int(hint.get("upper_touches") or 0) >= 2 and int(hint.get("lower_touches") or 0) >= 2
    score = 2 if width_atr >= 5 and touches_ok else (1 if width_atr >= 3 and touches_ok else 0)
    return {
        "ok": not errors,
        "errors": errors,
        "score": score,
        "range_high": canonical_high,
        "range_low": canonical_low,
        "range_mid": (canonical_high + canonical_low) / 2.0,
        "range_width": canonical_high - canonical_low,
        "atr": atr_value,
    }


def basic_geometry(plan: dict[str, Any]) -> dict[str, Any]:
    direction = plan.get("direction")
    zone = plan.get("entry_zone")
    stop = finite_number(plan.get("stop"))
    t1 = finite_number(plan.get("t1"))
    t2 = finite_number(plan.get("t2"))
    if direction not in {"long", "short"} or not isinstance(zone, list) or len(zone) != 2:
        return {"valid": False, "reason": "direction or entry_zone is invalid"}
    low, high = finite_number(zone[0]), finite_number(zone[1])
    if low is None or high is None or low > high or stop is None or t1 is None or t2 is None:
        return {"valid": False, "reason": "entry/stop/T1/T2 is incomplete"}
    midpoint = (low + high) / 2.0
    ordered = stop < midpoint < t1 < t2 if direction == "long" else t2 < t1 < midpoint < stop
    if not ordered:
        return {"valid": False, "reason": "stop/entry/T1/T2 ordering is inconsistent with direction"}
    risk = abs(midpoint - stop)
    if risk <= 0:
        return {"valid": False, "reason": "zero risk distance"}
    return {
        "valid": True,
        "entry_low": low,
        "entry_high": high,
        "entry_mid": midpoint,
        "entry_width": high - low,
        "stop": stop,
        "t1": t1,
        "t2": t2,
        "risk": risk,
        "reward": abs(t1 - midpoint),
        "rr": abs(t1 - midpoint) / risk,
    }


def geometry_validation(
    plan: dict[str, Any], *, setup: str, core: dict[str, Any], retest: dict[str, Any], range_result: dict[str, Any]
) -> dict[str, Any]:
    errors: list[str] = []
    geometry = basic_geometry(plan)
    if not geometry.get("valid"):
        return {**geometry, "ok": False, "errors": [geometry.get("reason", "invalid geometry")]}
    direction = plan.get("direction")
    expected = ALLOWED_SETUPS.get(setup)
    if not expected or direction != expected["direction"]:
        errors.append("setup and direction do not match")

    atr_value = finite_number(core.get("atr")) if setup in {"spring_retest", "ut_retest"} else finite_number(retest.get("atr"))
    stop_reference = (
        finite_number(core.get("extreme_price"))
        if setup in {"spring_retest", "ut_retest"}
        else finite_number(retest.get("extreme_price"))
    )
    if atr_value is None or atr_value <= 0 or stop_reference is None:
        errors.append("canonical ATR or stop reference is unavailable")
        stop_buffer_atr = None
    else:
        stop_distance = stop_reference - geometry["stop"] if direction == "long" else geometry["stop"] - stop_reference
        stop_buffer_atr = stop_distance / atr_value
        if not 0.30 <= stop_buffer_atr <= 0.60:
            errors.append("stop must be 0.30-0.60 ATR beyond the canonical event extreme")

    retest_price = finite_number(retest.get("price"))
    max_zone_width = max(0.75 * float(range_result["atr"]), 0.15 * float(range_result["range_width"]))
    max_entry_distance = max(0.75 * float(range_result["atr"]), 0.20 * float(range_result["range_width"]))
    if geometry["entry_width"] > max_zone_width:
        errors.append("entry zone is wider than the canonical structure permits")
    if retest_price is None or abs(geometry["entry_mid"] - retest_price) > max_entry_distance:
        errors.append("entry zone is not anchored to the canonical retest price")

    high = float(range_result["range_high"])
    low = float(range_result["range_low"])
    mid = float(range_result["range_mid"])
    width = float(range_result["range_width"])
    atr = float(range_result["atr"])
    rr = float(geometry["rr"])
    if rr < 1.6:
        errors.append("actual RR(T1) is below 1.6")
    if rr > 10:
        errors.append("actual RR(T1) is implausibly high")

    if setup == "spring_retest":
        if not (max(mid, geometry["entry_mid"] + 1.6 * geometry["risk"]) <= geometry["t1"] <= high + 0.25 * atr):
            errors.append("Spring-retest T1 must be a plausible move toward the range high")
        if not (geometry["t1"] < geometry["t2"] <= high + width):
            errors.append("Spring-retest T2 exceeds the one-range projection envelope")
    elif setup == "ut_retest":
        if not (low - 0.25 * atr <= geometry["t1"] <= min(mid, geometry["entry_mid"] - 1.6 * geometry["risk"])):
            errors.append("UT-retest T1 must be a plausible move toward the range low")
        if not (low - width <= geometry["t2"] < geometry["t1"]):
            errors.append("UT-retest T2 exceeds the one-range projection envelope")
    elif setup == "sos_lps":
        if not (geometry["entry_mid"] + 1.6 * geometry["risk"] <= geometry["t1"] <= high + width):
            errors.append("SOS-LPS T1 exceeds the first range projection envelope")
        if not (geometry["t1"] < geometry["t2"] <= high + 2 * width):
            errors.append("SOS-LPS T2 exceeds the second range projection envelope")
    elif setup == "sow_lpsy":
        if not (low - width <= geometry["t1"] <= geometry["entry_mid"] - 1.6 * geometry["risk"]):
            errors.append("SOW-LPSY T1 exceeds the first range projection envelope")
        if not (low - 2 * width <= geometry["t2"] < geometry["t1"]):
            errors.append("SOW-LPSY T2 exceeds the second range projection envelope")

    return {**geometry, "ok": not errors, "errors": errors, "stop_reference": stop_reference, "stop_buffer_atr": stop_buffer_atr}


def volume_confirmation(setup: str, core: dict[str, Any], retest: dict[str, Any]) -> dict[str, Any]:
    core_vol = finite_number(core.get("vol_rel"))
    retest_vol = finite_number(retest.get("vol_rel"))
    low_volume = retest_vol is not None and retest_vol <= 1.0
    lower_than_core = core_vol is not None and retest_vol is not None and retest_vol < core_vol
    structural_hold = True
    core_extreme = finite_number(core.get("extreme_price"))
    retest_extreme = finite_number(retest.get("extreme_price"))
    boundary = finite_number(retest.get("boundary"))
    retest_atr = finite_number(retest.get("atr"))
    if setup == "spring_retest":
        structural_hold = core_extreme is not None and retest_extreme is not None and retest_extreme > core_extreme
    elif setup == "ut_retest":
        structural_hold = core_extreme is not None and retest_extreme is not None and retest_extreme < core_extreme
    elif setup == "sos_lps":
        structural_hold = (
            retest_extreme is not None and boundary is not None and retest_atr is not None
            and retest_extreme >= boundary - 0.15 * retest_atr
        )
    elif setup == "sow_lpsy":
        structural_hold = (
            retest_extreme is not None and boundary is not None and retest_atr is not None
            and retest_extreme <= boundary + 0.15 * retest_atr
        )
    score = int(low_volume) + int(lower_than_core and structural_hold)
    return {
        "score": min(2, score),
        "low_volume_retest": low_volume,
        "retest_volume_below_core": lower_than_core,
        "structural_hold": structural_hold,
        "core_vol_rel": core_vol,
        "retest_vol_rel": retest_vol,
        "supports": low_volume and structural_hold,
    }


def daily_freshness_validation(daily: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Require current-enough evidence for a directional trading recommendation."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    last_bar = parse_timestamp(daily.get("last_bar"))
    if not last_bar:
        return {"ok": False, "errors": ["daily last_bar is invalid"], "age_days": None, "maximum_age_days": None}
    age_days = (current - last_bar).total_seconds() / 86400.0
    symbol = str(daily.get("symbol") or "").upper()
    interval = str(daily.get("interval") or "")
    if interval == "1h":
        maximum = 2.0
    elif symbol.endswith("-USD"):
        maximum = 3.0
    else:
        maximum = 7.0
    errors: list[str] = []
    if age_days < -1.0:
        errors.append("daily evidence is from the future")
    elif age_days > maximum:
        errors.append(f"daily evidence is stale ({age_days:.1f} calendar days old; maximum {maximum:.0f})")
    return {"ok": not errors, "errors": errors, "age_days": round(age_days, 3), "maximum_age_days": maximum}


def htf_validation(daily: dict[str, Any], weekly: dict[str, Any], direction: str) -> dict[str, Any]:
    errors: list[str] = []
    if weekly.get("interval") != "1wk":
        errors.append("weekly evidence interval must be 1wk")
    if daily.get("symbol") != weekly.get("symbol"):
        errors.append("daily and weekly evidence symbols differ")
    daily_date = parse_timestamp(daily.get("last_bar"))
    weekly_date = parse_timestamp(weekly.get("last_bar"))
    if not daily_date or not weekly_date:
        errors.append("daily/weekly last_bar is invalid")
    elif weekly_date > daily_date + timedelta(days=1) or daily_date - weekly_date > timedelta(days=10):
        errors.append("weekly evidence is stale or from the future relative to daily evidence")
    quality = weekly.get("data_quality") if isinstance(weekly.get("data_quality"), dict) else {}
    if quality.get("status") != "ok" or quality.get("trade_analysis_eligible") is not True:
        errors.append("weekly data quality is not eligible")
    trend = weekly.get("trend_filter") if isinstance(weekly.get("trend_filter"), dict) else {}
    weekly_direction = trend.get("direction")
    if direction == "long" and weekly_direction == "up":
        score = 2
    elif direction == "short" and weekly_direction == "down":
        score = 2
    elif weekly_direction == "mixed":
        score = 1
    else:
        score = 0
        errors.append("weekly trend is opposed or unavailable")
    return {"ok": not errors, "errors": errors, "score": score, "direction": weekly_direction}


def deterministic_adjustments(candidates: dict[str, Any], retest: dict[str, Any], htf: dict[str, Any], volume: dict[str, Any]) -> list[dict[str, Any]]:
    bars = int(candidates.get("bars") or 0)
    age = bars - 1 - int(retest.get("bar_index") or -1)
    adjustments: list[dict[str, Any]] = []
    if 0 <= age <= 3:
        adjustments.append({"code": "RECENT_RETEST", "reason": "selected retest is within the latest three bars", "delta": 1})
    elif age > 10:
        adjustments.append({"code": "AGING_RETEST", "reason": "selected retest is more than ten bars old", "delta": -2})
    if volume.get("retest_vol_rel") is not None and volume["retest_vol_rel"] <= 0.75:
        adjustments.append({"code": "CLEAR_VOLUME_CONTRACTION", "reason": "retest relative volume is at or below 0.75", "delta": 1})
    if htf.get("score") == 1:
        adjustments.append({"code": "MIXED_WEEKLY", "reason": "weekly direction is mixed", "delta": -2})
    total = max(-4, min(4, sum(int(item["delta"]) for item in adjustments)))
    if total != sum(int(item["delta"]) for item in adjustments):
        adjustments.append({"code": "AGGREGATE_CLAMP", "reason": "aggregate deterministic adjustment is capped at +/-4", "delta": total - sum(int(item["delta"]) for item in adjustments)})
    return adjustments


def probability_band(score: int, adjustments: list[dict[str, Any]], *, available: bool) -> dict[str, Any]:
    total = max(-4, min(4, sum(int(item.get("delta") or 0) for item in adjustments)))
    if not available:
        low = high = base_low = base_high = None
        status = "unavailable"
    else:
        base_low = min(70, 34 + 3 * score)
        base_high = min(72, int(round(40 + 3.2 * score)))
        low = max(0, min(72, base_low + total))
        high = max(low, min(72, base_high + total))
        status = "validated_conditional"
    return {
        "method": "heuristic_uncalibrated",
        "event": "reach_t1_before_stop",
        "status": status,
        "base_low": base_low,
        "base_high": base_high,
        "adjustments": adjustments,
        "adjustment_total": total,
        "low": low,
        "high": high,
        "unit": "percent",
        "cap": 72,
        "historical_win_rate": False,
        "conditional_on": [
            "structured trigger is true on a canonical bar",
            "the validated trading range remains intact",
            "no newer contradictory setup supersedes the selected pair",
        ],
    }


def alternate_plan_assessment(
    plan: dict[str, Any],
    *,
    candidates: dict[str, Any],
    weekly: dict[str, Any],
    range_score: int,
) -> dict[str, Any]:
    """Score preplan A independently; it remains non-executable by contract."""
    direction = plan.get("direction")
    setup = plan.get("setup")
    geometry = basic_geometry(plan)
    direction_ok = bool(
        direction in {"long", "short"}
        and setup in ALLOWED_SETUPS
        and ALLOWED_SETUPS[setup]["direction"] == direction
    )
    rr = finite_number(geometry.get("rr"))
    rr_score = 0 if rr is None or rr < 1.6 else (1 if rr < 2.0 else 2)

    pairs = [item for item in event_pair_index(candidates) if item.get("setup") == setup]
    pair = pairs[-1] if pairs else None
    if pair:
        volume = volume_confirmation(setup, pair["core"], pair["retest"])
        event_score = 2
        volume_score = int(volume.get("score") or 0)
    else:
        volume = {"score": 0, "supports": False, "reason": "no canonical opposite pair"}
        event_score = 0
        volume_score = 0

    htf = htf_validation(candidates, weekly, direction) if direction_ok else {"score": 0, "direction": None}
    htf_score = int(htf.get("score") or 0)
    score = min(10, int(range_score) + event_score + volume_score + htf_score + rr_score)
    available = bool(direction_ok and geometry.get("valid") and rr is not None and rr >= 1.6)
    probability = probability_band(score, [], available=available)
    probability["status"] = "conditional_reference_only" if available else "unavailable"
    probability["conditional_on"] = [
        "the independently described reverse trigger occurs in the future",
        "a canonical opposite core-and-retest sequence is confirmed before entry",
        "the reverse plan is revalidated against then-current daily and weekly evidence",
    ]
    return {
        "direction": direction,
        "setup": setup,
        "geometry_valid": bool(geometry.get("valid")),
        "rr_t1": None if rr is None else round(rr, 4),
        "canonical_opposite_pair_present": pair is not None,
        "weekly_direction": htf.get("direction"),
        "parts": {
            "trading_range": int(range_score),
            "event_sequence": event_score,
            "volume_confirmation": volume_score,
            "higher_timeframe": htf_score,
            "reward_to_risk": rr_score,
        },
        "score_0_10": score,
        "probability": probability,
    }


def risk_contract_ok(analysis: dict[str, Any]) -> tuple[bool, list[str]]:
    risk = analysis.get("risk") if isinstance(analysis.get("risk"), dict) else {}
    errors: list[str] = []
    if finite_number(risk.get("default_risk_pct_equity")) != DEFAULT_RISK_PCT:
        errors.append("risk.default_risk_pct_equity must remain 0.75")
    if risk.get("allowed_without_user_override_pct") != [0.5, DEFAULT_RISK_PCT]:
        errors.append("risk.allowed_without_user_override_pct was modified")
    if finite_number(risk.get("max_correlated_risk_pct_equity")) != MAX_CORRELATED_RISK_PCT:
        errors.append("risk.max_correlated_risk_pct_equity must remain 1.5")
    if int(finite_number(risk.get("max_correlated_positions")) or -1) != MAX_CORRELATED_POSITIONS:
        errors.append("risk.max_correlated_positions must remain 2")
    if risk.get("skip_if") != DEFAULT_SKIP_IF:
        errors.append("risk.skip_if must remain the canonical hard-block list")
    return not errors, errors


def load_and_validate_risk_config(
    path: str | None,
    reference_symbol: str,
    geometry: dict[str, Any],
    *,
    root: Path,
) -> dict[str, Any]:
    errors: list[str] = []
    if not path:
        return {"ok": False, "errors": ["trade action requires --risk-config"], "position": {}, "profile": None}
    try:
        config = load_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"risk config unreadable: {exc}"], "position": {}, "profile": None}
    try:
        risk_schema = load_json(root / "references" / "risk-config.schema.json")
        validator = Draft202012Validator(risk_schema)
        for error in sorted(validator.iter_errors(config), key=lambda item: list(item.absolute_path)):
            location = ".".join(str(item) for item in error.absolute_path) or "$"
            errors.append(f"risk schema {location}: {error.message}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"risk config schema unavailable: {exc}")
    if config.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"risk config schema_version must be {SCHEMA_VERSION}")
    equity = finite_number(config.get("account_equity"))
    fx = finite_number(config.get("fx_rate_account_to_quote"))
    risk_pct = finite_number(config.get("risk_pct_equity"))
    cost = finite_number(config.get("estimated_cost_per_unit"))
    current_corr = finite_number(config.get("current_correlated_risk_pct_equity"))
    current_positions = finite_number(config.get("current_correlated_positions"))
    if equity is None or equity <= 0:
        errors.append("risk config account_equity must be positive")
    if fx is None or fx <= 0:
        errors.append("risk config fx_rate_account_to_quote must be positive")
    if risk_pct is None or risk_pct <= 0 or risk_pct > MAX_RISK_WITH_EXPLICIT_OVERRIDE_PCT:
        errors.append("risk_pct_equity must be >0 and <=1.0")
    elif risk_pct > DEFAULT_RISK_PCT and config.get("override_acknowledgement") != RISK_OVERRIDE_ACK:
        errors.append("risk above 0.75% requires the exact override acknowledgement")
    if cost is None or cost < 0:
        errors.append("estimated_cost_per_unit must be non-negative")
    if current_corr is None or current_corr < 0:
        errors.append("current_correlated_risk_pct_equity must be non-negative")
    elif risk_pct is not None and current_corr + risk_pct > MAX_CORRELATED_RISK_PCT + 1e-9:
        errors.append("new trade would exceed the 1.5% correlated-risk cap")
    if current_positions is None or int(current_positions) != current_positions or current_positions < 0:
        errors.append("current_correlated_positions must be a non-negative integer")
    elif current_positions + 1 > MAX_CORRELATED_POSITIONS:
        errors.append("new trade would exceed the correlated-position cap of 2")
    if not text(config.get("account_currency")):
        errors.append("account_currency is required")

    profile, profile_error = executable_instrument_profile(reference_symbol, config.get("actual_tradable_symbol"))
    if profile_error:
        errors.append(profile_error)
    position: dict[str, Any] = {}
    if not errors and profile and geometry.get("valid"):
        multiplier = finite_number(profile.get("contract_multiplier"))
        assert equity is not None and fx is not None and risk_pct is not None and cost is not None and multiplier is not None
        risk_amount = equity * risk_pct / 100.0
        risk_amount_quote = risk_amount * fx
        denominator = float(geometry["risk"]) * multiplier + cost
        units = risk_amount_quote / denominator if denominator > 0 else 0
        if profile.get("position_unit") in {"shares", "contracts"}:
            units = math.floor(units)
        else:
            units = round(units, 8)
        if units <= 0:
            errors.append("risk amount is too small for one executable position unit")
        position = {
            "source": "validator_risk_config",
            "risk_config_sha256": sha256_file(path),
            "risk_pct_equity": risk_pct,
            "account_equity": equity,
            "account_currency": text(config.get("account_currency")).upper(),
            "fx_rate_account_to_quote": fx,
            "risk_amount": round(risk_amount, 8),
            "risk_amount_quote": round(risk_amount_quote, 8),
            "estimated_cost_per_unit": cost,
            "actual_tradable_symbol": profile.get("actual_tradable_symbol"),
            "contract_multiplier": multiplier,
            "position_unit": profile.get("position_unit"),
            "formula": "units = risk_amount_quote / (abs(entry_mid - stop) * contract_multiplier + estimated_cost_per_unit)",
            "units": units,
        }
    return {"ok": not errors, "errors": errors, "position": position, "profile": profile, "config": config}


def sensitive_position_mismatches(submitted: dict[str, Any], computed: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in [
        "risk_pct_equity", "account_equity", "account_currency", "fx_rate_account_to_quote",
        "estimated_cost_per_unit", "actual_tradable_symbol", "contract_multiplier", "position_unit", "units",
    ]:
        value = submitted.get(field)
        if value is None:
            continue
        expected = computed.get(field)
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            if not exact_number(value, expected, tolerance=1e-6):
                errors.append(f"main_plan.position.{field} conflicts with trusted risk config")
        elif value != expected:
            errors.append(f"main_plan.position.{field} conflicts with trusted risk config")
    return errors


def plans_contract(analysis: dict[str, Any], *, claims_trade: bool, main_direction: str | None) -> tuple[bool, list[str]]:
    errors: list[str] = []
    main = analysis.get("main_plan") if isinstance(analysis.get("main_plan"), dict) else {}
    alt = analysis.get("alt_plan") if isinstance(analysis.get("alt_plan"), dict) else {}
    invalidation = analysis.get("invalidation") if isinstance(analysis.get("invalidation"), dict) else {}
    manage = analysis.get("manage") if isinstance(analysis.get("manage"), dict) else {}
    if not text(main.get("name")) or not text(main.get("trigger", {}).get("text")):
        errors.append("main plan name and trigger text are required")
    if claims_trade and text(main.get("unavailable_reason")):
        errors.append("executable main plan must clear unavailable_reason")
    if not text(alt.get("name")) or alt.get("action") not in SAFE_ACTIONS or not text(alt.get("when")):
        errors.append("preplan A must include name, wait/stand_aside action, and when")
    alt_direction = alt.get("direction")
    if claims_trade and alt_direction != ("short" if main_direction == "long" else "long"):
        errors.append("preplan A direction must oppose the main direction")
    if claims_trade:
        alt_geometry = basic_geometry(alt)
        if not alt_geometry.get("valid"):
            errors.append("preplan A requires entry zone, stop, T1, and T2 geometry")
        if alt.get("setup") not in ALLOWED_SETUPS or ALLOWED_SETUPS[alt.get("setup")]["direction"] != alt_direction:
            errors.append("preplan A setup does not match its direction")
        alt_trigger = alt.get("trigger") if isinstance(alt.get("trigger"), dict) else {}
        if alt_trigger.get("mode") != "retest_close_volume" or not text(alt_trigger.get("text")):
            errors.append("preplan A requires a structured future trigger")
        if alt_trigger.get("confirmed") is not False:
            errors.append("preplan A must remain unconfirmed; it is not an automatic reverse order")
    if not text(invalidation.get("name")) or not text(invalidation.get("structure_break")):
        errors.append("preplan B requires name and structure_break")
    do_not = invalidation.get("do_not") if isinstance(invalidation.get("do_not"), list) else []
    if len([item for item in do_not if text(item)]) < 3:
        errors.append("preplan B requires at least three prohibitions")
    time_stop = invalidation.get("time_stop") if isinstance(invalidation.get("time_stop"), dict) else {}
    if finite_number(time_stop.get("bars")) is None or time_stop.get("starts_after_trigger") is not True or not text(time_stop.get("rule")):
        errors.append("preplan B time_stop is incomplete")
    for field in ["name", "at_t1", "if_effort_no_result_against", "if_news"]:
        if not text(manage.get(field)):
            errors.append(f"preplan C manage.{field} is required")
    return not errors, errors


def unsafe_language(analysis: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    structure = analysis.get("structure") if isinstance(analysis.get("structure"), dict) else {}
    decision = analysis.get("decision") if isinstance(analysis.get("decision"), dict) else {}
    main = analysis.get("main_plan") if isinstance(analysis.get("main_plan"), dict) else {}
    alt = analysis.get("alt_plan") if isinstance(analysis.get("alt_plan"), dict) else {}
    risk = analysis.get("risk") if isinstance(analysis.get("risk"), dict) else {}
    fields.extend([
        text(structure.get("narrative")), text(decision.get("reason")),
        text((main.get("trigger") or {}).get("text")), text((alt.get("trigger") or {}).get("text")),
    ])
    fields.extend(text(item) for item in risk.get("correlation_notes") or [])
    fields.extend(text(item) for item in risk.get("market_context_conflicts") or [])
    combined = "\n".join(item for item in fields if item)
    return [pattern for pattern in UNSAFE_PATTERNS if re.search(pattern, combined, re.I)]


def prevalidation_contract(
    analysis: dict[str, Any],
    *,
    daily: dict[str, Any],
    weekly: dict[str, Any],
) -> list[str]:
    """Reject Agent self-assertions before deterministic validation runs."""
    errors: list[str] = []
    if analysis.get("validation_status") != "unvalidated":
        errors.append("input analysis.validation_status must be unvalidated")

    decision = analysis.get("decision") if isinstance(analysis.get("decision"), dict) else {}
    if decision.get("ready_to_execute") is not False:
        errors.append("input decision.ready_to_execute must be false")

    confluence = analysis.get("confluence") if isinstance(analysis.get("confluence"), dict) else {}
    if confluence.get("stage") != "pending_final_validation":
        errors.append("input confluence.stage must be pending_final_validation")
    if confluence.get("final_score_0_10") is not None:
        errors.append("input confluence.final_score_0_10 must be null")
    parts = confluence.get("parts") if isinstance(confluence.get("parts"), dict) else {}
    if any(value is not None for value in parts.values()):
        errors.append("input confluence parts must be null; the validator computes them")

    instrument = analysis.get("instrument") if isinstance(analysis.get("instrument"), dict) else {}
    expected_profile = analysis_instrument_profile(str(daily.get("symbol") or ""))
    for field in [
        "instrument_type",
        "position_unit",
        "contract_multiplier",
        "quote_currency",
        "sizing_requires_actual_contract",
        "note",
    ]:
        expected = expected_profile.get(field)
        submitted = instrument.get(field)
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            if not exact_number(submitted, expected):
                errors.append(f"input instrument.{field} differs from the trusted symbol profile")
        elif submitted != expected:
            errors.append(f"input instrument.{field} differs from the trusted symbol profile")
    if instrument.get("actual_tradable_symbol") is not None:
        errors.append("input instrument.actual_tradable_symbol must be null; risk config supplies it")

    submitted_quality = analysis.get("data_quality") if isinstance(analysis.get("data_quality"), dict) else {}
    if submitted_quality.get("daily") != daily.get("data_quality"):
        errors.append("input data_quality.daily differs from verified daily evidence")
    if submitted_quality.get("weekly") != weekly.get("data_quality"):
        errors.append("input data_quality.weekly differs from verified weekly evidence")

    main = analysis.get("main_plan") if isinstance(analysis.get("main_plan"), dict) else {}
    position = main.get("position") if isinstance(main.get("position"), dict) else {}
    trusted_fields = [
        "risk_config_sha256",
        "risk_pct_equity",
        "account_equity",
        "account_currency",
        "fx_rate_account_to_quote",
        "risk_amount",
        "risk_amount_quote",
        "estimated_cost_per_unit",
        "actual_tradable_symbol",
        "contract_multiplier",
        "position_unit",
        "units",
    ]
    if any(position.get(field) is not None for field in trusted_fields):
        errors.append("input main_plan.position must not prefill validator-controlled sizing fields")
    if main.get("rr_t1") is not None:
        errors.append("input main_plan.rr_t1 must be null")
    trigger = main.get("trigger") if isinstance(main.get("trigger"), dict) else {}
    if trigger.get("evidence_hash") is not None:
        errors.append("input main_plan.trigger.evidence_hash must be null")

    alt = analysis.get("alt_plan") if isinstance(analysis.get("alt_plan"), dict) else {}
    if alt.get("rr_t1") is not None:
        errors.append("input alt_plan.rr_t1 must be null")

    for location, probability in [
        ("probability", analysis.get("probability")),
        ("main_plan.p_t1_before_stop", main.get("p_t1_before_stop")),
        ("alt_plan.p_t1_before_stop", alt.get("p_t1_before_stop")),
    ]:
        value = probability if isinstance(probability, dict) else {}
        allowed_status = "pending_independent_validation" if location.startswith("alt_plan") else "pending_final_validation"
        if value.get("status") != allowed_status:
            errors.append(f"input {location}.status must be {allowed_status}")
        for field in ["base_low", "base_high", "low", "high"]:
            if value.get(field) is not None:
                errors.append(f"input {location}.{field} must be null")
        if value.get("adjustment_total") != 0 or value.get("adjustments") != []:
            errors.append(f"input {location} adjustments must be empty")
    return errors


def fail_closed(
    daily: dict[str, Any] | None,
    weekly: dict[str, Any] | None,
    *,
    requested_symbol: str,
    errors: list[str],
    daily_hash: str | None = None,
    weekly_hash: str | None = None,
) -> dict[str, Any]:
    if daily:
        safe = bp.skeleton(
            daily,
            weekly or {},
            requested_symbol or str(daily.get("symbol") or "UNKNOWN"),
            daily_candidates_sha256=daily_hash,
            weekly_candidates_sha256=weekly_hash,
        )
    else:
        safe = {
            "schema_version": SCHEMA_VERSION,
            "lineage": {"daily_run_id": None, "daily_candidates_sha256": None, "weekly_run_id": None, "weekly_candidates_sha256": None},
            "instrument": {"symbol": requested_symbol or "UNKNOWN", "requested_symbol": requested_symbol or "UNKNOWN", "last": None, "asof": None, "interval": "1d", **analysis_instrument_profile(requested_symbol or "UNKNOWN"), "actual_tradable_symbol": None},
            "data_quality": {"daily": {"status": "unavailable", "trade_analysis_eligible": False}, "weekly": {"status": "unavailable", "trade_analysis_eligible": False}},
            "reference_levels": {"candidate_range_high": None, "candidate_range_low": None, "candidate_range_width_atr": None, "candidate_range_confirmed": False, "note": "Validation failed before evidence could be established."},
            "structure": {"cycle": "unclear", "phase": None, "has_trading_range": False, "range_high": None, "range_low": None, "range_start": None, "range_end": None, "events_confirmed": [], "events_rejected": [], "setup_evidence": bp.setup_evidence_template(), "narrative": ""},
            "decision": {"recommendation": "no_trade", "watch_bias": "neutral", "ready_to_execute": False, "reason": "Validation failed."},
            "confluence": {"stage": "blocked", "final_score_0_10": None, "parts": {"trading_range": None, "event_sequence": None, "volume_confirmation": None, "higher_timeframe": None, "reward_to_risk": None}},
            "main_plan": bp.plan_template("主方案", "Validation failed."),
            "alt_plan": bp.plan_template("预案 A · 反向", "Validation failed."),
            "invalidation": {"name": "预案 B · 作废", "hard_stop_beyond": None, "structure_break": "校验失败，所有交易价位作废", "time_stop": {"bars": 8, "starts_after_trigger": True, "rule": "无有效触发，不启动时间止损"}, "do_not": ["止损后立即反手", "加仓摊平", "把概率当成必然"]},
            "manage": {"name": "预案 C · 持仓", "at_t1": "无有效持仓计划", "if_effort_no_result_against": "无有效持仓计划", "if_news": "无有效持仓计划", "additional_rules": []},
            "risk": {"default_risk_pct_equity": DEFAULT_RISK_PCT, "allowed_without_user_override_pct": [0.5, DEFAULT_RISK_PCT], "max_correlated_risk_pct_equity": MAX_CORRELATED_RISK_PCT, "max_correlated_positions": MAX_CORRELATED_POSITIONS, "skip_if": list(DEFAULT_SKIP_IF), "correlation_notes": [], "market_context_conflicts": []},
            "probability": bp.probability_template("unavailable"),
            "validation_status": "blocked",
            "disclaimer": STANDARD_DISCLAIMER,
        }
        safe["alt_plan"].pop("position", None)
    safe["validation_status"] = "blocked"
    safe["decision"] = {
        "recommendation": "no_trade",
        "watch_bias": "neutral",
        "ready_to_execute": False,
        "reason": "Blocked by deterministic validation: " + "; ".join(errors[:5]),
    }
    safe["main_plan"]["action"] = "wait"
    safe["main_plan"]["direction"] = None
    safe["main_plan"]["setup"] = None
    safe["main_plan"]["entry_zone"] = None
    safe["main_plan"]["stop"] = None
    safe["main_plan"]["t1"] = None
    safe["main_plan"]["t2"] = None
    safe["main_plan"]["rr_t1"] = None
    safe["main_plan"]["position"] = bp.position_template()
    safe["main_plan"]["p_t1_before_stop"] = bp.probability_template("unavailable")
    safe["main_plan"]["trigger"] = bp.trigger_template()
    safe["main_plan"]["unavailable_reason"] = "Blocked by deterministic validation."
    safe["alt_plan"]["action"] = "wait"
    safe["alt_plan"]["trigger"]["confirmed"] = False
    safe["probability"] = bp.probability_template("unavailable")
    safe["confluence"] = {
        "stage": "blocked",
        "final_score_0_10": None,
        "parts": {"trading_range": None, "event_sequence": None, "volume_confirmation": None, "higher_timeframe": None, "reward_to_risk": None},
    }
    safe["disclaimer"] = STANDARD_DISCLAIMER
    return safe


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate analysis.json against raw run evidence")
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--run-dir", default=None, help="Daily run directory containing raw and computed artifacts")
    parser.add_argument("--weekly-dir", default=None, help="Weekly run directory")
    parser.add_argument("--candidates", default=None, help="Compatibility: infer daily run directory from this path")
    parser.add_argument("--weekly-candidates", default=None, help="Compatibility: infer weekly run directory from this path")
    parser.add_argument("--risk-config", default=None, help="Trusted operator risk configuration; required for enter_long/enter_short")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--write-back", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    warnings: list[str] = []
    analysis: dict[str, Any] = {}
    daily_result: dict[str, Any] = {}
    weekly_result: dict[str, Any] = {}

    try:
        analysis = load_json(args.analysis)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"analysis unreadable: {exc}")

    if analysis:
        try:
            errors.extend(schema_issues(analysis, root))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"analysis schema unavailable: {exc}")

    try:
        daily_dir, weekly_dir = resolve_run_dirs(args)
        daily_result = verify_run(daily_dir)
        weekly_result = verify_run(weekly_dir)
        errors.extend(daily_result.get("errors") or [])
        errors.extend(weekly_result.get("errors") or [])
    except ValueError as exc:
        errors.append(str(exc))

    daily = daily_result.get("candidates") if daily_result.get("ok") else None
    weekly = weekly_result.get("candidates") if weekly_result.get("ok") else None
    requested_symbol = str(((analysis.get("instrument") or {}).get("requested_symbol") if analysis else None) or (daily or {}).get("symbol") or "UNKNOWN")

    if not analysis or not daily or not weekly:
        safe = fail_closed(
            daily,
            weekly,
            requested_symbol=requested_symbol,
            errors=errors or ["missing analysis or verified evidence"],
            daily_hash=daily_result.get("candidates_file_sha256"),
            weekly_hash=weekly_result.get("candidates_file_sha256"),
        )
        validation = {
            "schema_version": SCHEMA_VERSION,
            "valid": False,
            "claims_trade": False,
            "trade_gates_pass": False,
            "errors": errors or ["missing analysis or verified evidence"],
            "warnings": warnings,
            "hard_gates": {},
            "analysis_validated_sha256": sha256_json(safe),
        }
        atomic_write_json(out_dir / "analysis.validated.json", safe)
        atomic_write_json(out_dir / "validation.json", validation)
        print("VALID=false action=wait evidence=invalid gates=0/0")
        return 2

    main_plan = analysis.get("main_plan") if isinstance(analysis.get("main_plan"), dict) else {}
    alt_plan = analysis.get("alt_plan") if isinstance(analysis.get("alt_plan"), dict) else {}
    action = main_plan.get("action")
    direction = main_plan.get("direction")
    recommendation = ((analysis.get("decision") or {}).get("recommendation"))
    claims_trade = action in TRADE_ACTIONS

    # Bind analysis to verified daily/weekly evidence.
    lineage = analysis.get("lineage") if isinstance(analysis.get("lineage"), dict) else {}
    expected_lineage = {
        "daily_run_id": daily.get("run_id"),
        "daily_candidates_sha256": daily_result.get("candidates_file_sha256"),
        "weekly_run_id": weekly.get("run_id"),
        "weekly_candidates_sha256": weekly_result.get("candidates_file_sha256"),
    }
    for field, expected in expected_lineage.items():
        if lineage.get(field) != expected:
            errors.append(f"lineage.{field} does not match verified run evidence")

    instrument = analysis.get("instrument") if isinstance(analysis.get("instrument"), dict) else {}
    if instrument.get("symbol") != daily.get("symbol"):
        errors.append("analysis instrument.symbol differs from canonical daily symbol")
    if instrument.get("interval") != daily.get("interval"):
        errors.append("analysis instrument.interval differs from canonical daily interval")
    if instrument.get("asof") != daily.get("last_bar"):
        errors.append("analysis instrument.asof differs from canonical daily last_bar")
    if not exact_number(instrument.get("last"), daily.get("last_close")):
        errors.append("analysis instrument.last differs from canonical daily last_close")
    errors.extend(prevalidation_contract(analysis, daily=daily, weekly=weekly))

    daily_quality = daily.get("data_quality") if isinstance(daily.get("data_quality"), dict) else {}
    daily_quality_ok = daily_quality.get("status") == "ok" and daily_quality.get("trade_analysis_eligible") is True
    freshness_result = daily_freshness_validation(daily)
    if claims_trade:
        if not daily_quality_ok:
            errors.append("daily data quality is not eligible for a trade")
        errors.extend(freshness_result.get("errors") or [])

    if action not in TRADE_ACTIONS | SAFE_ACTIONS:
        errors.append("main_plan.action is invalid")
    if claims_trade:
        expected_action = "enter_long" if direction == "long" else "enter_short" if direction == "short" else None
        if action != expected_action:
            errors.append("trade action does not match main_plan.direction")
        if recommendation != direction:
            errors.append("decision.recommendation must match the executable direction")
    elif recommendation != "no_trade":
        errors.append("wait/stand_aside requires decision.recommendation=no_trade")

    unsafe_hits = unsafe_language(analysis)
    if unsafe_hits:
        errors.append(f"unsafe or misleading order language detected: {unsafe_hits}")
    if analysis.get("disclaimer") != STANDARD_DISCLAIMER:
        errors.append("disclaimer must equal the standard non-advisory statement")

    risk_ok, risk_errors = risk_contract_ok(analysis)
    errors.extend(risk_errors)
    plans_ok, plans_errors = plans_contract(analysis, claims_trade=claims_trade, main_direction=direction)
    errors.extend(plans_errors)

    core, retest, review = validate_review_and_pair(analysis, daily, claims_trade=claims_trade)
    errors.extend(review["errors"])
    warnings.extend(review["warnings"])

    range_result = range_validation(analysis, daily, core, retest) if claims_trade else {"ok": False, "errors": [], "score": 0}
    if claims_trade:
        errors.extend(range_result.get("errors") or [])

    setup = main_plan.get("setup")
    setup_match = bool(setup in ALLOWED_SETUPS and ALLOWED_SETUPS[setup]["direction"] == direction)
    if claims_trade and not setup_match:
        errors.append("main setup does not match the executable direction")

    trigger_result = {"confirmed": False}
    trigger_ok = False
    geometry_result = {"valid": False, "ok": False, "rr": None, "errors": []}
    volume_result = {"score": 0, "supports": False}
    htf_result = htf_validation(daily, weekly, direction) if claims_trade and direction in {"long", "short"} else {"ok": False, "errors": [], "score": 0, "direction": None}
    if claims_trade:
        errors.extend(htf_result.get("errors") or [])
        if core and retest and setup_match and range_result.get("ok"):
            trigger_ok, trigger_errors, trigger_result = structured_trigger(main_plan, direction=direction, retest=retest, frame=daily_result["frame"])
            errors.extend(trigger_errors)
            geometry_result = geometry_validation(main_plan, setup=setup, core=core, retest=retest, range_result=range_result)
            errors.extend(geometry_result.get("errors") or [])
            volume_result = volume_confirmation(setup, core, retest)
            if not volume_result.get("supports"):
                errors.append("canonical volume/retest evidence does not support the selected setup")

    invalidation = analysis.get("invalidation") if isinstance(analysis.get("invalidation"), dict) else {}
    time_stop = invalidation.get("time_stop") if isinstance(invalidation.get("time_stop"), dict) else {}
    expected_time_stop = bp.interval_time_stop(str(daily.get("interval") or "1d"))
    time_stop_ok = bool(
        int(finite_number(time_stop.get("bars")) or -1) == expected_time_stop
        and time_stop.get("starts_after_trigger") is True
        and text(time_stop.get("rule"))
    )
    invalidation_matches = bool(
        geometry_result.get("valid")
        and finite_number(invalidation.get("hard_stop_beyond")) is not None
        and abs(float(invalidation["hard_stop_beyond"]) - float(geometry_result["stop"])) <= 1e-8
    )
    if claims_trade and not (time_stop_ok and invalidation_matches):
        errors.append("preplan B hard stop or timeframe rule does not match the main plan")

    rr = finite_number(geometry_result.get("rr"))
    rr_score = 0 if rr is None or rr < 1.6 else (1 if rr < 2.0 else 2)
    event_score = 2 if review.get("ok") and review.get("selected_pair") else (1 if core else 0)
    range_score = int(range_result.get("score") or 0)
    volume_score = int(volume_result.get("score") or 0)
    htf_score = int(htf_result.get("score") or 0)
    final_score = min(10, range_score + event_score + volume_score + htf_score + rr_score)

    risk_result = load_and_validate_risk_config(
        args.risk_config,
        str(daily.get("symbol") or ""),
        geometry_result,
        root=root,
    )
    if claims_trade:
        errors.extend(risk_result["errors"])
        submitted_position = main_plan.get("position") if isinstance(main_plan.get("position"), dict) else {}
        if risk_result.get("position"):
            errors.extend(sensitive_position_mismatches(submitted_position, risk_result["position"]))

    adjustments = deterministic_adjustments(daily, retest, htf_result, volume_result) if claims_trade and retest else []
    probability_available = bool(
        claims_trade
        and daily_quality_ok
        and freshness_result.get("ok")
        and review.get("ok")
        and range_result.get("ok")
        and trigger_ok
        and geometry_result.get("ok")
        and volume_result.get("supports")
        and htf_result.get("ok")
        and risk_result.get("ok")
        and final_score >= 6
    )
    probability = probability_band(final_score, adjustments, available=probability_available)
    alternate = alternate_plan_assessment(
        alt_plan,
        candidates=daily,
        weekly=weekly,
        range_score=range_score,
    )

    hard_gates = {
        "analysis_schema_valid": not any(item.startswith("schema ") for item in errors),
        "daily_evidence_recomputed": daily_result.get("ok") is True,
        "weekly_evidence_recomputed": weekly_result.get("ok") is True,
        "daily_weekly_symbol_bound": daily.get("symbol") == weekly.get("symbol"),
        "weekly_fresh_and_aligned": htf_result.get("ok") is True,
        "analysis_lineage_bound": all(lineage.get(key) == value for key, value in expected_lineage.items()),
        "daily_data_quality_ok": daily_quality_ok,
        "daily_evidence_fresh": freshness_result.get("ok") is True,
        "candidate_review_complete": review.get("ok") is True,
        "selected_pair_latest_and_recent": review.get("selected_pair") is not None and not any("stale" in item or "newer" in item for item in review.get("errors") or []),
        "range_bound_to_canonical_evidence": range_result.get("ok") is True,
        "setup_direction_matches": setup_match,
        "structured_trigger_true": trigger_ok,
        "retest_structure_and_volume_valid": volume_result.get("supports") is True,
        "stop_buffer_0_3_to_0_6_atr": geometry_result.get("stop_buffer_atr") is not None and 0.30 <= geometry_result.get("stop_buffer_atr") <= 0.60,
        "entry_and_targets_plausible": geometry_result.get("ok") is True,
        "actual_rr_at_least_1_6": rr is not None and rr >= 1.6,
        "final_confluence_at_least_6": final_score >= 6,
        "plans_a_b_c_complete": plans_ok,
        "invalidation_matches_stop_and_timeframe": invalidation_matches and time_stop_ok,
        "risk_contract_immutable": risk_ok,
        "trusted_position_sizing_available": risk_result.get("ok") is True,
        "no_unsafe_order_language": not unsafe_hits,
        "standard_disclaimer_present": analysis.get("disclaimer") == STANDARD_DISCLAIMER,
    }
    trade_gates_pass = all(hard_gates.values())
    if claims_trade and not trade_gates_pass:
        errors.append("trade action is blocked by one or more deterministic hard gates")

    valid = not errors
    if valid:
        normalized = deepcopy(analysis)
        normalized["schema_version"] = SCHEMA_VERSION
        normalized["lineage"] = expected_lineage
        trusted_profile = risk_result.get("profile") if claims_trade else analysis_instrument_profile(str(daily.get("symbol") or ""))
        normalized["instrument"] = {
            "symbol": daily.get("symbol"),
            "requested_symbol": instrument.get("requested_symbol") or daily.get("symbol"),
            "last": daily.get("last_close"),
            "asof": daily.get("last_bar"),
            "interval": daily.get("interval"),
            **(trusted_profile or analysis_instrument_profile(str(daily.get("symbol") or ""))),
            "actual_tradable_symbol": (risk_result.get("profile") or {}).get("actual_tradable_symbol") if claims_trade else None,
        }
        normalized["data_quality"] = {"daily": daily.get("data_quality"), "weekly": weekly.get("data_quality")}
        hint = daily.get("structure_hint") or {}
        normalized["reference_levels"].update(
            {
                "candidate_range_high": hint.get("range_high"),
                "candidate_range_low": hint.get("range_low"),
                "candidate_range_width_atr": hint.get("width_atr"),
            }
        )
        normalized["structure"]["events_confirmed"] = review["normalized_confirmed"]
        normalized["structure"]["events_rejected"] = review["normalized_rejected"]
        if claims_trade and core and retest:
            normalized["structure"]["setup_evidence"] = {
                "core_event_id": core["id"],
                "core_event_hash": core["evidence_hash"],
                "retest_id": retest["id"],
                "retest_hash": retest["evidence_hash"],
                "reviewed_event_ids": review["reviewable_ids"],
            }
            normalized["main_plan"]["trigger"] = trigger_result
            normalized["main_plan"]["rr_t1"] = round(float(rr), 4)
            normalized["main_plan"]["position"] = risk_result["position"]
            normalized["main_plan"]["p_t1_before_stop"] = probability
            normalized["probability"] = probability
            normalized["alt_plan"]["rr_t1"] = alternate["rr_t1"]
            normalized["alt_plan"]["p_t1_before_stop"] = alternate["probability"]
            normalized["decision"]["ready_to_execute"] = True
            normalized["validation_status"] = "validated_trade"
        else:
            normalized["decision"] = {
                "recommendation": "no_trade",
                "watch_bias": normalized.get("decision", {}).get("watch_bias", "neutral"),
                "ready_to_execute": False,
                "reason": normalized.get("decision", {}).get("reason") or "No validated executable setup.",
            }
            normalized["main_plan"]["action"] = "wait"
            normalized["main_plan"]["p_t1_before_stop"] = bp.probability_template("unavailable")
            normalized["probability"] = bp.probability_template("unavailable")
            normalized["validation_status"] = "validated_no_trade"
        normalized["confluence"] = {
            "stage": "validated",
            "final_score_0_10": final_score if claims_trade else 0,
            "parts": {
                "trading_range": range_score,
                "event_sequence": event_score,
                "volume_confirmation": volume_score,
                "higher_timeframe": htf_score,
                "reward_to_risk": rr_score,
            },
        }
        normalized["risk"] = {
            "default_risk_pct_equity": DEFAULT_RISK_PCT,
            "allowed_without_user_override_pct": [0.5, DEFAULT_RISK_PCT],
            "max_correlated_risk_pct_equity": MAX_CORRELATED_RISK_PCT,
            "max_correlated_positions": MAX_CORRELATED_POSITIONS,
            "skip_if": analysis["risk"]["skip_if"],
            "correlation_notes": analysis["risk"]["correlation_notes"],
            "market_context_conflicts": analysis["risk"]["market_context_conflicts"],
        }
        normalized["disclaimer"] = STANDARD_DISCLAIMER
    else:
        normalized = fail_closed(
            daily,
            weekly,
            requested_symbol=requested_symbol,
            errors=errors,
            daily_hash=daily_result.get("candidates_file_sha256"),
            weekly_hash=weekly_result.get("candidates_file_sha256"),
        )

    analysis_hash = sha256_json(normalized)
    validation = {
        "schema_version": SCHEMA_VERSION,
        "valid": valid,
        "claims_trade": claims_trade,
        "trade_gates_pass": bool(valid and claims_trade and trade_gates_pass),
        "errors": errors,
        "warnings": warnings,
        "hard_gates": hard_gates,
        "evidence": {
            "daily_run_id": daily.get("run_id"),
            "weekly_run_id": weekly.get("run_id"),
            "daily_candidates_sha256": daily_result.get("candidates_file_sha256"),
            "weekly_candidates_sha256": weekly_result.get("candidates_file_sha256"),
            "risk_config_sha256": sha256_file(args.risk_config) if args.risk_config and Path(args.risk_config).exists() else None,
        },
        "computed": {
            "main": {
                "direction": direction,
                "setup": setup,
                "weekly_direction": htf_result.get("direction"),
                "daily_freshness": freshness_result,
                "rr_t1": None if rr is None else round(float(rr), 4),
                "stop_reference": geometry_result.get("stop_reference"),
                "stop_buffer_atr": geometry_result.get("stop_buffer_atr"),
                "final_confluence_0_10": final_score,
                "probability": probability,
                "trigger": trigger_result,
                "volume_confirmation": volume_result,
            },
            "alternate": alternate,
        },
        "analysis_validated_sha256": analysis_hash,
    }
    atomic_write_json(out_dir / "analysis.validated.json", normalized)
    atomic_write_json(out_dir / "validation.json", validation)
    if args.write_back:
        atomic_write_json(args.analysis, normalized)

    print(
        f"VALID={str(valid).lower()} action={normalized['main_plan']['action']} "
        f"score={final_score} p={probability.get('low')}-{probability.get('high')} "
        f"gates={sum(bool(value) for value in hard_gates.values())}/{len(hard_gates)}"
    )
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
