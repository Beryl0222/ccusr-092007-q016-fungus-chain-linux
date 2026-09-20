# 野生菌样本联检

急诊、疾控和实验室围绕误食事件交接样本并修订联检结论。

`fixtures/sample_transfer.json` 保存一条经过脱敏的业务样例，源代码只定义读取这份样例所需的最小合同。后续模块应保持既有标识和时间含义，新增状态必须说明迁移方式。

## 本地检查

运行 `python -m unittest discover -s tests`。
