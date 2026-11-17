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


# 冗余服务故障赔付结算 v3

v3 是离线的故障赔付结算应用：用组件停机事实与服务依赖图计算各合同周期的精确不可用时长，按阶梯费率得出当前应赔额，并在 SQLite 账本中持久化故障更正、封账与追补/追回分录。它不使用事故响应人员、排班或候选 SLA 时钟。三个互斥入口：`--credit-init CONFIG --db DB`、`--credit-apply BATCH --db DB --output REPORT`、`--credit-view SEQ --db DB --output REPORT`；仅标准库、单写者、离线运行，不承诺多写者协调或数据库文件损毁恢复。

## 配置契约（init 一次写入，此后不可修改）

CONFIG 严格包含 `components`、`services`、`contracts`，拒绝未知字段、重复键、布尔冒充整数。

- `components` 最多 64 个唯一 id；`services` 最多 64 条 `{id, children, required}`；id 沿用标识符规则（非空、去空白、<=128 字符、无控制字符），且跨组件/服务两表唯一。
- `children` 为非空、无重复的组件或服务 id 数组，引用必须存在；`required` 为 1..len(children) 整数。拒绝未知引用与任何依赖环（含自环）。
- 组件停机即不可用，否则可用；服务在可用子节点数少于 `required` 时不可用（等价于至少 `len(children)-required+1` 个子节点同时不可用）。允许子服务被多处共享，同一组件可经多条路径影响同一根服务；实现按区间集合沿 DAG 精确传播，不假设子节点故障独立。
- `contracts` 最多 100 条 `{id, service, cycles, tiers}`，合同 id 唯一；`service` 必须引用已有服务（不是组件）。
- `cycles` 最多 24 个互不相交（半开意义下，端点相接允许）的周期 `{id, start, end, fee_cents}`；周期 id 在合同内唯一；`start`、`end` 为 0..4102444800 整数 UTC 秒且 `end>start`；`fee_cents` 为 0..10^12 整数。
- `tiers` 为非空阶梯表（本实现上限 1000 级），首级必须为 `{after_seconds:0, rate_bps:0}`，`after_seconds` 严格递增（0..4102444800），`rate_bps` 单调不减且为 0..10000 整数。
- 周期内服务不可用秒数 D 取实际不可用区间裁剪到周期后的并集长度；选择满足 `after_seconds<=D` 的最大档位；当前应赔额 `L=floor(fee_cents*rate_bps/10000)`，只在周期总金额处向下取整一次，不按故障事件分别算钱。

## 批次契约（apply）

BATCH 严格为 `{batch_id, replacements, close}`：

- `batch_id` 沿用标识符规则。已成功提交的 id：原始 BATCH 字节 SHA-256 相同则返回原 seq 与原报告（即使之后已有其他批），不同字节（含仅格式差异）整体拒绝。
- `replacements` 最多 64 条 `{component, intervals}`，组件不重复且必须存在于配置；`intervals` 每条最多 20000 对 `[start,end]`，全批总区间数最多 20000；端点 0..4102444800 且 `end>start`。每项**完整替换**该组件全部现行停机事实（先规范化为不相交半开区间再落库）；空数组表示撤销；未出现的组件不变。
- `close` 为无重复的 `{contract, cycle}` 引用数组（隐式上限 2400），引用必须存在；仅允许本批之前未封账的周期，否则整批拒绝。
- 一个成功 apply 分配从 1 开始的连续 seq；故障事实、周期状态、分录与该批审计视图在同一 SQLite 事务中提交；非法输入或提交前异常不改变它们（整批回滚）。

## 故障影响计算

对规范化后的半开故障区间沿门限 DAG 做扫描线合并，得到每个服务的精确不可用区间集，再裁剪到各合同周期；共享子图只计算一次并作为区间集合复用，因此相关性天然精确：同一组件经菱形两条路径影响根服务时只计一次。半开端点在同一时刻的先后由半开语义决定（`[0,10)` 与 `[10,20)` 相接不重叠也不留缝）。不按每秒展开、不求和子服务秒数。影响区间与 D 随该批审计视图保留供审计。

## 结算账本

- 所有周期初始未封账；每批更正都会重算所有周期的当前 L。
- `close` 命中的周期按本批更正后的 L 生成一笔 `close` 分录（零额也记）；已封账周期的原 close 金额永不改写。
- 此前已封账的周期，每批只追加 `adjustment = 当前L - 该周期所有既有分录之和`；差额为零不记；负数表示追回；未封账周期不记分录。因此每批提交后，任一已封账周期的累计分录恒等于其现行 L：撤销更正后又恢复故障时，累计分录重新等于现行 L，而不是一直相对最初 close 金额算差。
- 每笔分录以 `(seq, contract, cycle)` 唯一（对应唯一 batch_id），含 `kind`（`close`/`adjustment`）与有符号整数 `amount_cents`，按批次序列可还原已记金额。

## 审计视图与恢复

- 每个 seq 的视图永久可读，包含 `schema_version=3`、`seq`、`batch_id`、`source_sha256`，以及按 `(contract, cycle)` 排序的 `cycles{contract, cycle, closed, outage_intervals, downtime_seconds, liability_cents, posted_cents}` 和截至该 seq 按 `(seq, contract, cycle)` 排序的全部分录。`--credit-view` 读出的是提交时存储的视图原文，绝不用当前事实重新解释旧账；相同视图 JSON 字节稳定。
- DB 提交与报告文件替换不是同一原子动作：提交后、报告生成或替换前中断时，用相同字节重试同批即可恢复原报告且不重复记账；退出 2 保留旧报告，DB 是否已接受该批可由同 id 重试辨明（已接受则返回原报告，未接受则按新批处理）。
- 输入/DB/输出之间同路径与符号链接均拒绝；沿用受限 JSON 读入（<=8MB、拒绝重复键与非有限数值）。init/view 不产生新 seq。

## 数据库结构（SQLite，schema_version=3）

- `meta(key,value)`：`schema_version`、`config_sha256`（配置原文 SHA-256）、`config_json`（规范化配置，审计用）。
- `components(id)`；`services(id,required)`；`service_children(service,child,position)`；`contracts(id,service)`；`cycles(contract,id,start_sec,end_sec,fee_cents)`；`tiers(contract,after_seconds,rate_bps)`：均为 init 写入的不可变配置。
- `facts(component,start_sec,end_sec)`：现行停机事实，每组件为规范不相交区间，apply 时整组替换。
- `batches(seq,batch_id,source_sha256,report_json)`：seq 从 1 连续；`batch_id` 唯一；`report_json` 为该批审计视图原文，用于同字节重放与历史视图。
- `cycle_state(contract,cycle,closed,close_seq)`：封账状态。
- `entries(seq,contract,cycle,kind,amount_cents)`：只追加的分录。
- `audit_cycles(seq,contract,cycle,closed,outage_intervals,downtime_seconds,liability_cents,posted_cents)`：每批每周期的审计快照（`outage_intervals` 为 JSON 文本），与 `report_json` 内容一致、便于 SQL 核查。
