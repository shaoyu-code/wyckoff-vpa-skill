#!/usr/bin/env python3
"""Offline regression tests for the hardened Wyckoff VPA pipeline."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_plan as bp  # noqa: E402
import compute_vpa as cv  # noqa: E402
import fetch_ohlcv as fo  # noqa: E402
import make_chart as mc  # noqa: E402
import test_support as ts  # noqa: E402
import validate_analysis as va  # noqa: E402
from pipeline_common import SCHEMA_VERSION, sha256_file, sha256_json  # noqa: E402


class HardenedPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._base_tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._base_tmp.name) / "base"
        daily, weekly = ts.build_runs(cls.base)
        ts.populate_trade_analysis(daily, weekly)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._base_tmp.cleanup()

    def fixture(self) -> tuple[tempfile.TemporaryDirectory, Path, Path]:
        temp = tempfile.TemporaryDirectory()
        destination = Path(temp.name) / "fixture"
        daily, weekly = ts.clone_fixture(self.base, destination)
        return temp, daily, weekly

    def test_valid_trade_recomputes_all_hard_gates(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            validation = ts.validate_fixture(
                daily, weekly, risk_config=daily / "risk_config.json", expected=0
            )
            self.assertTrue(validation["valid"])
            self.assertTrue(validation["trade_gates_pass"])
            self.assertTrue(all(validation["hard_gates"].values()))
            self.assertGreaterEqual(validation["computed"]["main"]["rr_t1"], 1.6)
            self.assertLessEqual(validation["computed"]["main"]["probability"]["high"], 72)
            validated = ts.load(daily / "analysis.validated.json")
            self.assertEqual(validated["validation_status"], "validated_trade")
            self.assertGreater(validated["main_plan"]["position"]["units"], 0)
            self.assertEqual(
                validated["main_plan"]["p_t1_before_stop"]["status"],
                "validated_conditional",
            )
            self.assertEqual(
                validated["alt_plan"]["p_t1_before_stop"]["status"],
                "conditional_reference_only",
            )

    def test_valid_no_trade_requires_review_but_no_risk_config(self) -> None:
        temp = tempfile.TemporaryDirectory()
        with temp:
            root = Path(temp.name)
            daily, weekly = ts.build_runs(root)
            ts.populate_no_trade_analysis(daily, weekly)
            validation = ts.validate_fixture(daily, weekly, expected=0)
            self.assertTrue(validation["valid"])
            self.assertFalse(validation["trade_gates_pass"])
            output = ts.load(daily / "analysis.validated.json")
            self.assertEqual(output["main_plan"]["action"], "wait")
            self.assertEqual(output["decision"]["recommendation"], "no_trade")
            self.assertIsNone(output["probability"]["low"])

    def test_plan_skeleton_has_no_executable_geometry(self) -> None:
        candidates = ts.load(self.base / "daily" / "candidates.json")
        weekly = ts.load(self.base / "weekly" / "candidates.json")
        plan = bp.skeleton(candidates, weekly, "SPY")
        self.assertEqual(plan["main_plan"]["action"], "wait")
        self.assertFalse(plan["decision"]["ready_to_execute"])
        for key in ["entry_zone", "stop", "t1", "t2", "rr_t1"]:
            self.assertIsNone(plan["main_plan"][key])
        self.assertEqual(plan["probability"]["status"], "pending_final_validation")

    def test_compute_emits_candidates_only_and_manifest(self) -> None:
        candidates = ts.load(self.base / "daily" / "candidates.json")
        manifest = ts.load(self.base / "daily" / "run_manifest.json")
        self.assertFalse(candidates["precheck"]["tradeable"])
        self.assertFalse(candidates["precheck"]["final_confluence_available"])
        self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
        self.assertEqual(manifest["run_id"], candidates["run_id"])
        self.assertEqual(
            manifest["artifacts"]["candidates.json"]["canonical_sha256"],
            sha256_json(candidates),
        )

    def test_candidate_events_have_context_and_evidence_hash(self) -> None:
        candidates = ts.load(self.base / "daily" / "candidates.json")
        for event in candidates["events"]:
            self.assertGreaterEqual(len(event["context_bars"]), 2)
            self.assertEqual(len(event["evidence_hash"]), 64)
            canonical = deepcopy(event)
            supplied = canonical.pop("evidence_hash")
            self.assertEqual(supplied, sha256_json(canonical))

    def test_recent_spring_retest_does_not_breach_core_extreme(self) -> None:
        candidates = ts.load(self.base / "daily" / "candidates.json")
        pair = ts.latest_pair(candidates, setup="spring_retest")
        self.assertGreater(pair["retest"]["extreme_price"], pair["core"]["extreme_price"])
        self.assertLessEqual(pair["retest"]["vol_rel"], 1.0)

    def test_short_data_degrades_without_probability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = ts.make_daily_frame().head(20)
            ts.write_meta(root, frame, symbol="SPY", interval="1d")
            ts.compute_run(root)
            candidates = ts.load(root / "candidates.json")
            self.assertFalse(candidates["data_quality"]["trade_analysis_eligible"])
            self.assertEqual(candidates["events"], [])
            self.assertFalse(candidates["precheck"]["tradeable"])

    def test_zero_volume_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = ts.make_daily_frame()
            frame["volume"] = 0.0
            ts.write_meta(root, frame, symbol="SPY", interval="1d")
            ts.compute_run(root)
            candidates = ts.load(root / "candidates.json")
            self.assertEqual(candidates["data_quality"]["status"], "volume_unavailable")
            self.assertFalse(candidates["data_quality"]["trade_analysis_eligible"])

    def test_invalid_ohlc_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = ts.make_daily_frame()
            frame.loc[10, "close"] = frame.loc[10, "high"] + 5
            ts.write_meta(root, frame, symbol="SPY", interval="1d")
            result = ts.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "compute_vpa.py"),
                    "--csv",
                    str(root / "ohlcv.csv"),
                    "--meta",
                    str(root / "meta.json"),
                    "--out-dir",
                    str(root),
                ],
                expected=2,
            )
            self.assertIn("impossible OHLC", result.stdout)

    def test_duplicate_dates_are_deduplicated_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = pd.concat([ts.make_daily_frame(), ts.make_daily_frame().tail(1)], ignore_index=True)
            ts.write_meta(root, frame, symbol="SPY", interval="1d")
            ts.compute_run(root)
            quality = ts.load(root / "data_quality.json")
            self.assertEqual(quality["duplicates_removed"], 1)

    def test_trend_does_not_emit_range_spring_or_ut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = ts.make_trend_frame()
            ts.write_meta(root, frame, symbol="SPY", interval="1d")
            ts.compute_run(root)
            candidates = ts.load(root / "candidates.json")
            self.assertFalse(candidates["structure_hint"]["has_range"])
            self.assertFalse({"SPRING", "UT"} & {event["type"] for event in candidates["events"]})

    def test_threshold_waves_do_not_flip_on_every_bar(self) -> None:
        frame, _ = cv.load_ohlcv(str(self.base / "daily" / "ohlcv.csv"))
        frame["atr"] = cv.atr(frame)
        waves = cv.threshold_waves(frame)
        self.assertLess(waves["wave_id"].nunique(), len(frame) / 3)
        self.assertIn("wave_volume_rel", waves)

    def test_resolved_alias_instrument_profiles(self) -> None:
        btc = bp.skeleton({"symbol": "BTC-USD", "interval": "1d"}, {}, "btc")
        gold = bp.skeleton({"symbol": "GC=F", "interval": "1d"}, {}, "gold")
        self.assertEqual(btc["instrument"]["instrument_type"], "spot_reference")
        self.assertEqual(btc["instrument"]["contract_multiplier"], 1.0)
        self.assertEqual(gold["instrument"]["instrument_type"], "continuous_futures_reference")
        self.assertIsNone(gold["instrument"]["contract_multiplier"])
        self.assertTrue(gold["instrument"]["sizing_requires_actual_contract"])

    def test_gold_contract_profiles_use_trusted_multiplier(self) -> None:
        profile, error = va.executable_instrument_profile("GC=F", "GCZ26")
        self.assertIsNone(error)
        self.assertEqual(profile["contract_multiplier"], 100.0)
        micro, error = va.executable_instrument_profile("GC=F", "MGCZ26")
        self.assertIsNone(error)
        self.assertEqual(micro["contract_multiplier"], 10.0)
        _, error = va.executable_instrument_profile("GC=F", "GC=F")
        self.assertIn("actual gold contract", error)

    def test_index_reference_cannot_be_sized_through_etf_proxy(self) -> None:
        profile, error = va.executable_instrument_profile("^GSPC", "SPY")
        self.assertIsNone(profile)
        self.assertIn("rerun the full pipeline", error)

    def test_risk_config_schema_is_valid_and_enforced(self) -> None:
        schema = ts.load(ROOT / "references" / "risk-config.schema.json")
        Draft202012Validator.check_schema(schema)
        config = ts.load(self.base / "daily" / "risk_config.json")
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(config)), [])
        config["risk_pct_equity"] = 0.9
        config["override_acknowledgement"] = None
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(config)))

    def test_position_sizing_uses_fx_and_costs(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            config = ts.load(daily / "risk_config.json")
            config["account_currency"] = "CNY"
            config["fx_rate_account_to_quote"] = 0.14
            ts.write(daily / "risk_config.json", config)
            validation = ts.validate_fixture(daily, weekly, risk_config=daily / "risk_config.json", expected=0)
            output = ts.load(daily / "analysis.validated.json")
            position = output["main_plan"]["position"]
            self.assertAlmostEqual(position["risk_amount_quote"], 105.0)
            self.assertGreater(position["units"], 0)
            self.assertTrue(validation["hard_gates"]["trusted_position_sizing_available"])

    def test_probability_is_integer_band_and_capped(self) -> None:
        for score in range(11):
            p = va.probability_band(score, [{"code": "x", "reason": "x", "delta": 4}], available=True)
            self.assertIsInstance(p["low"], int)
            self.assertIsInstance(p["high"], int)
            self.assertLessEqual(p["high"], 72)
            self.assertLessEqual(p["low"], p["high"])
            self.assertFalse(p["historical_win_rate"])

    def test_split_adjustment_is_idempotent(self) -> None:
        # Already adjusted series: pre/post closes are both near 100, so no second adjustment.
        rows = [
            {"ts": 100, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0},
            {"ts": 200, "open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0, "volume": 1100.0},
        ]
        node = {"events": {"splits": {"150": {"date": 150, "numerator": 4, "denominator": 1, "splitRatio": "4:1"}}}}
        events = fo.apply_split_adjustments(rows, node)
        self.assertFalse(events[0]["adjustment_applied"])
        self.assertEqual(rows[0]["close"], 100.0)

        # Unadjusted 4:1 discontinuity: pre-split 400 becomes 100 after one adjustment.
        rows = [
            {"ts": 100, "open": 400.0, "high": 404.0, "low": 396.0, "close": 400.0, "volume": 250.0},
            {"ts": 200, "open": 101.0, "high": 102.0, "low": 99.0, "close": 100.0, "volume": 1000.0},
        ]
        events = fo.apply_split_adjustments(rows, node)
        self.assertTrue(events[0]["adjustment_applied"])
        self.assertEqual(rows[0]["close"], 100.0)
        self.assertEqual(rows[0]["volume"], 1000.0)

    def test_safe_script_json_blocks_script_boundary(self) -> None:
        encoded = mc.safe_script_json({"text": "</script><img src=x onerror=alert(1)>&\u2028"})
        self.assertNotIn("</script>", encoded.lower())
        self.assertNotIn("<img", encoded.lower())
        self.assertIn("\\u003c/script\\u003e", encoded)
        self.assertIn("\\u0026", encoded)

    def test_fail_closed_output_passes_schema_and_has_no_trade_geometry(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["main_plan"]["trigger"]["bar_date"] = "2099-12-31"
            ts.write(daily / "analysis.json", analysis)
            validation = ts.validate_fixture(
                daily, weekly, risk_config=daily / "risk_config.json", expected=2
            )
            self.assertFalse(validation["valid"])
            output = ts.load(daily / "analysis.validated.json")
            schema = ts.load(ROOT / "references" / "analysis.schema.json")
            self.assertEqual(list(Draft202012Validator(schema).iter_errors(output)), [])
            self.assertEqual(output["main_plan"]["action"], "wait")
            self.assertEqual(output["decision"]["recommendation"], "no_trade")
            for key in ["entry_zone", "stop", "t1", "t2", "rr_t1"]:
                self.assertIsNone(output["main_plan"][key])

    def test_write_back_replaces_failed_trade_with_safe_no_trade(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["main_plan"]["trigger"]["bar_date"] = "2099-12-31"
            ts.write(daily / "analysis.json", analysis)
            result = ts.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "validate_analysis.py"),
                    "--analysis",
                    str(daily / "analysis.json"),
                    "--run-dir",
                    str(daily),
                    "--weekly-dir",
                    str(weekly),
                    "--risk-config",
                    str(daily / "risk_config.json"),
                    "--out-dir",
                    str(daily),
                    "--write-back",
                ],
                expected=2,
            )
            self.assertIn("VALID=false", result.stdout)
            written_back = ts.load(daily / "analysis.json")
            self.assertEqual(written_back["validation_status"], "blocked")
            self.assertEqual(written_back["decision"]["recommendation"], "no_trade")
            self.assertEqual(written_back["main_plan"]["action"], "wait")
            self.assertIsNone(written_back["main_plan"]["entry_zone"])

    def test_chart_rejects_resigned_vpa_tamper_after_validation(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            ts.validate_fixture(daily, weekly, risk_config=daily / "risk_config.json", expected=0)
            frame = pd.read_csv(daily / "vpa.csv")
            frame.loc[200, "vsa_residual"] = 99.0
            frame.to_csv(daily / "vpa.csv", index=False)
            manifest = ts.load(daily / "run_manifest.json")
            manifest["artifacts"]["vpa.csv"]["sha256"] = sha256_file(daily / "vpa.csv")
            ts.write(daily / "run_manifest.json", manifest)
            result = ts.run(
                [sys.executable, str(ROOT / "scripts" / "make_chart.py"), "--dir", str(daily)],
                expected=2,
            )
            self.assertIn("raw-data recomputation", result.stderr.lower())

    def test_chart_requires_validated_hash_bound_artifacts(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            result = ts.run(
                [sys.executable, str(ROOT / "scripts" / "make_chart.py"), "--dir", str(daily)],
                expected=2,
            )
            self.assertIn("validation", result.stderr.lower())
            ts.validate_fixture(daily, weekly, risk_config=daily / "risk_config.json", expected=0)
            ts.run([sys.executable, str(ROOT / "scripts" / "make_chart.py"), "--dir", str(daily)], expected=0)
            self.assertTrue((daily / "chart.html").exists())
            validation = ts.load(daily / "validation.json")
            validation["analysis_validated_sha256"] = "0" * 64
            ts.write(daily / "validation.json", validation)
            ts.run([sys.executable, str(ROOT / "scripts" / "make_chart.py"), "--dir", str(daily)], expected=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
