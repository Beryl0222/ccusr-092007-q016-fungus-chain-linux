import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fungus_chain import (  # noqa: E402
    ChainBundle,
    ChainIntegrityError,
    clone_for_mutation,
    load_bundle,
    load_record,
)

FIXTURE = ROOT / "fixtures" / "sample_transfer.json"


def bundle() -> ChainBundle:
    return load_bundle(FIXTURE)


def mutated(mutate) -> ChainBundle:
    b = clone_for_mutation(bundle())
    mutate(b.raw)
    return b


def expect_violation(self: unittest.TestCase, b: ChainBundle, keyword: str):
    violations = b.validate()
    self.assertTrue(violations, "应至少报告一条不变量违规")
    self.assertTrue(
        any(keyword in v for v in violations),
        f"期望出现与「{keyword}」相关的违规，实际：{violations}",
    )


class GoldenPathTest(unittest.TestCase):
    def test_fixture_is_a_valid_chain(self):
        self.assertEqual(bundle().validate(), [])

    def test_check_raises_on_violation(self):
        b = mutated(lambda raw: raw["samples"][0].pop("seal_id"))
        with self.assertRaises(ChainIntegrityError):
            b.check()

    def test_v1_envelope_contract_still_loads(self):
        record = load_record(FIXTURE)
        self.assertEqual(record.domain, "fungus_chain")
        self.assertEqual(record.record_id, "sample-016")
        self.assertGreaterEqual(record.revision, 2)

    def test_all_materials_balance_with_destination_evidence(self):
        evidence = bundle().disposition_evidence()
        material_ids = set(evidence)
        self.assertEqual(
            material_ids,
            {"sam-remnant", "sam-fruiting", "ali-A-morph", "ali-B-chem", "ali-C-mol", "ali-D-screen"},
        )
        for mid, info in evidence.items():
            self.assertTrue(info["balance_ok"], f"{mid} 数量未对平：{info}")
            self.assertEqual(info["remaining"], 0)
            self.assertTrue(info["destruction_certificates"], f"{mid} 缺少销毁凭证")

    def test_lab_view_is_minimized(self):
        view = bundle().lab_clinical_view()
        leaked = {"identity", "contact", "address", "fields_withheld", "granted_to"}
        for row in view:
            self.assertTrue(row["minimum_necessary"])
            self.assertFalse(leaked & set(row), f"实验室视图泄漏受限字段：{row}")

    def test_public_advisory_is_safe(self):
        advisories = bundle().public_advisories_safe()
        self.assertEqual(len(advisories), 1)
        text = advisories[0]["text"]
        for pid in bundle().persons():
            self.assertNotIn(pid, text)

    def test_escalation_traces_results_state_and_acks(self):
        trace = bundle().trace_escalation("esc-001")
        self.assertEqual(trace["adopted_results"][0]["test_run_id"], "tst-screen-01")
        states = {s["material_ref"]: s for s in trace["sample_states"]}
        self.assertEqual(states["sam-remnant"]["rebuilt_state"], "in_lab_custody")
        self.assertEqual(states["sam-fruiting"]["rebuilt_state"], "not_yet_collected")
        self.assertEqual(trace["receipt_acks"][0]["ack_id"], "ack-rem-01")


class SourceRetentionTest(unittest.TestCase):
    def test_duplicate_registrations_cannot_be_silently_dropped(self):
        def drop(raw):
            raw["registration_aliases"][0]["registrations"].pop()
        expect_violation(self, mutated(drop), "原始登记")

    def test_pending_group_must_link_event(self):
        def unlink(raw):
            raw["registration_aliases"][0]["linked_event_id"] = "evt-other"
        expect_violation(self, mutated(unlink), "回链事件")

    def test_offline_scan_cannot_self_archive_while_pending(self):
        def sync(raw):
            raw["offline_scans"][0]["synced_at"] = "2026-09-21T09:00:00+08:00"
        expect_violation(self, mutated(sync), "待核实")

    def test_offline_scan_must_hang_on_alias_group(self):
        def detach(raw):
            raw["offline_scans"][0]["alias_group_id"] = "agl-ghost"
        expect_violation(self, mutated(detach), "待核实合并组")


class QuantityLedgerTest(unittest.TestCase):
    def test_destroy_qty_tampering_unbalances(self):
        def tamper(raw):
            raw["disposition_ledger"]["sam-remnant"][-1]["qty_value"] = 10
        expect_violation(self, mutated(tamper), "数量不平")

    def test_consumed_qty_must_match_test_record(self):
        def mismatch(raw):
            raw["disposition_ledger"]["ali-B-chem"][0]["qty_value"] = 30
        expect_violation(self, mutated(mismatch), "数量不平")

    def test_destroy_requires_certificate(self):
        def no_cert(raw):
            raw["disposition_ledger"]["ali-C-mol"][-1].pop("certificate_id")
        expect_violation(self, mutated(no_cert), "销毁凭证")

    def test_destroy_before_retention_end_rejected(self):
        def early(raw):
            raw["disposition_ledger"]["sam-remnant"][-1]["at"] = "2026-10-01T10:00:00+08:00"
        expect_violation(self, mutated(early), "法定留样期")

    def test_unit_mixing_rejected(self):
        def mix(raw):
            raw["disposition_ledger"]["sam-remnant"][-1]["qty_unit"] = "g"
        expect_violation(self, mutated(mix), "计量单位混用")

    def test_return_must_have_matching_parent_entry(self):
        def orphan(raw):
            raw["disposition_ledger"]["sam-remnant"][3]["qty_value"] = 6
        expect_violation(self, mutated(orphan), "配对入账")


