# Elapsed-time incident SLA v1

事故运营分析者按整数UTC秒核响应与解决时长。targets给非负response/resolution seconds。incident最多10000个唯一id，opened整数秒；first_response/resolved可null。响应不能早于opened；解决若有则要求响应存在、且resolved>=response/opened。开放事故可报告已发生的响应SLA，解决时长与判定为null。
pauses最多20000条incident/start/end，必须引用已知事故且end>start。对opened→响应、opened→解决分别将暂停区间裁剪到对应计时范围并按区间并集合并，重叠暂停不重复扣减；响应后暂停只影响解决时钟。有效时间=end-start-并集暂停秒。阈值采用elapsed<=target为满足。暂停原因与授权由调用方保证。
样例I-1墙钟解决4000秒，重叠暂停并集500秒，有效解决3500；响应有效300秒恰满足。所有时间是UTC绝对秒；不计算营业时间、节假日、时区墙钟、优先级升级或法定SLA结论。该计算不是事故管理系统。

# 共享响应人员工作计划 v2

v2 是离线的有限人员排班应用，回答“有限人员下哪些未响应事故能完整按 SLA 服务、以及如何安排”，而不是给每起事故独立估算截止时间。命令行用 `--plan-source FILE --output FILE`，与 v1 的 `--source` 互斥；成功输出 `schema_version=2`，保留源字节 SHA256。

## 计划输入契约（拒绝未知字段，结构错误整体拒绝）

顶层六个字段全部必填：`slot_seconds`、`horizon`、`targets`、`incidents`、`pauses`、`responders`。

- `slot_seconds` 固定为 60。
- `horizon` 为 `{start,end}`：UTC 整数秒，均被 60 整除，满足 `0<=start<end<=4102444800`，长度为 1 至 32 个时隙。时隙 k 表示 `[start+60k, start+60(k+1))`。
- `targets` 沿用两个非负整数秒阈值 `response_seconds`、`resolution_seconds`。
- `incidents` 最多 8 条，字段 `id, opened, customer_ready, resolution_slots, value, response_responders, resolution_responders`。
  - `id` 沿用标识符规则（非空、去空白、<=128 字符、无控制字符）且全表唯一。
  - `opened` 为不晚于 `horizon.start` 的非负 UTC 整数秒；`customer_ready` 在 `[opened, horizon.end]` 内；两者均被 60 整除。
  - `resolution_slots` 取 1..4；`value` 取 1..100 整数。
  - 两种资格名单都是非空、无重复的人员 id 数组；有事故时名单引用的人员必须存在。
- `responders` 最多 3 条且 id 唯一，每条 `{id, available}`；`available` 是无重复的有效时隙编号数组（0..时隙数-1）。允许零人员，但此时不得有事故。
- `pauses` 沿用 `incident/start/end`，最多 200 条，端点被 60 整除且 `end>start`，必须引用已知事故；允许超出 horizon。
- 布尔值不能冒充整数；非法引用、重复 id、未知字段或任何结构问题都整体拒绝。允许零事故（空计划合法，目标为 `(0,0,0)`）。

## 候选计划的 SLA 时钟

选中事故必须安排恰好一个首次响应时隙和 `resolution_slots` 个解决时隙。记首次响应时隙结束为 R、最后解决时隙结束为 F；解决工作只能从 `max(R, customer_ready)` 起开始。

- 等待客户自动暂停为 `[R, customer_ready)`，仅当 `R<customer_ready` 时存在；它不是输入事实，而是依据实际选定的 R 重新形成。
- 响应有效秒 = opened 至 R，减去显式 pauses 裁剪到该范围后的并集。
- 解决有效秒 = opened 至 F，减去显式 pauses 与自动暂停取并集后再裁剪的总时长。
- 两者均须 `<=` 各自 target（包含等于）。R 改变时自动暂停与解决时钟允许的完成时刻随之重算，绝不沿用前一候选的暂停或固定截止时间。

## 共享资源与优化目标

- 每个工作时隙由对应阶段资格名单中、且该时隙 available 的一名人员承担。
- 同一人员同一时隙至多一个工作；同一事故同一时隙至多一个工作；解决工作允许跨人员、非连续执行。
- 工作时隙不得与该事故任一显式暂停相交。
- 选中事故必须完整满足工作量、先后顺序和双 SLA；未选中事故完全不安排工作。
- 目标按序：精确最大化选中事故 value 总和，再最大化选中数量，再最小化所有选中事故 F 之和；仍并列时取按 `(slot, responder, incident, phase)` 排序后的工作项序列字典序最小者（字符串按 Python Unicode 顺序，phase 取值 `response` 或 `resolution`）。
- 求解为穷举证明，不使用贪心、超时当前最好结果或近似；仅依赖 Python 标准库，不访问外部服务。

## v2 报告

`result` 含：`objective{value,count,completion_sum}`；`schedule` 工作项 `{slot,responder,incident,phase}`；按 id 排序的 `selected{id,first_response,resolved,response_active_seconds,resolution_active_seconds}`；按 id 排序的 `excluded` 字符串数组。所报时刻与有效秒均由实际 schedule 重建并核验。相同输入报告字节稳定；任何阶段失败退出码 2 且旧输出不变，沿用受限 JSON、输入/输出同路径拒绝与符号链接保护。

