# 野生菌样本联检

急诊、疾控和实验室围绕一次误食事件交接样本并修订联检结论。

`fixtures/sample_transfer.json` 是一份经过脱敏的业务样例（**schema v2**），以**样本链条**为主线，把暴露事件、同餐关系、模糊化采集位置、样本部位/保存条件/封签、转交接收、分装—检测耗用—退回—留样到期销毁台账、多方法多置信结论、报告版本链、重评、升级追溯、最小必要临床摘要和公开风险提示全部用同一套关联标识串在一起。源代码只定义读取与校验这份样例所需的合同，不含业务流程实现。

## 本地检查

```bash
python -m unittest discover -s tests
```

## v1 → v2 迁移

- 顶层字段 `schema_version / record_id / domain / occurred_at / revision / source` 含义不变：`record_id` 仍为样例编号（`sample-016`），`domain=fungus_chain`，顶层 `occurred_at` 与 `event.occurred_at` 同值，仍是误食事件锚点时间。
- v1 的单条信封记录扩展为一个 bundle；`revision` 升到 2，并在 `migration` 中记录来源版本，不产生新的事件主键。
- `load_record()` 仍只读信封字段，对 v2 样例兼容；新逻辑请用 `load_bundle()`。

## 链条如何表达需求

| 需求 | 落点 |
| --- | --- |
| 两个编号登记同一次误食 | `registration_aliases`（`pending_verification`），两套原始登记并存，回链同一事件 |
| 重复建档/离线扫码先等核实、任何来源不丢弃 | 离线扫码 `queued_pending_verification` 挂到合并组，不自行建档；校验禁止把待核实来源标成已同步或删除任一登记 |
| 模糊化采集位置 | `collection_sites`：`descriptor_fuzzy` + `geo_precision=grid_1km`，禁存精确坐标 |
| 样本部位/保存/封签/转交 | `samples.body_part_fuzzy`、`storage_requirement`、`seal_id`；`transfers` 带冷链、封签状态和 `receipt_ack` |
| 分装/耗用/退回/销毁数量相符 | `disposition_ledger` 逐笔流水；每份材料 初始+流入−流出=0，耗用笔与 `test_runs.consumed_qty_*` 一致，退回父子配对，单位不得混用 |
| 留样到期可证明去向 | 销毁动作必须带 `certificate_id`、在场角色、处置方式，且销毁日期不早于 `retention_until`；`disposition_evidence()` 汇总 |
| 形态/化学/分子不同置信结论 | `findings` 每条带 `confidence`（high/medium/low/none）；低置信阴性（熟汤初筛、降解 PCR）保留但不采信 |
| 报告复核/污染 → 新版本重评，不覆盖旧结果 | `reports` 版本链（`supersedes`/`superseded_by`），恰好一份 `current` 且必须是最高版本；污染检测 `valid=false` 对应 `withdrawn_contamination/none`；`re_evaluations` 只覆盖观察未结束人员，`changes_diagnosis=false` |
| 不替医生诊断 | 报告文本与重评记录均显式声明；校验强制重评不改诊断 |
| 实验室只接触必要临床摘要 | `clinical_summaries`：仅 `minimum_necessary`，强制屏蔽 identity/contact/address，只授予联检实验室；`lab_clinical_view()` 出最小视图 |
| 公开风险提示隐藏个人和敏感采集点 | `public_advisories`：带 `redactions` 清单、不带 `site_ref`；`public_advisories_safe()` 只出 id/时间/文本 |
| 一次升级追到报告/结果、样本状态、接收确认 | `escalations.sample_state_snapshot` 由链条时间线重建核对（未采集/在途/在库），在库样本必须能追到对应 `ack`；`trace_escalation()` 出三联追溯 |

## 主要 API

```python
from fungus_chain import load_bundle, ChainIntegrityError

b = load_bundle("fixtures/sample_transfer.json")
b.check()                       # 违反任一不变量即抛 ChainIntegrityError（含全部违规说明）
b.validate()                    # 或直接取违规列表
b.lab_clinical_view()           # 实验室最小必要临床视图
b.public_advisories_safe()      # 脱敏后的公开提示
b.trace_escalation("esc-001")   # 升级 → 采用结果 + 样本当时状态 + 接收确认
b.disposition_evidence()        # 每份材料数量对平情况与销毁凭证
```

新增状态时：沿用现有关联标识，旧状态不得静默删除（重复来源只能 `pending_verification` 挂起），历史报告只能 `superseded` 不能改写。
