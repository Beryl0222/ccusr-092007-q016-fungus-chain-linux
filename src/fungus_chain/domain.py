"""样本链条领域模型与不变量校验（schema v2）。

设计原则：
- 以样本链条为主线：暴露事件 → 同餐关系 → 采集/部位/保存/封签 → 转交接收
  → 分装 → 检测耗用 → 退回 → 留样到期销毁，全部通过关联标识串联。
- 任何来源都不丢弃：重复登记号进入待核实地名字典组，离线扫码排队等待合并，
  不生成独立档案，也不删除来源。
- 数量必须对平：分装、耗用、退回、销毁逐笔可算，销毁须有凭证且不早于留样期。
- 结论多置信并存：形态/化学/分子方法可各自给出不同置信度；污染试验作废。
- 报告只增不改：复核或发现污染时发新版本重评仍在观察人员，旧版保留，
  实验室结论不替代医师诊断。
- 权限分层：实验室只见最小必要临床摘要；公开提示隐藏个人与敏感采集点。
- 升级可追溯：一次升级能追到采用的结果、当时样本状态与接收确认。
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ENVELOPE_KEYS = ("schema_version", "record_id", "domain", "occurred_at", "revision", "source")
SUPPORTED_SCHEMA_VERSIONS = (2,)

# 数量流向：返回为流入，其余出库/销毁为流出；voucher 仅状态标注、不改变数量。
INFLOW_ACTIONS = {"returned_from_aliquot"}
OUTFLOW_ACTIONS = {
    "aliquot_prepared",
    "consumed_by_test",
    "returned_to_parent",
    "residue_destroyed",
    "destroyed_after_retention",
    "container_destroyed_empty",
}
STATE_ONLY_ACTIONS = {"retained_as_voucher"}
DESTRUCTION_ACTIONS = {
    "residue_destroyed",
    "destroyed_after_retention",
    "container_destroyed_empty",
}
REQUIRED_PUBLIC_REDACTIONS = {
    "person_ids",
    "names",
    "contacts",
    "exact_addresses",
    "precise_collection_sites",
}
LAB_PARTIES = {"联检实验室"}
MINIMIZATION_REQUIRED_WITHHELD = {"identity", "contact", "address"}


class ChainIntegrityError(ValueError):
    """样本链条存在一条或多条被违反的不变量。"""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("；".join(violations))


def _parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


@dataclass
class QuantityAccount:
    material_id: str
    unit: str
    initial: float
    inflow: float
    outflow: float
    remaining: float
    terminal_evidence: str


class ChainBundle:
    """一次误食事件样本链条的结构化载体与校验器。"""

    def __init__(self, payload: dict[str, Any]):
        self.raw = payload
        self._violations: list[str] = []

    # ---- 基础索引 -------------------------------------------------------

    @property
    def event(self) -> dict[str, Any]:
        return self.raw.get("event", {})

    @property
    def samples(self) -> list[dict[str, Any]]:
        return self.raw.get("samples", [])

    @property
    def aliquots(self) -> list[dict[str, Any]]:
        return self.raw.get("aliquots", [])

    @property
    def test_runs(self) -> list[dict[str, Any]]:
        return self.raw.get("test_runs", [])

    def _by_id(self, key: str, id_attr: str = "id") -> dict[str, dict[str, Any]]:
        return {item[id_attr]: item for item in self.raw.get(key, [])}

    def persons(self) -> dict[str, dict[str, Any]]:
        return self._by_id("persons", "person_id")

    def test_index(self) -> dict[str, dict[str, Any]]:
        return {t["test_run_id"]: t for t in self.test_runs}

    def finding_index(self) -> dict[str, dict[str, Any]]:
        return {f["finding_id"]: f for f in self.raw.get("findings", [])}

    def report_index(self) -> dict[str, dict[str, Any]]:
        return {r["report_id"]: r for r in self.raw.get("reports", [])}

    def ack_index(self) -> dict[str, dict[str, Any]]:
        acks: dict[str, dict[str, Any]] = {}
        for trf in self.raw.get("transfers", []):
            ack = trf.get("receipt_ack")
            if ack:
                acks[ack["ack_id"]] = {**ack, "transfer_id": trf["transfer_id"],
                                       "material_ref": trf["material_ref"]}
        return acks

    def registration_index(self) -> dict[str, dict[str, Any]]:
        regs: dict[str, dict[str, Any]] = {}
        for group in self.raw.get("registration_aliases", []):
            for reg in group.get("registrations", []):
                regs[reg["registration_id"]] = {**reg, "alias_group_id": group["alias_group_id"],
                                                "group_status": group["status"]}
        return regs

    # ---- 校验入口 -------------------------------------------------------

    def validate(self) -> list[str]:
        """返回全部被违反的不变量；空列表表示链条完整。"""
        self._violations = []
        self._check_envelope()
        self._check_referential_integrity()
        self._check_duplicate_sources_retained()
        self._check_fuzzy_site()
        self._check_seals_transfers()
        self._check_findings()
        self._check_quantity_ledger()
        self._check_reports_and_reevaluation()
        self._check_escalations()
        self._check_clinical_minimization()
        self._check_public_advisories()
        return list(self._violations)

    def check(self) -> None:
        violations = self.validate()
        if violations:
            raise ChainIntegrityError(violations)

    def _err(self, msg: str) -> None:
        self._violations.append(msg)

    # ---- 1. 信封与迁移语义 ----------------------------------------------

    def _check_envelope(self) -> None:
        raw = self.raw
        if raw.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
            self._err(f"schema_version 必须为 {SUPPORTED_SCHEMA_VERSIONS}")
        if raw.get("domain") != "fungus_chain":
            self._err("domain 必须保持为 fungus_chain")
        if not isinstance(raw.get("revision"), int) or raw["revision"] < 2:
            self._err("v2 束的 revision 必须 >= 2")
        event = self.event
        if event.get("occurred_at") != raw.get("occurred_at"):
            self._err("顶层 occurred_at 语义不变，必须与 event.occurred_at 一致（事件锚点时间）")
        # record_id 沿用既有样例编号（sample-016），不强制与 event_id 相同。
        migration = raw.get("migration")
        if not migration or migration.get("from_schema_version") != 1:
            self._err("v2 必须记录从 v1 的迁移说明，且 from_schema_version=1")

    # ---- 2. 关联标识引用完整性 ------------------------------------------

    def _check_referential_integrity(self) -> None:
        raw = self.raw
        person_ids = set(self.persons())
        meal = raw.get("shared_meal", {})
        if meal.get("event_id") != self.event.get("event_id"):
            self._err("shared_meal 必须回链 event")
        for diner in meal.get("diner_person_ids", []):
            if diner not in person_ids:
                self._err(f"同餐人员 {diner} 不在 persons 中")

        obs_persons = set()
        for obs in raw.get("observation_periods", []):
            if obs["person_id"] not in person_ids:
                self._err(f"观察记录引用了未知人员 {obs['person_id']}")
            obs_persons.add(obs["person_id"])
        for pid in person_ids:
            if pid not in obs_persons:
                self._err(f"人员 {pid} 缺少观察期记录")

        sample_ids = {s["sample_id"] for s in self.samples}
        site_ids = {s["site_id"] for s in raw.get("collection_sites", [])}
        regs = self.registration_index()
        for s in self.samples:
            if s.get("event_id") != self.event.get("event_id"):
                self._err(f"样本 {s['sample_id']} 未回链事件")
            if s.get("site_ref") is not None and s["site_ref"] not in site_ids:
                self._err(f"样本 {s['sample_id']} 引用未知采集地 {s['site_ref']}")
            for rid in s.get("registration_ids", []):
                if rid not in regs:
                    self._err(f"样本 {s['sample_id']} 引用未登记编号 {rid}")

        aliquot_ids = set()
        for a in self.aliquots:
            aliquot_ids.add(a["aliquot_id"])
            if a["parent_sample_id"] not in sample_ids:
                self._err(f"分装 {a['aliquot_id']} 的母体样本不存在")

        materials = sample_ids | aliquot_ids
        for t in self.test_runs:
            if t["material_ref"] not in materials:
                self._err(f"检测 {t['test_run_id']} 耗用了未知材料 {t['material_ref']}")
        for f in raw.get("findings", []):
            if f["test_run_id"] not in self.test_index():
                self._err(f"结论 {f['finding_id']} 引用未知检测 {f['test_run_id']}")
        for trf in raw.get("transfers", []):
            if trf["material_ref"] not in sample_ids:
                self._err(f"转交 {trf['transfer_id']} 引用未知样本")

    # ---- 3. 重复登记与离线扫码：只挂起、不丢源 --------------------------

    def _check_duplicate_sources_retained(self) -> None:
        groups = self.raw.get("registration_aliases", [])
        for group in groups:
            if group.get("status") == "pending_verification":
                if len(group.get("registrations", [])) < 2:
                    self._err(f"待核实合并组 {group.get('alias_group_id')} 必须保留 >=2 个原始登记")
                if group.get("linked_event_id") != self.event.get("event_id"):
                    self._err(f"待合并组 {group.get('alias_group_id')} 必须回链事件")
        valid_groups = {g["alias_group_id"] for g in groups}
        for scan in self.raw.get("offline_scans", []):
            if scan.get("sync_status") == "queued_pending_verification":
                if scan.get("synced_at") is not None:
                    self._err(f"离线扫码 {scan['scan_id']} 仍待核实时不得标记已同步")
                if scan.get("alias_group_id") not in valid_groups:
                    self._err(f"离线扫码 {scan['scan_id']} 必须挂到待核实合并组，禁止自行建档")
            if scan.get("material_ref") not in {s["sample_id"] for s in self.samples}:
                self._err(f"离线扫码 {scan['scan_id']} 引用未知样本")

    # ---- 4. 模糊采集点 ---------------------------------------------------

    def _check_fuzzy_site(self) -> None:
        for site in self.raw.get("collection_sites", []):
            if not site.get("sensitive"):
                self._err(f"采集地 {site.get('site_id')} 必须标记 sensitive")
            if site.get("exact_coordinates") is not None:
                self._err(f"采集地 {site.get('site_id')} 不得保存精确坐标")
            if not site.get("descriptor_fuzzy") or site.get("geo_precision") == "exact":
                self._err(f"采集地 {site.get('site_id')} 必须使用模糊化位置描述")

    # ---- 5. 封签与转交接收 ----------------------------------------------

    def _check_seals_transfers(self) -> None:
        sample_seals = {s["sample_id"]: s.get("seal_id") for s in self.samples}
        for trf in self.raw.get("transfers", []):
            expected = sample_seals.get(trf["material_ref"])
            for sid in trf.get("seal_ids", []):
                if sid != expected:
                    self._err(f"转交 {trf['transfer_id']} 封签 {sid} 与样本封签不一致")
            ack = trf.get("receipt_ack")
            if not ack:
                self._err(f"转交 {trf['transfer_id']} 缺少接收确认")
                continue
            if ack.get("acknowledged_at") != trf.get("received_at"):
                self._err(f"转交 {trf['transfer_id']} 接收确认时间与签收时间不符")
            if _parse_ts(ack["acknowledged_at"]) < _parse_ts(trf["handed_over_at"]):
                self._err(f"转交 {trf['transfer_id']} 签收早于交出")

    # ---- 6. 结论：方法可异置信、污染作废 --------------------------------

    def _check_findings(self) -> None:
        tests = self.test_index()
        for f in self.raw.get("findings", []):
            t = tests[f["test_run_id"]]
            if not t.get("valid"):
                if f.get("confidence") != "none" or f.get("conclusion") != "withdrawn_contamination":
                    self._err(
                        f"无效/污染检测 {t['test_run_id']} 的结论必须为 withdrawn_contamination/none，"
                        "不得保留阳性或阴性判定"
                    )

    # ---- 7. 分装/耗用/退回/销毁台账：数量对平 + 去向凭证 ----------------

    def _aliquot_initial(self, aliquot: dict[str, Any]) -> tuple[float, str | None]:
        """分装的初始数量只能来自母体台账上唯一一笔 aliquot_prepared。"""
        ledger = self.raw.get("disposition_ledger", {})
        sources = [
            e for entries in ledger.values() for e in entries
            if e["action"] == "aliquot_prepared" and e.get("ref") == aliquot["aliquot_id"]
        ]
        if len(sources) != 1:
            self._err(f"分装 {aliquot['aliquot_id']} 必须由母体台账上唯一一笔分装记录创建，实际 {len(sources)} 笔")
            return 0.0, None
        entry = sources[0]
        if entry["qty_unit"] != aliquot["qty_unit"] or entry["qty_value"] != aliquot["qty_value"]:
            self._err(f"分装 {aliquot['aliquot_id']} 创建笔数量与分装登记不一致")
        return float(entry["qty_value"]), entry["qty_unit"]

    def quantity_accounts(self) -> dict[str, QuantityAccount]:
        samples = {s["sample_id"]: s for s in self.samples}
        aliquots = {a["aliquot_id"]: a for a in self.aliquots}
        ledger = self.raw.get("disposition_ledger", {})
        accounts: dict[str, QuantityAccount] = {}

        initials: dict[str, tuple[float, str]] = {}
        for sid, s in samples.items():
            initials[sid] = (float(s["collected_qty_value"]), s["collected_qty_unit"])
        for aid, a in aliquots.items():
            value, unit = self._aliquot_initial(a)
            initials[aid] = (value, unit or a["qty_unit"])

        tests = self.test_index()
        for material_id, (initial, unit) in initials.items():
            inflow = outflow = 0.0
            evidence = "无去向凭证"
            cert_seen = False
            pass_through = 0.0  # 经有 ref 的可追踪动作流出的数量
            for e in ledger.get(material_id, []):
                action, qty, eunit = e["action"], float(e["qty_value"]), e["qty_unit"]
                if eunit != unit:
                    self._err(f"{material_id} 台账出现计量单位混用：{eunit} vs {unit}")
                if action in INFLOW_ACTIONS:
                    inflow += qty
                elif action in OUTFLOW_ACTIONS:
                    outflow += qty
                    if action in DESTRUCTION_ACTIONS:
                        if not e.get("certificate_id"):
                            self._err(f"{material_id} 销毁记录 {e.get('entry_id')} 缺少销毁凭证编号")
                        cert_seen = True
                        if action == "destroyed_after_retention":
                            retention_until = e.get("retention_until")
                            if not retention_until:
                                self._err(f"{material_id} 留样到期销毁缺少 retention_until")
                            elif _parse_ts(e["at"]).date() < _parse_ts(retention_until).date():
                                self._err(f"{material_id} 销毁时间 {e['at']} 早于法定留样期末 {retention_until}")
                    if action == "consumed_by_test":
                        t = tests.get(e.get("ref", ""))
                        if not t or t["material_ref"] != material_id:
                            self._err(f"{material_id} 耗用笔 {e.get('entry_id')} 的检测引用不匹配")
                        elif (t["consumed_qty_value"], t["consumed_qty_unit"]) != (e["qty_value"], e["qty_unit"]):
                            self._err(f"{material_id} 耗用笔数量与检测登记不一致")
                    if action == "aliquot_prepared" and e.get("ref") not in aliquots:
                        self._err(f"{material_id} 分装笔引用未知分装 {e.get('ref')}")
                    if e.get("ref") is not None or action in DESTRUCTION_ACTIONS:
                        pass_through += qty
                elif action in STATE_ONLY_ACTIONS:
                    pass
                else:
                    self._err(f"{material_id} 台账出现未知动作 {action}")

            # 退回必须父子成对、数量相等。
            for e in ledger.get(material_id, []):
                if e["action"] == "returned_to_parent":
                    parent = aliquots[material_id]["parent_sample_id"]
                    twins = [x for x in ledger.get(parent, [])
                             if x["action"] == "returned_from_aliquot"
                             and x.get("ref") == material_id]
                    if len(twins) != 1 or twins[0]["qty_value"] != e["qty_value"]:
                        self._err(f"{material_id} 退回母体 {parent} 缺少数量一致的配对入账")
                if e["action"] == "returned_from_aliquot" and e.get("ref") not in aliquots:
                    self._err(f"{material_id} 退回入账引用未知分装 {e.get('ref')}")

            remaining = initial + inflow - outflow
            if cert_seen:
                evidence = "销毁凭证销毁（certificate_id 见台账）"
            elif remaining == 0 and pass_through >= outflow:
                evidence = "全量经检测耗用/分装/退回母体逐笔流转（见 ref）"
            if remaining != 0:
                self._err(
                    f"{material_id} 数量不平：初始 {initial}{unit} + 流入 {inflow} - 流出 {outflow} "
                    f"= 余 {remaining}（须为 0，留样到期也须登记去向）"
                )
            accounts[material_id] = QuantityAccount(material_id, unit, initial, inflow, outflow,
                                                    remaining, evidence if remaining == 0 else "去向不明")
        return accounts

    def _check_quantity_ledger(self) -> None:
        ledger = self.raw.get("disposition_ledger", {})
        known = {s["sample_id"] for s in self.samples} | {a["aliquot_id"] for a in self.aliquots}
        for material_id in ledger:
            if material_id not in known:
                self._err(f"台账包含未知材料 {material_id}")
        self.quantity_accounts()

    # ---- 8. 报告版本链 + 重评 --------------------------------------------

    def _check_reports_and_reevaluation(self) -> None:
        reports = self.raw.get("reports", [])
        by_id = self.report_index()
        per_event: dict[str, list[dict[str, Any]]] = {}
        for r in reports:
            per_event.setdefault(r["event_id"], []).append(r)
            if r["event_id"] != self.event.get("event_id"):
                self._err(f"报告 {r['report_id']} 未回链事件")
            for fid in r.get("finding_ids", []):
                if fid not in self.finding_index():
                    self._err(f"报告 {r['report_id']} 引用未知结论 {fid}")

        for event_id, rs in per_event.items():
            versions = [r["version"] for r in rs]
            if len(versions) != len(set(versions)):
                self._err(f"事件 {event_id} 报告版本号重复")
            currents = [r for r in rs if r["status"] == "current"]
            if len(currents) != 1:
                self._err(f"事件 {event_id} 必须恰好有一份 current 报告，实际 {len(currents)}")
            current = currents[0]
            if current["version"] != max(versions):
                self._err("current 报告必须是最高版本；旧报告只能标记 superseded，不得覆盖")
            for r in rs:
                if r["status"] == "superseded":
                    nxt = by_id.get(r.get("superseded_by", ""))
                    if not nxt or nxt.get("supersedes") != r["report_id"]:
                        self._err(f"旧报告 {r['report_id']} 的 superseded_by 链接不完整（历史必须可追）")
            for fid in current.get("finding_ids", []):
                t = self.test_index()[self.finding_index()[fid]["test_run_id"]]
                if not t.get("valid"):
                    self._err(f"current 报告 {current['report_id']} 不得采信无效/污染检测结果 {t['test_run_id']}")
            for fid in current.get("withdrawn_finding_ids", []):
                f = self.finding_index().get(fid)
                if not f:
                    self._err(f"current 报告撤回了未知结论 {fid}")
                    continue
                t = self.test_index()[f["test_run_id"]]
                if t.get("result") in ("positive", "positive_match") and t.get("valid"):
                    self._err(f"无替代证据时不得在新版中撤回有效阳性结论 {fid}")

        open_observations = {
            o["person_id"] for o in self.raw.get("observation_periods", []) if o.get("ended_at") is None
        }
        for ree in self.raw.get("re_evaluations", []):
            base = by_id.get(ree.get("based_on_report_id", ""))
            if not base or base.get("status") != "current":
                self._err(f"重评 {ree['re_evaluation_id']} 必须基于当时 current 的新版本报告")
            if ree.get("changes_diagnosis") is not False:
                self._err(f"重评 {ree['re_evaluation_id']} 不得替代医生诊断（changes_diagnosis 必须为 false）")
            decided = _parse_ts(ree["decided_at"])
            for pid in ree.get("affected_person_ids", []):
                obs = next((o for o in self.raw.get("observation_periods", [])
                            if o["person_id"] == pid), None)
                if pid not in open_observations:
                    self._err(f"重评 {ree['re_evaluation_id']} 只能影响仍在观察的人员，{pid} 已结束观察")
                elif _parse_ts(obs["started_at"]) > decided:
                    self._err(f"重评 {ree['re_evaluation_id']} 影响了观察尚未开始的人员 {pid}")
            for pid in ree.get("excluded_person_ids", []):
                if pid in open_observations:
                    self._err(f"重评 {ree['re_evaluation_id']} 不得把仍在观察的 {pid} 排除在外")
            if ree.get("trigger") == "contamination_found":
                t = self.test_index().get(ree.get("trigger_test_run_id", ""))
                if not t or t.get("valid") is not False:
                    self._err(f"重评 {ree['re_evaluation_id']} 的污染触发必须指向作废检测")

    # ---- 9. 升级追溯：结果/样本状态/接收确认 ----------------------------

    def _sample_state_at(self, sample_id: str, at: datetime) -> str:
        transfers = sorted(
            (t for t in self.raw.get("transfers", []) if t["material_ref"] == sample_id),
            key=lambda t: t["handed_over_at"],
        )
        sample = next(s for s in self.samples if s["sample_id"] == sample_id)
        if at < _parse_ts(sample["collected_at"]):
            return "not_yet_collected"
        received = [t for t in transfers if _parse_ts(t["received_at"]) <= at]
        handed = [t for t in transfers if _parse_ts(t["handed_over_at"]) <= at]
        if received:
            return "in_lab_custody"
        if handed:
            return "in_transit"
        return "collected_not_transferred"

    def _check_escalations(self) -> None:
        tests = self.test_index()
        acks = self.ack_index()
        sample_ids = {s["sample_id"] for s in self.samples}
        for esc in self.raw.get("escalations", []):
            raised = _parse_ts(esc["raised_at"])
            for ref in esc.get("adopted_result_refs", []):
                t = tests.get(ref)
                if not t:
                    self._err(f"升级 {esc['escalation_id']} 采用了未知结果 {ref}")
                elif _parse_ts(t["started_at"]) > raised:
                    self._err(f"升级 {esc['escalation_id']} 采用了当时尚未开始的检测 {ref}")
            for ack_ref in esc.get("receipt_ack_refs", []):
                ack = acks.get(ack_ref)
                if not ack:
                    self._err(f"升级 {esc['escalation_id']} 引用了未知接收确认 {ack_ref}")
                elif _parse_ts(ack["acknowledged_at"]) > raised:
                    self._err(f"升级 {esc['escalation_id']} 引用了升级后才签收的确认 {ack_ref}")
            snap_ids = set()
            for snap in esc.get("sample_state_snapshot", []):
                mid = snap["material_ref"]
                if mid not in sample_ids:
                    self._err(f"升级 {esc['escalation_id']} 快照引用未知样本 {mid}")
                    continue
                snap_ids.add(mid)
                expected = self._sample_state_at(mid, raised)
                if snap.get("state") != expected:
                    self._err(
                        f"升级 {esc['escalation_id']} 中 {mid} 状态为 {snap.get('state')}，"
                        f"按链条重建应为 {expected}"
                    )
                if expected == "in_lab_custody":
                    if snap.get("seal_intact") is None:
                        self._err(f"升级 {esc['escalation_id']} 中在库样本 {mid} 缺少封签状态")
                    trf = next(t for t in self.raw.get("transfers", [])
                               if t["material_ref"] == mid
                               and _parse_ts(t["received_at"]) <= raised)
                    if snap.get("seal_intact") != trf["seal_intact_on_receipt"]:
                        self._err(f"升级 {esc['escalation_id']} 中 {mid} 封签状态与接收记录不符")
                    if not any(acks[a]["material_ref"] == mid for a in esc.get("receipt_ack_refs", [])):
                        self._err(f"升级 {esc['escalation_id']} 在库样本 {mid} 必须能追到接收确认")
                elif expected == "not_yet_collected" and snap.get("seal_intact") is not None:
                    self._err(f"升级 {esc['escalation_id']} 中 {mid} 尚未采集，封签必须为空")

    # ---- 10. 临床最小必要 ------------------------------------------------

    def lab_clinical_view(self) -> list[dict[str, Any]]:
        """实验室视角：只含最小必要摘要，身份/联系方式/住址等一律剔除。"""
        view = []
        for c in self.raw.get("clinical_summaries", []):
            view.append({
                "person_id": c["person_id"],
                "minimum_necessary": c["minimum_necessary"],
                "purpose": c.get("purpose"),
                "fields_shared": c.get("fields_shared", []),
            })
        return view

    def _check_clinical_minimization(self) -> None:
        person_ids = set(self.persons())
        for c in self.raw.get("clinical_summaries", []):
            if c["person_id"] not in person_ids:
                self._err(f"临床摘要引用未知人员 {c['person_id']}")
            if not c.get("minimum_necessary"):
                self._err("临床摘要必须提供 minimum_necessary 文本")
            withheld = set(c.get("fields_withheld", []))
            missing = MINIMIZATION_REQUIRED_WITHHELD - withheld
            if missing:
                self._err(f"人员 {c['person_id']} 临床摘要必须向实验室屏蔽 {sorted(missing)}")
            granted = set(c.get("granted_to", []))
            if not granted or not granted <= LAB_PARTIES:
                self._err(f"人员 {c['person_id']} 摘要只能授予实验室，实际授予 {sorted(granted)}")

    # ---- 11. 公开提示脱敏 ------------------------------------------------

    def public_advisories_safe(self) -> list[dict[str, Any]]:
        return [{"advisory_id": a["advisory_id"], "issued_at": a["issued_at"], "text": a["text"]}
                for a in self.raw.get("public_advisories", [])]

    def _check_public_advisories(self) -> None:
        for a in self.raw.get("public_advisories", []):
            if a.get("site_ref") is not None:
                self._err(f"公开提示 {a['advisory_id']} 不得携带任何采集点引用")
            missing = REQUIRED_PUBLIC_REDACTIONS - set(a.get("redactions", []))
            if missing:
                self._err(f"公开提示 {a['advisory_id']} 缺少脱敏项 {sorted(missing)}")
            text = a.get("text", "")
            for pid in self.persons():
                if pid in text:
                    self._err(f"公开提示 {a['advisory_id']} 文本中出现个人标识 {pid}")

    # ---- 追溯视图 --------------------------------------------------------

    def trace_escalation(self, escalation_id: str) -> dict[str, Any]:
        """从一次值班升级追到：采用结果、样本当时状态、接收确认。"""
        esc = next(e for e in self.raw.get("escalations", []) if e["escalation_id"] == escalation_id)
        raised = _parse_ts(esc["raised_at"])
        tests = self.test_index()
        acks = self.ack_index()
        return {
            "escalation_id": escalation_id,
            "raised_at": esc["raised_at"],
            "adopted_results": [
                {"test_run_id": r, "result": tests[r]["result"], "started_at": tests[r]["started_at"]}
                for r in esc.get("adopted_result_refs", [])
            ],
            "sample_states": [
                {**snap, "rebuilt_state": self._sample_state_at(snap["material_ref"], raised)}
                for snap in esc.get("sample_state_snapshot", [])
            ],
            "receipt_acks": [acks[r] for r in esc.get("receipt_ack_refs", []) if r in acks],
        }

    def disposition_evidence(self) -> dict[str, dict[str, Any]]:
        """法定留样期结束后，每份材料去了哪里的凭证汇总。"""
        accounts = self.quantity_accounts()
        ledger = self.raw.get("disposition_ledger", {})
        result: dict[str, dict[str, Any]] = {}
        for mid, acct in accounts.items():
            certs = sorted({e["certificate_id"] for e in ledger.get(mid, []) if e.get("certificate_id")})
            result[mid] = {
                "unit": acct.unit,
                "initial": acct.initial,
                "remaining": acct.remaining,
                "balance_ok": acct.remaining == 0,
                "destruction_certificates": certs,
                "terminal_evidence": acct.terminal_evidence,
            }
        return result


def load_bundle(path: str | Path) -> ChainBundle:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return ChainBundle(payload)


def clone_for_mutation(bundle: ChainBundle) -> ChainBundle:
    """供测试或修订使用：深拷贝一份可变束（原始 JSON 与历史版本不被改动）。"""
    return ChainBundle(deepcopy(bundle.raw))
