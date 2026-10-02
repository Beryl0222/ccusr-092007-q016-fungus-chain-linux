"""事件链场景测试：以业务断点为单位，正向样例必须通过、违规变体必须被抓住。"""

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fungus_chain import (  # noqa: E402
    ChainValidationError,
    load_case,
    load_record,
    validate_case,
    validate_payload,
)

FIXTURE = ROOT / "fixtures" / "sample_transfer.json"


def base_payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def by_id(payload, entity_id):
    return next(e for e in payload["entities"] if e["id"] == entity_id)


class ChainTest(unittest.TestCase):
    def assertRejects(self, mutate, *keywords):
        payload = copy.deepcopy(base_payload())
        mutate(payload)
        problems = validate_payload(payload)
        self.assertTrue(problems, "变异数据应当被拒绝，但校验通过了")
        joined = "\n".join(problems)
        for word in keywords:
            self.assertIn(word, joined)

    # ---- 正向 ----------------------------------------------------------------
    def test_fixture_is_a_valid_chain(self):
        case = validate_case(FIXTURE)
        self.assertEqual(len(case.entities), 39)
        self.assertTrue(case.by_type("report"))

    def test_envelope_contract_still_reads_revision_2(self):
        record = load_record(FIXTURE)
        self.assertEqual(record.domain, "fungus_chain")
        self.assertGreaterEqual(record.revision, 2)

    def test_model_indexes_entities(self):
        case = load_case(FIXTURE)
        self.assertEqual(case.get("smp-soup-ER").type, "sample")
        self.assertIsNone(case.find("does-not-exist"))

    # ---- 两个编号登记同一次误食 ----------------------------------------------
    def test_two_accessions_pending_merge_keeps_every_source(self):
        soup = by_id(base_payload(), "smp-soup-ER")
        dup = soup["duplicate_record"]
        self.assertEqual(dup["status"], "pending_merge")
        numbers = {r["accession_no"] for r in soup["registrations"]}
        self.assertTrue(numbers.issubset(set(dup["survive_as"])))

    def test_dropping_either_registration_is_rejected(self):
        def drop(d):
            by_id(d, "smp-soup-ER")["duplicate_record"]["survive_as"] = [
                "ER-LAB-260920-007"
            ]

        self.assertRejects(drop, "survive_as")

    def test_offline_scan_must_await_verification(self):
        self.assertRejects(
            lambda d: by_id(d, "smp-soup-ER")["offline_scans"][0].update(
                accepted=True
            ),
            "未核实",
        )

    # ---- 数量相符 ------------------------------------------------------------
    def test_split_conservation(self):
        payload = base_payload()
        by_id(payload, "ali-soup-A")["quantity"]["value"] = 99
        problems = validate_payload(payload)
        self.assertTrue(any("分装不守恒" in p for p in problems))

    def test_every_aliquot_has_a_destination(self):
        def delete_plan(d):
            d["entities"].remove(by_id(d, "disp-soup-retain"))

        # 删掉留样到期计划后，ali-soup-retain 有结余却无去向
        self.assertRejects(delete_plan, "去向不符")

    def test_overconsumption_makes_negative_ledger(self):
        def overuse(d):
            by_id(d, "cons-v1")["quantity"]["value"] = 45

        self.assertRejects(overuse, "台账为负")

    def test_executed_disposal_after_hold_balances_to_zero(self):
        # 合法终态：留样期满后凭联单销毁，台账归零
        payload = copy.deepcopy(base_payload())
        plan = by_id(payload, "disp-soup-retain")
        plan["status"] = "executed"
        plan["executed_at"] = "2028-09-20T10:00:00+08:00"
        plan["manifest_ref"] = "MANIFEST-2028-09-013"
        self.assertEqual(validate_payload(payload), [])

    # ---- 多方法多置信度 ------------------------------------------------------
    def test_morphology_chemical_molecular_coexist(self):
        payload = base_payload()
        methods = {
            e["method"]
            for e in payload["entities"]
            if e["type"] == "test_result"
        }
        self.assertEqual(methods, {"morphology", "chemical", "molecular"})
        confidences = {
            e["confidence"]
            for e in payload["entities"]
            if e["type"] == "test_result"
        }
        self.assertEqual(confidences, {"low", "medium", "high"})

    def test_negative_screening_cannot_end_observation(self):
        def release(d):
            meal = by_id(d, "meal-2026-0919-01")
            meal["observation"]["early_termination_blocked"] = False
            meal["observation"]["holds"][0]["superseded"] = True

        self.assertRejects(release, "提前结束观察")

    # ---- 新版本，不覆盖、不替医生诊断 ----------------------------------------
    def test_report_versions_chain_and_old_version_is_kept(self):
        payload = base_payload()
        v1 = by_id(payload, "rpt-0919-01")
        v2 = by_id(payload, "rpt-0919-02")
        self.assertEqual(v1["status"], "superseded")
        self.assertEqual(v1["superseded_by"], "rpt-0919-02")
        self.assertEqual(v2["supersedes"], "rpt-0919-01")
        # 旧版本实体仍在链上
        self.assertIs(by_id(payload, "rpt-0919-01") is not None, True)

    def test_deleting_old_result_is_rejected(self):
        self.assertRejects(
            lambda d: d["entities"].remove(by_id(d, "res-r3-chem")),
            "不得删除",
        )

    def test_report_without_diagnostic_boundary_rejected(self):
        def erase(d):
            by_id(d, "rpt-0919-01")["diagnostic_boundary"] = ""

        self.assertRejects(erase, "不替代医生诊断")

    def test_reassessment_targets_only_people_still_under_observation(self):
        re = by_id(base_payload(), "reassess-obs-v2")
        self.assertIn("ppl-neighbor", re["still_under_observation"])

    def test_reassessment_must_follow_a_new_version(self):
        self.assertRejects(
            lambda d: by_id(d, "reassess-obs-v2").update(
                triggered_by_report_ref="rpt-0919-01"
            ),
            "新版本",
        )

    # ---- 升级追溯 ------------------------------------------------------------
    def test_escalation_traces_report_state_and_receipt(self):
        esc = by_id(base_payload(), "esc-01")["trace"]
        self.assertEqual(esc["report_ref"], "rpt-0919-01")
        self.assertEqual(esc["report_version_at_use"], 1)
        self.assertTrue(esc["sample_states_at_use"])
        self.assertEqual(
            set(esc["receipt_refs"]), {"tfr-ER-to-CDC", "tfr-CDC-to-LAB"}
        )

    def test_unconfirmed_receipt_cannot_back_an_escalation(self):
        self.assertRejects(
            lambda d: by_id(d, "tfr-ER-to-CDC").update(receipt_confirmed=False),
            "接收确认",
        )

    def test_broken_seal_is_rejected(self):
        self.assertRejects(
            lambda d: by_id(d, "tfr-CDC-to-LAB").update(
                seal_intact_at_receive=False
            ),
            "封签",
        )

    # ---- 留样与去向 ----------------------------------------------------------
    def test_disposal_before_legal_hold_is_rejected(self):
        self.assertRejects(
            lambda d: by_id(d, "disp-soup-retain").update(
                planned_after="2027-01-01"
            ),
            "法定留样期",
        )

    def test_every_retained_aliquot_has_policy_and_final_destination(self):
        payload = base_payload()
        retained = {
            a["id"]
            for a in payload["entities"]
            if a["type"] == "aliquot" and a.get("retained")
        }
        planned = {
            d["aliquot_ref"]
            for d in payload["entities"]
            if d["type"] == "disposal"
        }
        self.assertTrue(retained.issubset(planned))

    # ---- 隐私 ----------------------------------------------------------------
    def test_people_carry_only_pseudonyms(self):
        def leak(d):
            by_id(d, "ppl-index")["id_number"] = "110101..."

        self.assertRejects(leak, "敏感字段")

    def test_clinical_summary_is_minimal(self):
        summary = by_id(base_payload(), "evt-2026-0919-01")[
            "clinical_summary_min"
        ]
        self.assertEqual(summary["audience"], "laboratory")
        self.assertIn("phone", summary["fields_excluded"])

    def test_public_notice_hides_exact_collection_point(self):
        site = by_id(base_payload(), "site-watershed-7")
        self.assertEqual(site["precision"], "blurred")
        self.assertTrue(site["public_release"]["hide_exact_point"])

        def expose(d):
            by_id(d, "site-watershed-7")["public_release"][
                "hide_exact_point"
            ] = False

        self.assertRejects(expose, "精确采集点")


class ErrorTypeTest(unittest.TestCase):
    def test_validate_case_raises_aggregated_error(self):
        payload = copy.deepcopy(base_payload())
        by_id(payload, "ali-soup-B")["quantity"]["value"] = 0
        with self.assertRaises(ChainValidationError) as ctx:
            import tempfile

            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
                json.dump(payload, fh)
                tmp = fh.name
            validate_case(tmp)
        self.assertTrue(ctx.exception.problems)


if __name__ == "__main__":
    unittest.main()
