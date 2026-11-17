本目录为原创虚构合成数据，使用MIT许可。没有真实个人、线上实验、生产日志、交易、项目进度或搜索用户信息；不来自外部平台。

- `input.json`：v1 经过时长 SLA 核算样例。
- `plan_input.json`：v2 共享响应人员工作计划样例，含两起未响应事故、两名共享人员与一条显式暂停。
- `credit_config.json`：v3 故障赔付结算样例配置，菱形 DAG（共享组件 db-shared 经 svc-read/svc-write 影响 svc-root）与三档费率合同。
- `reconcile_input.json`：v4 跨系统遥测对齐样例，两个采集系统经等式同步约束关联、六条事件覆盖两起事故的全部三相位。
- `credit_batch1.json` ~ `credit_batch4.json`：v3 样例批次，演示同批更正后封账、跨档追补、撤销追回与恢复再补。
- `vault_keyring.json`：v5 证据仓演示密钥环（`key-2026-a`/`key-2026-b` 两个随机生成的演示密钥，仅用于本地样例与测试，不保护任何真实数据）。
- `vault_import1.json`：v5 样例导入批，acme 两份、globex 一份 v1 事故文档。
- `vault_import2.json`：v5 样例导入批，轮换期间为 acme 追加的一份文档。
