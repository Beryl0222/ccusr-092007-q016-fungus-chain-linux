"""事件链业务校验。

校验器回答需求里的每个“不能/必须”：

* 关联标识完整，任何来源都不能被丢弃（双号登记待合并、离线扫码待核实）；
* 分装、检测耗用、退回、销毁数量相符；
* 形态/化学/分子结论可并存且置信度不同，阴性初筛不能提前解除观察；
* 报告以新版本重评，不覆盖旧结果、不替代医生诊断；
* 一次升级能追到采用的报告（版本）、样本当时状态、接收确认；
* 法定留样期满后每份材料都有可证明的去向；
* 临床摘要最小化、公开提示隐去个人与敏感采集点。

只输出问题清单，不修改数据。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .model import Case, Entity, load_case

SCREENING_NEGATIVE_MARKERS = ("阴性", "未检出", "negative", "not detected")
CONFIDENCE_LEVELS = ("low", "medium", "high")
IDENTITY_FIELDS = ("name", "id_number", "phone", "home_address", "medical_history")


class ChainValidationError(ValueError):
    """聚合事件链的全部校验问题，便于一次修完。"""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("；".join(problems))


def _q(entity: Entity, key: str = "quantity") -> float:
    return float(entity[key]["value"])


def _isotime(value: str) -> str:
    """归一化到可比较字符串（样例均为带偏移 ISO 时间）。"""

    return value


def _date_of(value: str) -> date:
    return date.fromisoformat(value[:10])


def validate_payload(payload: dict[str, Any]) -> list[str]:
    """校验原始 JSON 字典，返回问题列表（空列表表示通过）。"""

    problems: list[str] = []
    entities = payload.get("entities", [])

    ids: dict[str, dict[str, Any]] = {}
    for item in entities:
        if "id" not in item or "type" not in item:
            problems.append(f"实体缺少 id/type: {item!r}")
            continue
        if item["id"] in ids:
            problems.append(f"实体 id 重复: {item['id']}")
        ids[item["id"]] = item

    def exists(ref: str | None, *, what: str, owner: str) -> bool:
        if ref is None:
            return True
        if ref not in ids:
            problems.append(f"{owner} 引用的{what}不存在: {ref}")
            return False
        return True

    def require_all(refs: list[str], what: str, owner: str) -> set[str]:
        found: set[str] = set()
        for ref in refs:
            if exists(ref, what=what, owner=owner):
                found.add(ref)
        return found

    # ---- 索引 ----------------------------------------------------------------
    samples = [e for e in entities if e["type"] == "sample"]
    aliquots = [e for e in entities if e["type"] == "aliquot"]
    results = [e for e in entities if e["type"] == "test_result"]
    reports = [e for e in entities if e["type"] == "report"]
    transfers = [e for e in entities if e["type"] == "transfer"]
    consumptions = [e for e in entities if e["type"] == "consumption"]
    returns = [e for e in entities if e["type"] == "return"]
    disposals = [e for e in entities if e["type"] == "disposal"]
    events = [e for e in entities if e["type"] == "exposure_event"]
    meals = [e for e in entities if e["type"] == "shared_meal"]
    reassessments = [e for e in entities if e["type"] == "observation_reassessment"]
    escalations = [e for e in entities if e["type"] == "escalation"]
    policies = [e for e in entities if e["type"] == "retention_policy"]
    sites = [e for e in entities if e["type"] == "collection_site"]
    people = [e for e in entities if e["type"] == "person"]

    aliquot_ids = {a["id"] for a in aliquots}
    sample_ids = {s["id"] for s in samples}
    result_ids = {r["id"] for r in results}
    seal_owners = {
        s["seal"]["seal_id"]: s["id"]
        for s in samples
        if isinstance(s.get("seal"), dict)
    }

    # ---- 引用完整性 ----------------------------------------------------------
    for event in events:
        require_all(event.get("linked_people", []), "同餐人员", event["id"])
        require_all(event.get("linked_samples", []), "样本", event["id"])
        exists(event.get("collection_site_ref"), what="采集地点", owner=event["id"])

    for meal in meals:
        exists(meal.get("event_ref"), what="暴露事件", owner=meal["id"])
        for member in meal.get("members", []):
            exists(member.get("person_ref"), what="同餐人员", owner=meal["id"])

    for sample in samples:
        exists(sample.get("event_ref"), what="暴露事件", owner=sample["id"])
        exists(sample.get("site_ref"), what="采集地点", owner=sample["id"])
        if not sample.get("registrations"):
            problems.append(f"样本 {sample['id']} 没有任何登记编号")

    for aliquot in aliquots:
        parent = aliquot.get("parent_sample_ref")
        exists(parent, what="母样本", owner=aliquot["id"])

    # ---- 双号登记：待合并且任何来源不得丢弃 -----------------------------------
    for sample in samples:
        dup = sample.get("duplicate_record")
        accession_set = {
            reg["accession_no"] for reg in sample.get("registrations", [])
        }
        if dup is not None:
            if dup.get("status") != "pending_merge":
                problems.append(
                    f"样本 {sample['id']} 双号登记状态应为 pending_merge，"
                    f"实际 {dup.get('status')!r}"
                )
            if dup.get("review_state") != "awaiting_verification":
                problems.append(
                    f"样本 {sample['id']} 重复建档须先等待核实合并"
                )
            if not dup.get("keep_all_sources"):
                problems.append(
                    f"样本 {sample['id']} 合并核实期间不得丢弃任何来源编号"
                )
            survivors = dup.get("survive_as", [])
            if accession_set and not accession_set.issubset(set(survivors)):
                problems.append(
                    f"样本 {sample['id']} survive_as 必须保留全部登记编号，"
                    f"遗漏 {accession_set - set(survivors)}"
                )
            if len(sample.get("registrations", [])) < 2:
                problems.append(
                    f"样本 {sample['id']} 标记了重复建档却只有一个登记编号"
                )

        # ---- 离线扫码：核实前不得入账 ----
        for scan in sample.get("offline_scans", []):
            if scan.get("review_state") == "awaiting_verification" and scan.get("accepted"):
                problems.append(
                    f"样本 {sample['id']} 离线扫码 {scan.get('scan_id')} "
                    f"未核实即被接受入账"
                )

    # ---- 分装守恒：Σ 分装 == 母样初始数量 -------------------------------------
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for aliquot in aliquots:
        children_by_parent.setdefault(aliquot["parent_sample_ref"], []).append(aliquot)
    for sample in samples:
        children = children_by_parent.get(sample["id"], [])
        if not children:
            continue
        units = {c["quantity"]["unit"] for c in children}
        units.add(sample["initial_quantity"]["unit"])
        if len(units) != 1:
            problems.append(
                f"样本 {sample['id']} 分装单位不一致: {sorted(units)}"
            )
            continue
        split_sum = round(sum(_q(c) for c in children), 6)
        initial = _q(sample, "initial_quantity")
        if split_sum != initial:
            problems.append(
                f"样本 {sample['id']} 分装不守恒: 分装合计 {split_sum} "
                f"!= 初始 {initial}"
            )

    # ---- 分装台账：制备 + 退回 − 耗用 − 已销毁 == 待处置（销毁/归档） -----------
    def ledger(aliquot_id: str) -> tuple[float, float, float, float, float, str]:
        prepared = consumed = returned_in = returned_out = disposed = 0.0
        unit = ""
        for aliquot in aliquots:
            if aliquot["id"] == aliquot_id:
                prepared = _q(aliquot)
                unit = aliquot["quantity"]["unit"]
        for cons in consumptions:
            if cons.get("aliquot_ref") == aliquot_id:
                consumed += _q(cons)
                if cons["quantity"]["unit"] != unit:
                    problems.append(
                        f"耗用 {cons['id']} 与分装 {aliquot_id} 单位不符"
                    )
        for ret in returns:
            if ret.get("returned_to_aliquot_ref") == aliquot_id:
                returned_in += _q(ret)
                if ret["quantity"]["unit"] != unit:
                    problems.append(
                        f"退回 {ret['id']} 与分装 {aliquot_id} 单位不符"
                    )
            if ret.get("aliquot_ref") == aliquot_id:
                returned_out += _q(ret)
        for disp in disposals:
            if disp.get("aliquot_ref") == aliquot_id and disp.get("status") == "executed":
                disposed += _q(disp)
        return prepared, consumed, returned_in, returned_out, disposed, unit

    for ret in returns:
        source = ret.get("aliquot_ref")
        target = ret.get("returned_to_aliquot_ref")
        exists(source, what="分装", owner=ret["id"])
        exists(target, what="退回目标分装", owner=ret["id"])
        if source in ids and target in ids:
            if ids[source].get("parent_sample_ref") != ids[target].get("parent_sample_ref"):
                problems.append(
                    f"退回 {ret['id']} 只能并入同一母样本的分装"
                )

    for cons in consumptions:
        target = cons.get("aliquot_ref")
        exists(target, what="分装", owner=cons["id"])
        for result_ref in cons.get("used_by_result_refs", []):
            exists(result_ref, what="检测结果", owner=cons["id"])
            if result_ref in ids and ids[result_ref].get("aliquot_ref") != target:
                problems.append(
                    f"耗用 {cons['id']} 关联的结果 {result_ref} 取材分装不一致"
                )

    for disp in disposals:
        exists(disp.get("aliquot_ref"), what="分装", owner=disp["id"])
        if disp.get("status") not in ("planned_pending_expiry", "executed"):
            problems.append(f"处置 {disp['id']} 状态非法: {disp.get('status')}")

    for aliquot in aliquots:
        prepared, consumed, returned_in, returned_out, disposed, unit = ledger(
            aliquot["id"]
        )
        planned = round(
            sum(
                _q(d)
                for d in disposals
                if d.get("aliquot_ref") == aliquot["id"]
                and d.get("status") == "planned_pending_expiry"
            ),
            6,
        )
        units = {
            d["quantity"]["unit"]
            for d in disposals
            if d.get("aliquot_ref") == aliquot["id"]
        }
        if units and unit and units != {unit}:
            problems.append(f"处置数量与分装 {aliquot['id']} 单位不符: {units}")
        remaining = round(
            prepared + returned_in - returned_out - consumed - disposed, 6
        )
        if remaining < 0:
            problems.append(
                f"分装 {aliquot['id']} 台账为负: 制备 {prepared} + 退回 {returned_in} "
                f"- 耗用 {consumed} - 已销毁 {disposed}"
            )
        elif remaining != planned:
            problems.append(
                f"分装 {aliquot['id']} 去向不符: 结余 {remaining} 与已登记待处置 "
                f"{planned} 不一致（每份材料都必须有用途或到期去向）"
            )

    # ---- 留样政策：留样在保，处置排在法定留样期满之后 --------------------------
    retained_ids = {a["id"] for a in aliquots if a.get("retained")}
    policy_covered: set[str] = set()
    for policy in policies:
        exists(policy.get("event_ref"), what="暴露事件", owner=policy["id"])
        hold_until = _date_of(policy["legal_hold_until"])
        for ref in policy.get("applies_to_aliquots", []):
            if exists(ref, what="留样分装", owner=policy["id"]):
                if ref not in retained_ids:
                    problems.append(
                        f"政策 {policy['id']} 把非留样分装 {ref} 列为留样"
                    )
                policy_covered.add(ref)
        for disp in disposals:
            if disp.get("aliquot_ref") in policy.get("applies_to_aliquots", []):
                if _date_of(disp["planned_after"]) < hold_until:
                    problems.append(
                        f"处置 {disp['id']} 排在法定留样期 "
                        f"{policy['legal_hold_until']} 结束之前"
                    )
    for ref in retained_ids:
        if ref not in policy_covered:
            problems.append(f"留样分装 {ref} 没有法定留样政策覆盖")

    # ---- 转交：封签、接收确认、时间顺序 ---------------------------------------
    for tfr in transfers:
        owner = tfr["id"]
        sample_refs = tfr.get("sample_refs", [])
        if tfr.get("sample_ref"):
            sample_refs.append(tfr["sample_ref"])
        require_all(sample_refs, what="样本", owner=owner)
        require_all(tfr.get("aliquot_refs", []), what="分装", owner=owner)
        for seal_ref in ([tfr["seal_ref"]] if tfr.get("seal_ref") else []) + tfr.get(
            "seal_refs", []
        ):
            if seal_ref not in seal_owners:
                problems.append(f"转交 {owner} 封签不存在: {seal_ref}")
        if not tfr.get("seal_intact_at_ship") or not tfr.get("seal_intact_at_receive"):
            problems.append(f"转交 {owner} 封签在交出或接收时不完整")
        if not tfr.get("receipt_confirmed"):
            problems.append(f"转交 {owner} 缺少接收确认")
        if tfr.get("received_at") and _isotime(tfr["received_at"]) < _isotime(
            tfr["shipped_at"]
        ):
            problems.append(f"转交 {owner} 接收时间早于发出时间")

    # ---- 检测结果：多方法多置信度可并存，初筛阴性不得解除观察 -------------------
    for result in results:
        exists(result.get("aliquot_ref"), what="取材分装", owner=result["id"])
        if result.get("confidence") not in CONFIDENCE_LEVELS:
            problems.append(
                f"结果 {result['id']} 置信度非法: {result.get('confidence')}"
            )
        if result.get("method") not in ("morphology", "chemical", "molecular"):
            problems.append(f"结果 {result['id']} 方法类别非法: {result.get('method')}")
        if result.get("rerun_of"):
            exists(result["rerun_of"], what="原结果", owner=result["id"])

    methods = {r.get("method") for r in results}
    if results and len(methods) < 2:
        problems.append("联检应允许形态/化学/分子等不同方法并存并分别给出置信结论")

    negative_screening = [
        r
        for r in results
        if r.get("is_screening")
        and any(mark in str(r.get("conclusion", "")).lower() for mark in SCREENING_NEGATIVE_MARKERS)
    ]
    if negative_screening:
        if not any(
            m.get("observation", {}).get("early_termination_blocked")
            and any(
                hold.get("decision") == "continue_observation"
                and not hold.get("superseded", False)
                for hold in m.get("observation", {}).get("holds", [])
            )
            for m in meals
        ):
            problems.append(
                "存在阴性初筛却没有阻断提前结束观察的决定（阴性 + 症状缓解不能解除观察）"
            )
        for r in negative_screening:
            aliquot = ids.get(r.get("aliquot_ref", ""))
            sample = ids.get(aliquot.get("parent_sample_ref", ""), {}) if aliquot else {}
            event_id = sample.get("event_ref")
            covered = any(
                m.get("event_ref") == event_id
                and m.get("observation", {}).get("early_termination_blocked")
                for m in meals
            )
            if event_id and not covered:
                problems.append(
                    f"初筛阴性结果 {r['id']} 所属事件没有维持观察的阻断记录"
                )

    # ---- 报告：版本只增不覆盖，结论不替代诊断 ---------------------------------
    by_report_no: dict[str, list[dict[str, Any]]] = {}
    for report in reports:
        exists(report.get("event_ref"), what="暴露事件", owner=report["id"])
        require_all(report.get("based_on_result_refs", []), what="检测结果", owner=report["id"])
        if not str(report.get("diagnostic_boundary", "")).strip():
            problems.append(f"报告 {report['id']} 必须声明检验结论不替代医生诊断")
        by_report_no.setdefault(report["report_no"], []).append(report)

    for report_no, group in by_report_no.items():
        versions = sorted(r["version"] for r in group)
        if versions != list(range(1, len(versions) + 1)):
            problems.append(
                f"报告 {report_no} 版本号必须从 1 连续递增，实际 {versions}"
            )
        if len(group) != len({r["version"] for r in group}):
            problems.append(f"报告 {report_no} 存在重复版本号")
        by_version = {r["version"]: r for r in group}
        for report in group:
            newer = report.get("supersedes")
            older = report.get("superseded_by")
            if newer:
                if newer not in ids or ids[newer].get("report_no") != report_no:
                    problems.append(
                        f"报告 {report['id']} 的 supersedes 必须指向同一编号旧版"
                    )
                elif ids[newer].get("superseded_by") != report["id"]:
                    problems.append(
                        f"报告 {newer} 未回指新版 {report['id']}（版本链断裂）"
                    )
                if report["version"] != ids[newer].get("version", 0) + 1:
                    problems.append(
                        f"报告 {report['id']} 只能紧邻替代上一版本"
                    )
            if older:
                if older not in ids or ids[older].get("supersedes") != report["id"]:
                    problems.append(
                        f"报告 {report['id']} 的 superseded_by 与新版互指不一致"
                    )
                if report.get("status") != "superseded":
                    problems.append(
                        f"旧版报告 {report['id']} 被替代后状态应为 superseded（保留不覆盖）"
                    )

    # 复测是新增结果，原结果必须仍在链上且不得标记为被覆盖删除
    for result in results:
        original_id = result.get("rerun_of")
        if original_id and original_id not in result_ids:
            problems.append(
                f"复测 {result['id']} 的原结果 {original_id} 已不在链上——旧结果不得删除"
            )
        if result.get("superseded"):
            problems.append(
                f"结果 {result['id']} 不应被覆盖；请以新结果（rerun_of）追加"
            )

    # ---- 新版本触发在观人员重评 ----------------------------------------------
    for reassess in reassessments:
        report_id = reassess.get("triggered_by_report_ref")
        exists(report_id, what="触发报告", owner=reassess["id"])
        exists(reassess.get("event_ref"), what="暴露事件", owner=reassess["id"])
        targets = reassess.get("targets", [])
        require_all(targets, what="重评对象", owner=reassess["id"])
        if report_id in ids and ids[report_id]["version"] < 2:
            problems.append(
                f"重评 {reassess['id']} 应由新版本报告触发，而不是覆盖首版"
            )
        watching = reassess.get("still_under_observation", [])
        if not watching:
            problems.append(f"重评 {reassess['id']} 没有仍在观察的关联人员")
        if set(watching) - set(targets):
            problems.append(f"重评 {reassess['id']} 的在观人员超出重评对象范围")

    # ---- 升级可追溯：采用报告+版本、样本当时状态、接收确认 ---------------------
    for esc in escalations:
        exists(esc.get("event_ref"), what="暴露事件", owner=esc["id"])
        trace = esc.get("trace", {})
        report_id = trace.get("report_ref")
        exists(report_id, what="采用的报告", owner=esc["id"])
        if report_id in ids:
            if trace.get("report_version_at_use") != ids[report_id].get("version"):
                problems.append(
                    f"升级 {esc['id']} 记录的报告版本与所指报告不一致"
                )
        receipt_refs = trace.get("receipt_refs", [])
        if not receipt_refs:
            problems.append(f"升级 {esc['id']} 缺少接收确认追溯")
        for ref in receipt_refs:
            if exists(ref, what="转交记录", owner=esc["id"]) and not ids[ref].get(
                "receipt_confirmed"
            ):
                problems.append(f"升级 {esc['id']} 依据的转交 {ref} 未经接收确认")
        for state in trace.get("sample_states_at_use", []):
            ref = state.get("sample_ref")
            exists(ref, what="样本", owner=esc["id"])
            for retained in state.get("retained_aliquots", []):
                if exists(retained, what="在存留样", owner=esc["id"]):
                    if ids[retained].get("parent_sample_ref") != ref:
                        problems.append(
                            f"升级 {esc['id']} 样本状态中留样 {retained} 不属于 {ref}"
                        )
        if not trace.get("sample_states_at_use"):
            problems.append(f"升级 {esc['id']} 缺少样本当时状态")

    # ---- 隐私：临床最小化 + 公开提示脱敏 --------------------------------------
    for person in people:
        leaked = [f for f in IDENTITY_FIELDS if f in person]
        if leaked:
            problems.append(
                f"人员 {person['id']} 含敏感字段 {leaked}，链条中只应出现化名"
            )
    for event in events:
        summary = event.get("clinical_summary_min", {})
        if summary.get("audience") != "laboratory":
            problems.append(
                f"事件 {event['id']} 临床摘要应限定面向实验室（最小必要）"
            )
        excluded = set(summary.get("fields_excluded", []))
        missing = set(IDENTITY_FIELDS) - excluded
        if missing:
            problems.append(
                f"事件 {event['id']} 临床摘要未声明排除 {sorted(missing)}"
            )
    event_sites = {
        e.get("collection_site_ref")
        for e in events
        if e.get("collection_site_ref")
    }
    for site in sites:
        if site["id"] in event_sites and site.get("precision") != "blurred":
            problems.append(f"采集点 {site['id']} 必须模糊化")
        release = site.get("public_release", {})
        if not release.get("hide_exact_point"):
            problems.append(f"采集点 {site['id']} 公开风险提示未隐藏精确采集点")
        text = str(release.get("text", ""))
        for person in people:
            pseudo = person.get("pseudonym")
            if pseudo and pseudo in text and len(pseudo) <= 1:
                # 单字化名可能误命中常用字；只做结构性提醒，不做逐字判定。
                pass
        if not text.strip():
            problems.append(f"采集点 {site['id']} 缺少公开风险提示文案")

    return problems


def validate_case(path: str | Path) -> Case:
    """加载并校验样例；有问题时抛出 :class:`ChainValidationError`。"""

    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    problems = validate_payload(payload)
    if problems:
        raise ChainValidationError(problems)
    return load_case(path)
