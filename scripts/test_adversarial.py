#!/usr/bin/env python3
"""Adversarial regression suite for every previously demonstrated bypass."""
from __future__ import annotations

import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import test_support as ts  # noqa: E402
import validate_analysis as va  # noqa: E402
from pipeline_common import sha256_file, sha256_json  # noqa: E402


class AdversarialGateTests(unittest.TestCase):
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

    def blocked(
        self,
        daily: Path,
        weekly: Path,
        *,
        risk_config: Path | None = None,
        contains: str | None = None,
    ) -> dict:
        validation = ts.validate_fixture(
            daily,
            weekly,
            risk_config=risk_config,
            expected=2,
        )
        self.assertFalse(validation["valid"])
        self.assertFalse(validation["trade_gates_pass"])
        if contains:
            self.assertTrue(
                any(contains.lower() in error.lower() for error in validation["errors"]),
                f"missing expected error {contains!r}: {validation['errors']}",
            )
        output = ts.load(daily / "analysis.validated.json")
        self.assertEqual(output["validation_status"], "blocked")
        self.assertEqual(output["decision"]["recommendation"], "no_trade")
        self.assertFalse(output["decision"]["ready_to_execute"])
        self.assertEqual(output["main_plan"]["action"], "wait")
        self.assertIsNone(output["main_plan"]["entry_zone"])
        self.assertIsNone(output["main_plan"]["stop"])
        self.assertIsNone(output["probability"]["low"])
        return validation

    @staticmethod
    def sync_weekly_analysis(daily: Path, weekly: Path) -> None:
        analysis = ts.load(daily / "analysis.json")
        candidates = ts.load(weekly / "candidates.json")
        analysis["lineage"]["weekly_run_id"] = candidates["run_id"]
        analysis["lineage"]["weekly_candidates_sha256"] = sha256_file(weekly / "candidates.json")
        analysis["data_quality"]["weekly"] = candidates["data_quality"]
        ts.write(daily / "analysis.json", analysis)

    def test_cross_symbol_weekly_is_blocked_even_when_lineage_is_updated(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            frame = ts.make_weekly_frame("up")
            ts.write_meta(weekly, frame, symbol="BTC-USD", interval="1wk")
            ts.compute_run(weekly)
            self.sync_weekly_analysis(daily, weekly)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="symbols differ")

    def test_entire_stale_dataset_is_blocked_for_trade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            daily = root / "daily"
            weekly = root / "weekly"
            frame = ts.make_daily_frame()
            frame["date"] = (pd.to_datetime(frame["date"]) - pd.Timedelta(days=120)).dt.strftime("%Y-%m-%d")
            weekly_frame = ts.make_weekly_frame("up", end=str(frame.iloc[-1]["date"]))
            ts.write_meta(daily, frame, symbol="SPY", interval="1d")
            ts.write_meta(weekly, weekly_frame, symbol="SPY", interval="1wk")
            ts.compute_run(daily)
            ts.compute_run(weekly)
            ts.populate_trade_analysis(daily, weekly)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="daily evidence is stale")

    def test_stale_weekly_is_blocked_even_when_lineage_is_updated(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            frame = ts.make_weekly_frame("up", end="2024-01-05")
            ts.write_meta(weekly, frame, symbol="SPY", interval="1wk")
            ts.compute_run(weekly)
            self.sync_weekly_analysis(daily, weekly)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="stale")

    def test_daily_interval_cannot_pose_as_weekly(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            frame = ts.make_weekly_frame("up")
            ts.write_meta(weekly, frame, symbol="SPY", interval="1d")
            ts.compute_run(weekly)
            self.sync_weekly_analysis(daily, weekly)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="interval must be 1wk")

    def test_opposed_weekly_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            frame = ts.make_weekly_frame("down")
            ts.write_meta(weekly, frame, symbol="SPY", interval="1wk")
            ts.compute_run(weekly)
            self.sync_weekly_analysis(daily, weekly)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="opposed")

    def test_future_trigger_bar_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["main_plan"]["trigger"]["bar_date"] = "2099-12-31"
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="retest date")

    def test_trivial_price_trigger_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["main_plan"]["trigger"]["price_rule"]["value"] = 0
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="range boundary")

    def test_permissive_volume_trigger_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["main_plan"]["trigger"]["volume_rule"]["value"] = 99
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="between 0.5 and 1.0")

    def test_fake_confirmed_event_date_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["structure"]["events_confirmed"][0]["date"] = "1900-01-01"
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="field date")

    def test_fake_confirmed_event_type_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["structure"]["events_confirmed"][0]["type"] = "UT"
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="field type")

    def test_fake_event_hash_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["structure"]["events_confirmed"][0]["evidence_hash"] = "0" * 64
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="evidence_hash")

    def test_incomplete_candidate_review_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["structure"]["events_rejected"] = analysis["structure"]["events_rejected"][:-1]
            analysis["structure"]["setup_evidence"]["reviewed_event_ids"] = analysis["structure"]["setup_evidence"]["reviewed_event_ids"][:-1]
            analysis["main_plan"]["setup_evidence"] = deepcopy(analysis["structure"]["setup_evidence"])
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="review is incomplete")

    def test_stale_pair_is_blocked_when_newer_pair_exists(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            candidates = ts.load(daily / "candidates.json")
            pairs = [pair for pair in va.event_pair_index(candidates) if pair["setup"] == "spring_retest"]
            self.assertGreaterEqual(len(pairs), 2)
            old = pairs[-2]
            analysis = ts.load(daily / "analysis.json")
            reviewable = ts.reviewable_events(candidates)
            selected = {old["core"]["id"], old["retest"]["id"]}
            analysis["structure"]["events_confirmed"] = [
                ts._confirmed(old["core"], "Older Spring candidate."),
                ts._confirmed(old["retest"], "Older retest candidate."),
            ]
            analysis["structure"]["events_rejected"] = [
                ts._rejected(event) for event in reviewable if event["id"] not in selected
            ]
            evidence = {
                "core_event_id": old["core"]["id"],
                "core_event_hash": old["core"]["evidence_hash"],
                "retest_id": old["retest"]["id"],
                "retest_hash": old["retest"]["evidence_hash"],
                "reviewed_event_ids": sorted(event["id"] for event in reviewable),
            }
            analysis["structure"]["setup_evidence"] = deepcopy(evidence)
            analysis["main_plan"]["setup_evidence"] = deepcopy(evidence)
            analysis["main_plan"]["trigger"]["bar_date"] = old["retest"]["date"]
            analysis["main_plan"]["trigger"]["price_rule"]["value"] = old["retest"]["boundary"]
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="stale")

    def test_duplicate_setup_evidence_sources_must_match(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["main_plan"]["setup_evidence"]["core_event_hash"] = "0" * 64
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="exactly match")

    def test_agent_cannot_self_assert_confluence(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["confluence"]["stage"] = "validated"
            analysis["confluence"]["final_score_0_10"] = 10
            analysis["confluence"]["parts"] = {key: 2 for key in analysis["confluence"]["parts"]}
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="pending_final_validation")

    def test_agent_cannot_self_assert_ready_to_execute(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["decision"]["ready_to_execute"] = True
            analysis["validation_status"] = "validated_trade"
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="must be unvalidated")

    def test_agent_cannot_prefill_contract_multiplier_or_units(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["main_plan"]["position"]["contract_multiplier"] = 0.0001
            analysis["main_plan"]["position"]["units"] = 99999999
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="must not prefill")

    def test_agent_cannot_spoof_instrument_profile(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["instrument"]["contract_multiplier"] = 0.0001
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="trusted symbol profile")

    def test_agent_cannot_spoof_data_quality(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["data_quality"]["daily"]["status"] = "fabricated"
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="data_quality.daily")

    def test_arbitrary_range_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["reference_levels"]["candidate_range_low"] = 1
            analysis["reference_levels"]["candidate_range_high"] = 1_000_000
            analysis["structure"]["range_low"] = 1
            analysis["structure"]["range_high"] = 1_000_000
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="canonical")

    def test_absurd_target_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["main_plan"]["t1"] = 1_000_000
            analysis["main_plan"]["t2"] = 2_000_000
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="implausibly high")

    def test_nanounit_stop_buffer_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            candidates = ts.load(daily / "candidates.json")
            pair = ts.latest_pair(candidates, setup="spring_retest")
            stop = float(pair["core"]["extreme_price"]) - 1e-9
            analysis["main_plan"]["stop"] = stop
            analysis["invalidation"]["hard_stop_beyond"] = stop
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="0.30-0.60 ATR")

    def test_wrong_side_stop_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["main_plan"]["stop"] = 103
            analysis["invalidation"]["hard_stop_beyond"] = 103
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="ordering")

    def test_rr_below_1_6_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            mid = sum(analysis["main_plan"]["entry_zone"]) / 2
            risk = mid - analysis["main_plan"]["stop"]
            analysis["main_plan"]["t1"] = mid + 1.59 * risk
            analysis["main_plan"]["t2"] = mid + 2.1 * risk
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="below 1.6")

    def test_missing_t2_is_blocked_by_schema(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["main_plan"]["t2"] = None
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="incomplete")

    def test_missing_reverse_plan_is_blocked_by_schema(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            del analysis["alt_plan"]
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="alt_plan")

    def test_reverse_setup_direction_mismatch_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["alt_plan"]["setup"] = "spring_retest"
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="does not match")

    def test_trade_without_risk_config_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            self.blocked(daily, weekly, contains="requires --risk-config")

    def test_risk_config_unknown_field_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            config = ts.load(daily / "risk_config.json")
            config["contract_multiplier"] = 0.0001
            ts.write(daily / "risk_config.json", config)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="additional properties")

    def test_risk_above_default_without_acknowledgement_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            config = ts.load(daily / "risk_config.json")
            config["risk_pct_equity"] = 0.9
            config["override_acknowledgement"] = None
            ts.write(daily / "risk_config.json", config)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="override")

    def test_correlated_risk_cap_is_enforced(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            config = ts.load(daily / "risk_config.json")
            config["current_correlated_risk_pct_equity"] = 1.0
            ts.write(daily / "risk_config.json", config)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="1.5%")

    def test_correlated_position_cap_is_enforced(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            config = ts.load(daily / "risk_config.json")
            config["current_correlated_positions"] = 2
            ts.write(daily / "risk_config.json", config)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="position cap")

    def test_equity_actual_symbol_mismatch_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            config = ts.load(daily / "risk_config.json")
            config["actual_tradable_symbol"] = "AAPL"
            ts.write(daily / "risk_config.json", config)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="must equal")

    def test_index_reference_cannot_size_etf_from_index_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            daily, weekly = ts.build_runs(root, symbol="^GSPC")
            ts.populate_trade_analysis(
                daily, weekly, requested_symbol="spx", actual_tradable_symbol="SPY"
            )
            self.blocked(
                daily, weekly,
                risk_config=daily / "risk_config.json",
                contains="rerun the full pipeline",
            )

    def test_gc_continuous_reference_without_delivery_contract_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            daily, weekly = ts.build_runs(root, symbol="GC=F")
            ts.populate_trade_analysis(daily, weekly, requested_symbol="GC=F", actual_tradable_symbol="GC=F")
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="actual gold contract")

    def test_gc_malformed_contract_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            daily, weekly = ts.build_runs(root, symbol="GC=F")
            ts.populate_trade_analysis(daily, weekly, requested_symbol="GC=F", actual_tradable_symbol="GOLD")
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="must match")

    def test_unsafe_now_market_buy_language_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["structure"]["narrative"] = "现在市价买入，保证盈利。"
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="unsafe")

    def test_disclaimer_cannot_be_replaced_with_guarantee(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["disclaimer"] = "代客操盘并保证盈利。"
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="disclaimer")

    def test_unknown_analysis_field_is_blocked_by_schema(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["hidden_override"] = True
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="additional properties")

    def test_decimal_probability_self_assertion_is_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["probability"]["status"] = "validated_conditional"
            analysis["probability"]["low"] = 0.732
            analysis["probability"]["high"] = 0.741
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="must be pending_final_validation")

    def test_raw_ohlcv_tamper_is_detected(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            frame = pd.read_csv(daily / "ohlcv.csv")
            frame.loc[100, "close"] += 0.01
            # Keep OHLC legal while changing the raw evidence hash.
            frame.loc[100, "high"] = max(frame.loc[100, "high"], frame.loc[100, "close"])
            frame.to_csv(daily / "ohlcv.csv", index=False)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="hash mismatch")

    def test_candidates_tamper_is_detected(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            candidates = ts.load(daily / "candidates.json")
            candidates["last_close"] += 1
            ts.write(daily / "candidates.json", candidates)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="candidates.json")

    def test_candidates_and_manifest_resign_still_fail_raw_recomputation(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            candidates = ts.load(daily / "candidates.json")
            candidates["events"][0]["price"] += 1
            ts.write(daily / "candidates.json", candidates)
            manifest = ts.load(daily / "run_manifest.json")
            manifest["artifacts"]["candidates.json"]["sha256"] = sha256_file(daily / "candidates.json")
            manifest["artifacts"]["candidates.json"]["canonical_sha256"] = sha256_json(candidates)
            ts.write(daily / "run_manifest.json", manifest)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="raw-data recomputation")

    def test_vpa_artifact_tamper_is_detected(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            with (daily / "vpa.csv").open("a", encoding="utf-8") as handle:
                handle.write("\n")
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="vpa.csv")

    def test_vpa_and_manifest_resign_still_fail_raw_recomputation(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            frame = pd.read_csv(daily / "vpa.csv")
            frame.loc[200, "vsa_residual"] = 99.0
            frame.to_csv(daily / "vpa.csv", index=False)
            manifest = ts.load(daily / "run_manifest.json")
            manifest["artifacts"]["vpa.csv"]["sha256"] = sha256_file(daily / "vpa.csv")
            ts.write(daily / "run_manifest.json", manifest)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="vpa.csv differs")

    def test_quality_and_manifest_resign_still_fail_raw_recomputation(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            quality = ts.load(daily / "data_quality.json")
            quality["status"] = "fabricated_ok"
            ts.write(daily / "data_quality.json", quality)
            manifest = ts.load(daily / "run_manifest.json")
            manifest["artifacts"]["data_quality.json"]["sha256"] = sha256_file(daily / "data_quality.json")
            ts.write(daily / "run_manifest.json", manifest)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="data_quality.json differs")

    def test_hint_and_manifest_resign_still_fail_raw_recomputation(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            hint = ts.load(daily / "structure_hint.json")
            hint["range_high"] = float(hint["range_high"]) + 1000.0
            ts.write(daily / "structure_hint.json", hint)
            manifest = ts.load(daily / "run_manifest.json")
            manifest["artifacts"]["structure_hint.json"]["sha256"] = sha256_file(daily / "structure_hint.json")
            ts.write(daily / "run_manifest.json", manifest)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="structure_hint.json differs")

    def test_noncanonical_compute_parameters_are_blocked(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            manifest = ts.load(daily / "run_manifest.json")
            manifest["parameters"]["wave_reversal_atr"] = 0.01
            ts.write(daily / "run_manifest.json", manifest)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="non-canonical compute parameters")

    def test_manifest_tamper_is_detected(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            manifest = ts.load(daily / "run_manifest.json")
            manifest["symbol"] = "BTC-USD"
            ts.write(daily / "run_manifest.json", manifest)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="manifest symbol")

    def test_retest_breaching_spring_extreme_is_not_emitted_as_valid_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = ts.make_daily_frame()
            frame.loc[247, "low"] = 98.0  # below the Spring extreme at 98.8
            ts.write_meta(root, frame, symbol="SPY", interval="1d")
            ts.compute_run(root)
            candidates = ts.load(root / "candidates.json")
            breached = [
                pair for pair in va.event_pair_index(candidates)
                if pair["retest"]["date"] == str(frame.loc[247, "date"])
            ]
            self.assertEqual(breached, [])

    def test_risk_hard_limits_cannot_be_relaxed_in_analysis(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["risk"]["max_correlated_risk_pct_equity"] = 99
            analysis["risk"]["max_correlated_positions"] = 999
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="must remain")

    def test_skip_if_hard_blocks_cannot_be_removed(self) -> None:
        temp, daily, weekly = self.fixture()
        with temp:
            analysis = ts.load(daily / "analysis.json")
            analysis["risk"]["skip_if"] = []
            ts.write(daily / "analysis.json", analysis)
            self.blocked(daily, weekly, risk_config=daily / "risk_config.json", contains="skip_if")


if __name__ == "__main__":
    unittest.main(verbosity=2)
