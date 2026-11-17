# data-qd-149 · 事故响应SLA核算与共享人员工作计划

本仓提供两个相互独立、离线运行的数据应用，业务输入、计算和边界详见 [数据契约](docs/contract.md)：

1. **v1 经过时长 SLA 核算**：按显式排除的暂停区间核算响应与解决有效时长。
2. **v2 共享响应人员工作计划**：在有限人员下，精确求出哪些未响应事故能完整按双 SLA 服务及最优排班。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/pip check
# v1 经过时长核算
.venv/bin/incident-sla --source fixtures/input.json --output run/report.json
# v2 共享人员工作计划（与 --source 互斥）
.venv/bin/incident-sla --plan-source fixtures/plan_input.json --output run/plan_report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加（报告字节稳定）。测试使用临时目录，运行产物在忽略的run/。

`src/incident_sla/model.py` 与 `sla.py` 为 v1 业务模型，`plan.py` 为 v2 规划器；
`common.py` 提供受限JSON及报告发布，`cli.py` 用互斥参数衔接两种数据链路。
fixtures 为明确自制样例，代码与样例采用MIT许可。

## v1 经过时长 SLA 核算

事故运营分析者按整数UTC秒核响应与解决时长。targets给非负 response/resolution seconds。
incident 最多10000个唯一 id，opened 整数秒；first_response/resolved 可 null。响应不能早于
opened；解决若有则要求响应存在、且 resolved>=response/opened。开放事故可报告已发生的响应 SLA，
解决时长与判定为 null。

pauses 最多20000条 incident/start/end，必须引用已知事故且 end>start。对 opened→响应、opened→解决
分别将暂停区间裁剪到对应计时范围并按区间并集合并，重叠暂停不重复扣减；响应后暂停只影响解决时钟。
有效时间 = end-start-并集暂停秒。阈值采用 elapsed<=target 为满足。暂停原因与授权由调用方保证。

样例 I-1 墙钟解决4000秒，重叠暂停并集500秒，有效解决3500；响应有效300秒恰满足。所有时间是UTC绝对秒；
不计算营业时间、节假日、时区墙钟、优先级升级或法定SLA结论。该计算不是事故管理系统。

## v2 共享响应人员工作计划

计划输入只包含尚未响应的事故，目标是在 60 秒固定时隙、最多 3 名共享人员、最多 32 个时隙的 horizon 内，
精确决定最多 8 起事故中哪些能完整满足双 SLA，以及逐时隙的人员安排。

- 选中事故各安排一个首次响应时隙和 `resolution_slots`（1..4）个解决时隙；解决工作只能从
  `max(首次响应结束, customer_ready)` 起开始。
- 等待客户的自动暂停 `[R, customer_ready)` 依据实际选定的响应时隙 R 重新形成，与显式 pauses
  取并集后裁剪；R 变化时时钟重算，不沿用固定截止时间。
- 每个工作时隙须由对应阶段资格名单中且该时隙 available 的人员承担；同一人员同一时隙至多一个工作，
  同一事故同一时隙至多一个工作；解决工作允许跨人员、非连续。
- 优化按序精确最大化 value 总和、选中数量，再最小化完成时刻 F 之和；仍并列时取工作项序列
  `(slot, responder, incident, phase)` 的字典序最小者。
- 求解是穷举证明（标准库实现，无贪心、无超时近似、无外部服务），报告时刻与有效秒由实际 schedule
  重建并核验。空计划合法，目标为 `(0,0,0)`。
