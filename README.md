# 野生菌样本联检

急诊、疾控和实验室围绕误食事件交接样本并修订联检结论。

`fixtures/sample_transfer.json` 保存一条经过脱敏的业务样例：**9·19 误食事件**中，急诊剩余菌汤初筛阴性恰逢患者症状缓解，同餐人员差点提前结束观察；疾控从采集地补采完整菌体后，形态、化学（LC-MS/MS）与分子（ITS）三条线分别给出不同置信度的结论，污染疑点又以报告新版本重评在观人员。

## 模型概览

样例在信封字段（`schema_version / record_id / domain / occurred_at / revision / source`）之外追加 `entities` 事件链，实体之间一律用 `*_ref` 稳定标识关联：

| 实体类型 | 作用 |
| --- | --- |
| `exposure_event` | 暴露事件；挂载面向实验室的最小临床摘要 |
| `shared_meal` | 同餐关系（进食份额、发病/潜伏、观察窗与“不得提前结束”的阻断决定） |
| `person` | 同餐人员，只有化名，无身份字段 |
| `collection_site` | 模糊化采集位置 + 脱敏的公开风险提示 |
| `sample` | 母样本：部位、保存条件、封签、多个登记编号、离线扫码、初始数量 |
| `aliquot` | 分装（含留样 `retained`） |
| `transfer` | 转交：封签完整性、双方接收确认、时间顺序 |
| `consumption` / `return` / `disposal` | 检测耗用、残液退回、到期销毁或归档 |
| `test_result` | 形态/化学/分子结果，各自带 `confidence`；复测用 `rerun_of` 追加 |
| `report` | 同编号多版本报告，`supersedes/superseded_by` 互指，旧版保留为 `superseded` |
| `observation_reassessment` | 新版本报告触发的在观人员重评 |
| `escalation` | 升级：可追到采用的报告+版本、样本当时状态、转交接收确认 |
| `retention_policy` | 法定留样期及覆盖的留样分装 |

## 关键规则（由 `fungus_chain.validation` 强制）

- **来源不丢弃**：两个系统的登记编号标记 `pending_merge / awaiting_verification`，`survive_as` 必须保留全部编号；离线扫码核实前不得入账。
- **数量相符**：分装合计等于母样初始量；每个分装满足 `制备 + 退回 − 耗用 − 已销毁 == 已登记的待处置量`，每份材料都有用途或到期去向；处置不得早于法定留样期结束。
- **阴性初筛不解除观察**：低置信筛查阴性必须同时存在“继续观察”的未失效阻断决定。
- **多方法并存**：形态/化学/分子可给出不同置信结论；污染疑点以**新结果 + 报告新版本**处理，旧结果、旧报告保留不覆盖，报告必须声明不替代医生诊断；重评只针对仍在观察的关联人员。
- **升级可追溯**：升级记录必须含采用报告的版本、样本当时状态（含在存留样）、已确认接收的转交记录。
- **隐私最小化**：链条中只有化名；实验室只拿到排除身份字段的最小临床摘要；公开预警隐去精确采集点与个人信息。

## 使用

```python
from fungus_chain import load_record, validate_case

load_record("fixtures/sample_transfer.json")   # 旧最小合同：只读信封字段
validate_case("fixtures/sample_transfer.json") # 事件链 + 全部业务规则，失败抛 ChainValidationError
```

## 本地检查

运行 `python -m unittest discover -s tests`。

## 修订与迁移约定

- 既有标识（`record_id`、各实体 `id`、`*_ref`）和时间字段（ISO 8601 带偏移）含义不变；`revision` 只增。
- 信封字段向前兼容：`DomainRecord` 只取已知键，旧读取方忽略 `entities` 等新增键。
- 新增实体状态必须同时：(1) 在本说明中登记取值；(2) 在 `validation.py` 说明旧状态如何迁移；(3) 提供正向与违规两类测试。
- 报告与结果**不做原地覆盖**：修正一律新增版本/复测记录，并由旧记录回指。
