#!/usr/bin/env python3
"""Validate stored examples and regenerate-equivalent end-to-end fixture cases.

Stored ``analysis.*.example.json`` files are post-validation outputs intended
for readers and chart consumers.  Stored ``analysis.*.input.example.json``
files show the Agent submission contract before deterministic validation.
The raw evidence chain is exercised independently with deterministic fixtures
so examples cannot replace the actual validator trust boundary.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import test_support as ts  # noqa: E402
from pipeline_common import SCHEMA_VERSION, sha256_json  # noqa: E402

EXAMPLES = ROOT / "examples"
ANALYSIS_SCHEMA = ROOT / "references" / "analysis.schema.json"
RISK_SCHEMA = ROOT / "references" / "risk-config.schema.json"


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return value


def schema_errors(validator: Draft202012Validator, value: dict[str, Any]) -> list[str]:
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    return [
        f"{'/'.join(map(str, error.path)) or '<root>'}: {error.message}"
        for error in errors
    ]


def assert_probability_bands(value: object, path: str = "root") -> None:
    if isinstance(value, dict):
        if value.get("method") == "heuristic_uncalibrated" and value.get("event") == "reach_t1_before_stop":
            low, high = value.get("low"), value.get("high")
            for name, number in [("low", low), ("high", high)]:
                if number is not None and (not isinstance(number, int) or isinstance(number, bool) or not 0 <= number <= 72):
                    raise AssertionError(f"{path}.{name} must be an integer in 0..72 or null")
            if low is not None and high is not None and low > high:
                raise AssertionError(f"{path} has low > high")
            if value.get("historical_win_rate") is not False:
                raise AssertionError(f"{path}.historical_win_rate must be false")
            if value.get("cap") != 72:
                raise AssertionError(f"{path}.cap must equal 72")
        for key, item in value.items():
            assert_probability_bands(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_probability_bands(item, f"{path}[{index}]")


def validate_stored_case(name: str, validator: Draft202012Validator) -> None:
    input_path = EXAMPLES / f"analysis.{name}.input.example.json"
    output_path = EXAMPLES / f"analysis.{name}.example.json"
    validation_path = EXAMPLES / f"validation.{name}.example.json"
    candidates_path = EXAMPLES / f"candidates.{name}.example.json"
    weekly_path = EXAMPLES / f"weekly.{name}.example.json"
    for required in [input_path, output_path, validation_path, candidates_path, weekly_path]:
        if not required.exists():
            raise AssertionError(f"missing example: {required}")

    submitted = load(input_path)
    validated = load(output_path)
    validation = load(validation_path)
    candidates = load(candidates_path)
    weekly = load(weekly_path)

    for path, value in [(input_path, submitted), (output_path, validated)]:
        errors = schema_errors(validator, value)
        if errors:
            raise AssertionError(f"schema errors in {path.name}:\n- " + "\n- ".join(errors))
        assert_probability_bands(value, path.name)

    for path, value in [(candidates_path, candidates), (weekly_path, weekly), (validation_path, validation)]:
        if value.get("schema_version") != SCHEMA_VERSION:
            raise AssertionError(f"{path.name} must use schema_version={SCHEMA_VERSION}")

    if submitted.get("validation_status") != "unvalidated":
        raise AssertionError(f"{input_path.name} must be an unvalidated Agent submission")
    if validation.get("valid") is not True:
        raise AssertionError(f"{validation_path.name} must record valid=true")
    if validation.get("analysis_validated_sha256") != sha256_json(validated):
        raise AssertionError(f"{validation_path.name} does not bind {output_path.name}")

    if name == "tradeable":
        if validated.get("validation_status") != "validated_trade":
            raise AssertionError("tradeable output must be validated_trade")
        if validation.get("trade_gates_pass") is not True:
            raise AssertionError("tradeable validation must pass trade gates")
        if validated.get("main_plan", {}).get("action") not in {"enter_long", "enter_short"}:
            raise AssertionError("tradeable output must contain a validated directional action")
    else:
        if validated.get("validation_status") != "validated_no_trade":
            raise AssertionError("no-trade output must be validated_no_trade")
        if validation.get("trade_gates_pass") is not False:
            raise AssertionError("no-trade validation must not claim trade gates pass")
        if validated.get("decision", {}).get("recommendation") != "no_trade":
            raise AssertionError("no-trade output must remain no_trade")
        if validated.get("main_plan", {}).get("action") != "wait":
            raise AssertionError("no-trade output must remain wait")


def validate_dynamic_e2e() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        daily, weekly = ts.build_runs(root / "tradeable")
        ts.populate_trade_analysis(daily, weekly)
        trade_validation = ts.validate_fixture(
            daily,
            weekly,
            risk_config=daily / "risk_config.json",
            expected=0,
        )
        if not (trade_validation.get("valid") is True and trade_validation.get("trade_gates_pass") is True):
            raise AssertionError("dynamic tradeable example failed deterministic validation")

        daily, weekly = ts.build_runs(root / "no_trade")
        ts.populate_no_trade_analysis(daily, weekly)
        no_trade_validation = ts.validate_fixture(daily, weekly, expected=0)
        if no_trade_validation.get("valid") is not True or no_trade_validation.get("trade_gates_pass") is not False:
            raise AssertionError("dynamic no-trade example failed deterministic validation")


def main() -> int:
    analysis_schema = load(ANALYSIS_SCHEMA)
    risk_schema = load(RISK_SCHEMA)
    Draft202012Validator.check_schema(analysis_schema)
    Draft202012Validator.check_schema(risk_schema)
    analysis_validator = Draft202012Validator(analysis_schema)
    risk_validator = Draft202012Validator(risk_schema)

    validate_stored_case("tradeable", analysis_validator)
    validate_stored_case("no_trade", analysis_validator)

    risk = load(EXAMPLES / "risk-config.tradeable.example.json")
    errors = schema_errors(risk_validator, risk)
    if errors:
        raise AssertionError("risk config schema errors:\n- " + "\n- ".join(errors))

    validate_dynamic_e2e()

    stale = {
        "analysis.example.json",
        "plan.example.json",
        "plan.sample.json",
        "BTC-USD.candidates.json",
        "BTC-USD.plan_skeleton.json",
    }
    present = sorted(path.name for path in EXAMPLES.iterdir() if path.name in stale)
    if present:
        raise AssertionError(f"stale incompatible examples remain: {present}")

    print("OK stored schema examples + 2 deterministic end-to-end cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