class FindingsAndReportsTest(unittest.TestCase):
    def test_contaminated_run_cannot_keep_verdict(self):
        def keep(raw):
            raw["findings"][4]["conclusion"] = "positive"
            raw["findings"][4]["confidence"] = "high"
        expect_violation(self, mutated(keep), "withdrawn_contamination")

    def test_two_current_reports_is_overwrite_pattern(self):
        def two_current(raw):
            raw["reports"][0]["status"] = "current"
        expect_violation(self, mutated(two_current), "current")

    def test_superseded_chain_must_stay_intact(self):
        def cut(raw):
            raw["reports"][0].pop("superseded_by")
        expect_violation(self, mutated(cut), "superseded_by")

    def test_current_report_cannot_adopt_invalid_run(self):
        def adopt(raw):
            raw["reports"][1]["finding_ids"].append("fnd-pcr-02")
        expect_violation(self, mutated(adopt), "无效/污染")

    def test_reevaluation_only_covers_people_still_under_observation(self):
        def release(raw):
            raw["observation_periods"][1]["ended_at"] = "2026-09-22T08:00:00+08:00"
        expect_violation(self, mutated(release), "仍在观察")

    def test_reevaluation_must_not_diagnose(self):
        def diagnose(raw):
            raw["re_evaluations"][0]["changes_diagnosis"] = True
        expect_violation(self, mutated(diagnose), "医生诊断")

    def test_new_version_reevaluates_rather_than_editing_old(self):
        def edit_old(raw):
            raw["reports"][0]["finding_ids"] = ["fnd-morph", "fnd-tox", "fnd-pcr-03"]
            raw["reports"][0]["status"] = "current"
            raw["reports"][1]["status"] = "superseded"
        expect_violation(self, mutated(edit_old), "current")

    def test_new_negative_method_kept_as_low_confidence_not_override(self):
        # tst-pcr-01/fnd-pcr-01 为阴性低置信：仍保留在数据中，只是不进入 current 报告
        findings = {f["finding_id"]: f for f in bundle().raw["findings"]}
        self.assertEqual(findings["fnd-pcr-01"]["conclusion"], "negative")
        self.assertEqual(findings["fnd-pcr-01"]["confidence"], "low")
        self.assertIn("fnd-pcr-01", bundle().raw["reports"][1]["withdrawn_finding_ids"])


class EscalationTraceTest(unittest.TestCase):
    def test_snapshot_state_must_match_rebuilt_chain(self):
        def lie(raw):
            raw["escalations"][0]["sample_state_snapshot"][0]["state"] = "in_transit"
        expect_violation(self, mutated(lie), "重建应为")

    def test_snapshot_seal_must_match_receipt_record(self):
        def broken_seal(raw):
            raw["escalations"][0]["sample_state_snapshot"][0]["seal_intact"] = False
        expect_violation(self, mutated(broken_seal), "封签状态与接收记录不符")

    def test_cannot_adopt_future_result(self):
        def future(raw):
            raw["test_runs"][0]["started_at"] = "2026-09-21T22:00:00+08:00"
        expect_violation(self, mutated(future), "尚未开始")

    def test_in_lab_sample_requires_receipt_ack(self):
        def no_ack(raw):
            raw["escalations"][0]["receipt_ack_refs"] = []
        expect_violation(self, mutated(no_ack), "接收确认")


class PrivacyTest(unittest.TestCase):
    def test_exact_coordinates_forbidden(self):
        def pinpoint(raw):
            raw["collection_sites"][0]["exact_coordinates"] = [25.123, 102.456]
        expect_violation(self, mutated(pinpoint), "精确坐标")

    def test_clinical_summary_must_withhold_identity(self):
        def overshare(raw):
            raw["clinical_summaries"][0]["fields_withheld"].remove("identity")
        expect_violation(self, mutated(overshare), "屏蔽")

    def test_summary_only_granted_to_lab(self):
        def grant(raw):
            raw["clinical_summaries"][0]["granted_to"].append("公众")
        expect_violation(self, mutated(grant), "授予")

    def test_public_advisory_must_redact_person_data(self):
        def leak(raw):
            raw["public_advisories"][0]["redactions"].remove("person_ids")
        expect_violation(self, mutated(leak), "脱敏项")

    def test_public_advisory_cannot_carry_site(self):
        def site(raw):
            raw["public_advisories"][0]["site_ref"] = "site-pickup-01"
        expect_violation(self, mutated(site), "采集点引用")


class ReferentialAndEnvelopeTest(unittest.TestCase):
    def test_seal_mismatch_on_transfer_rejected(self):
        def reseal(raw):
            raw["transfers"][0]["seal_ids"] = ["seal-fru-01"]
        expect_violation(self, mutated(reseal), "封签")

    def test_ack_time_must_match_receipt(self):
        def drift(raw):
            raw["transfers"][0]["receipt_ack"]["acknowledged_at"] = "2026-09-20T22:00:00+08:00"
        expect_violation(self, mutated(drift), "签收时间不符")

    def test_event_time_anchor_preserved(self):
        def shift(raw):
            raw["occurred_at"] = "2026-09-19T09:00:00+08:00"
        expect_violation(self, mutated(shift), "事件锚点")

    def test_unknown_material_in_ledger_rejected(self):
        def ghost(raw):
            raw["disposition_ledger"]["sam-ghost"] = []
        expect_violation(self, mutated(ghost), "未知材料")

    def test_bundle_is_plain_json_roundtrippable(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(copy.deepcopy(payload), payload)


if __name__ == "__main__":
    unittest.main()
