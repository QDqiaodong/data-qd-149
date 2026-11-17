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


# 跨系统事故遥测对齐 v4

v4 是离线数据应用，回答“多个采集系统各自的本地时钟与含糊事故标签联合起来，证据真正支持哪些事件归属于哪起事故、以及能够成立的UTC时间范围”，供数据治理人员判断哪些记录可以合并。它不安排响应人员、不计算赔付，也不把不确定时间任选一点当成事实。命令行用 `--reconcile-source FILE --output FILE`，与 v1/v2/v3 全部入口互斥；成功输出 `schema_version=4` 并保留源字节 SHA256；仅标准库、无网络服务、无持久数据库。

## 输入契约（拒绝未知字段、重复键、布尔冒充整数、非法引用）

顶层严格包含 `sources`、`syncs`、`incidents`、`events` 四个字段，数组上限整体校验。

- `sources` 为 1..8 条 `{id, min_offset, max_offset}`。每个系统有一个**全局共享**整数偏移 `o`，`UTC = local + o`；`-10^9 <= min_offset <= max_offset <= 10^9`；`id` 唯一并沿用标识符规则。
- `syncs` 最多 32 条 `{id, left, right, min_delta, max_delta}`，独立唯一 `id`，引用已有 source，要求 `min_delta <= o[right] - o[left] <= max_delta`，界限在 `[-2*10^9, 2*10^9]`。允许自引用、重复系统对及零宽约束——每条都是独立证据，不因重复而合并或失效。
- `incidents` 为 1..4 个唯一事故 id。
- `events` 恰为 `3*事故数` 条 `{id, source, local, phase, candidates}`，`id` 唯一、`source` 有效、`local` 为 `0..4102444800` 整数；`phase` 只能是 `opened`、`first_response`、`resolved`，且每种 phase 的条数恰等于事故数；`candidates` 是非空、无重复的已有事故 id 数组。

结构合法但无任何全局解释是**正常业务结果**：报告 `status=infeasible`，退出 0，`sources/events/incidents` 均为空数组；不复用输入错误退出码 2。

## 时钟证据

联合处理偏移边界、同步差值与各事件 `UTC∈[0,4102444800]` 的限制。一个归属下的时钟可行域是整数差分约束系统 `x_v - x_u <= w`（偏移盒、同步差值、事件UTC窗、生命周期次序）；精确极值取自该约束图的最短路闭包（Floyd–Warshall，全整数权）。因此：

- 不逐秒枚举偏移（偏移跨度可达 20 亿秒），也不以浮点容差判定整数可行性；
- 保留系统间相关性，绝不把各 source 独立最小/最大值的笛卡尔积当成可行时钟；
- 负偏移、等式同步、多跳相关性、矛盾环、自引用矛盾、20 亿秒跨度与 UTC 边界都按同一机制精确处理。

## 事件归属与生命周期

每个 event 必须且只能归给 `candidates` 中的一个事故；每个事故必须恰好得到三个不同 phase 的事件。归属后 `opened <= first_response <= resolved` 必须按**精确整数UTC**成立。v1 事故生命周期的顺序/缺失约束已提取为 `model.validate_lifecycle` 共用能力，v1 四种输入（已响应/未响应 × 已解决/未解决）的接受或拒绝行为保持不变；对齐模块每个完整具体生命周期也通过同一能力校验（对齐输入三相位齐全，但校验器仍保留 v1 允许缺失的语义）。不用原 v1 接受区间或模糊时间替代整数。

## 联合求解（不得先校时再做二分图匹配）

一个全局解释同时包含所有 source 偏移与所有 event 归属，并满足全部证据。若某事件归属引入的生命周期次序收紧了系统间偏移差值，时钟证据职责必须把该限制带入其他事件归属的可行性判断；反过来，同步证据排除一个时钟组合时，也必须剔除依赖它的归属。实现按相位枚举候选双射（`permutations`，按候选成员过滤），但对每一个**完整归属**都与时钟证据**联合**求可满足性：生命周期次序直接作为差分约束边加入同一约束图。因此不存在“先独立校时、再做普通匹配”的分解；不引入目标函数，所有合法解释地位相同。

## v4 报告

`result.status` 为 `feasible` 或 `infeasible`。

- 可行时 `sources` 按 id 输出 `{id, offset_min, offset_max}`，为所有全局解释的**精确极值**。
- `events` 按 id 输出 `{id, possible_incidents, forced_incident}`；`possible_incidents` 为全局有解的事故 id 排序集合，仅当集合大小为 1 时 `forced_incident` 才为该 id，否则 `null`。
- `incidents` 按 id 输出 `{id, opened, first_response, resolved, response_elapsed, resolution_elapsed}`，每项时间或时长均为 `{min,max}`。`response_elapsed = first_response - opened`、`resolution_elapsed = resolved - opened`，**必须在同一解释内做差再求极值**，不得用两个独立时间范围的端点相减（否则宽时间范围下固定差值会被错误放宽）。
- 所有范围与 possible 关系都由至少一个**完整**全局解释支持；不同极值可以来自不同解释，报告不声称它们能同时实现；不报告某个最优解、可行样例或匹配局部可行边。

报告精确且字节稳定；沿用受限 JSON 读取、SHA 与安全文件替换；拒绝输入输出同路径及符号链接；失败退出 2 且保留旧输出。

## 正确性说明（为何不丢解、不混入不存在的解释）

相位双射枚举覆盖每个事件到候选事故的每一种可能归属；对每个归属，差分约束系统可满足当且仅当存在真实整数偏移向量使其全部约束成立，且约束图无负环是其充要条件（最短路三角不等式给出构造性见证）。极值查询使用约束闭包：节点 `i` 的偏移下/上界为 `-d(i,ref)`、`d(ref,i)`；事件 `e`（source `s`、local `l`）的UTC下/上界为 `l - d(s,ref)`、`l + d(ref,s)`。这些值均为整数，且在图可行时在最短路闭包中取紧可达。因此所报范围恰为所有全局解释的并集投影，既不遗漏（每个可行的相位归属都被枚举并与时钟联合检验），也不多报（每个被报告的值都对应一个完整解释）。时长在单解释内取差后再做“跨解释”极值，故得到的是真实可实现时长的极值。


# 可轮换密钥的事故证据仓 v5

v5 是离线的多租户事故证据仓：把多个租户的 v1 事故数据加密进同一个 SQLite 仓库，按租户精确检索、还原并核算 SLA，并支持不离线的密钥轮换。它不使用排班、赔付或遥测对齐概念；存入的唯一业务载荷是 v1 `targets/incidents/pauses` 文档，校验直接调用 `model.analyze` 本身，接受/拒绝契约与 v1 完全一致。四个互斥入口，均须 `--db DB --keyring FILE`：`--vault-init`（另须 `--key-id KEYID`）、`--vault-import FILE`、`--vault-query TENANT`（另须 `--output FILE`）、`--vault-rekey KEYID`（另须 `--limit N`）；其余参数组合（含把 `--keyring/--key-id/--limit` 用于 v1-v4 入口）一律拒绝。加密使用锁版本的 `cryptography` 依赖（AES-256-GCM 与 HKDF），不接任何外部密钥服务；v1-v4 入口与报告保持兼容、仍只用标准库。

## 密钥环契约（每命令重新读入）

KEYRING 严格为 `{"keys":[{"id","key_hex"}]}`，拒绝未知字段与重复键。`id` 沿用标识符规则且在环内唯一；`key_hex` 恰为 64 位小写十六进制（32 字节）。每个命令执行时重新读入并校验整个密钥环；密钥字节绝不写入 DB、日志或报告，stdout/stderr 与出错信息不含 `key_hex`。环中每个密钥是一个 256 位密钥加密密钥（KEK），以其 `id` 作为密钥版本。

## init：只创建不存在的仓库

`--vault-init --db DB --keyring FILE --key-id KEYID` 仅在 DB 不存在（也不是符号链接）时创建仓库，已存在即拒绝。`KEYID` 必须存在于密钥环，成为首个活动密钥。init 生成 16 字节随机仓库身份 `vault_id`（32 位小写十六进制）并持久化于 `meta`，此后所有查询与轮换报告都携带它；密文 AAD 绑定该身份，使跨仓库移植密文必然认证失败。init 失败不留残缺 DB。

## import：整批校验、原子入仓

输入严格为 `{"documents":[{"id","tenant","payload"}]}`，1..100 条。`id` 沿用标识符规则，批内与仓内都唯一且不可修改（重复 id 整批拒绝，无重放语义）；`tenant` 沿用标识符规则、大小写敏感、不做任何 Unicode 正规化；`payload` 严格复用 v1 的 `targets/incidents/pauses` 接受与拒绝契约（未知字段、缺失生命周期、重叠暂停等行为与 v1 相同）。整批先完整校验再原子入仓：任一重复 id 或非法 payload 都不得部分写入。

每份 payload 保存其规范 JSON 的 SHA256 以核实还原。规范字节精确为 `json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',',':'), allow_nan=False).encode('utf-8')`，无末尾换行，Unicode 不额外正规化；摘要随记录一起加密，查询解密后重算比对。

## 数据表示（加密入仓职责）

- 每份文档生成全新随机 256 位文档密钥（DEK）。记录 `{"payload","payload_sha256","tenant"}` 以规范 JSON 序列化后用 AES-256-GCM 在 DEK 下加密；DEK 用活动 KEK 以 AES-256-GCM 封装。两处加密的 AAD 都绑定 `vault_id` 与 document id（封装还绑定 key id），因此翻转密文/nonce、跨文档替换密文、跨仓库移植整条记录都认证失败。
- 随机 96 位 nonce 遵循 AES-GCM 的不重复要求（NIST SP 800-38D 随机 nonce 构造；本仓单写者且文档数远低于每密钥 2^32 次调用的界限）。不自设计密码算法，全部使用成熟库的 AEAD/HKDF/HMAC 原语。
- 租户等值索引是带密钥且不可逆的：索引键由 KEK 经 HKDF-SHA256（域分离 info）派生，token 为 `HMAC-SHA256(索引键, tenant)`。不以普通 SHA 或确定性明文加密充当租户索引。
- `key_checks` 为每个在仓内出现过的密钥版本保存不可逆校验值 `HMAC-SHA256(KEK, 域分离串 || vault_id || key_id)`。每个命令在使用任何密钥前先核对其校验值：密钥值错误（id 对、值错）在写入前明确拒绝，绝不产生乱码业务数据或把部分可读误报为成功。轮换完成时删除已退役密钥的校验值。
- DB 中除 document id、仓库身份、密钥版本、计数、密文元信息（nonce、封装 DEK、记录密文、校验值）与带密钥等值索引外，不出现 tenant、事故值或密钥明文；该禁令同样覆盖回滚日志/WAL 与任何临时文件（明文只存在于进程内存与最终发布的查询报告中）。

一份文档的逻辑版本 = 记录密文 + 可解密密钥材料（`key_id, wrap_nonce, wrapped_dek`）+ 索引行（`doc_id, token`）。轮换时 DEK 不变（记录密文无需重加密），封装与索引在同一 SQLite 事务内一起切到目标密钥，因此密钥可解密性与索引可达性始终一致：任何已提交状态下，每份文档都能被其 `key_id` 指向的 KEK 解开封装，并被该 KEK 派生的索引 token 精确定位。

## query：混合版本检索职责

`--vault-query TENANT --db DB --keyring FILE --output FILE` 按 tenant 的完整标识精确选中全部文档（大小写、前缀、Unicode 形似标识都互不匹配）。实现对仓内仍被引用的每个密钥版本各算一次 token（轮换中至多新旧两个），经 SQLite 索引 `tenant_index_by_token` 做带密钥等值检索，只解密候选行；解密后核实记录内 tenant 与查询相等、payload 摘要与重算一致，任一认证失败退出 2 且不发布部分报告。非命中行的密文损坏不影响该 tenant 的查询（这同时证明没有偷偷全仓解密）。输出严格为 `{schema_version:5, vault_id, tenant, documents}`；`documents` 按 id 排序，每份严格为 `{id, tenant, payload, payload_sha256, sla}`，`sla` 是 `model.analyze(payload)` 的完整结果。相同仓库状态与查询的 JSON 字节稳定；查询以只读方式打开、不写 DB；零命中返回空数组并成功；失败保留旧输出。

## rekey：原子轮换职责

`--vault-rekey KEYID --db DB --keyring FILE --limit N`，N 为 1..100。第一次调用在单个事务内把新写入的活动密钥切到 KEYID 并至多迁移 N 份旧版本文档；后续同目标调用继续迁移。任一时刻至多一个未完成目标（`meta.rekey_target`），未完成时改另一目标（含改回旧密钥）整体拒绝。各次调用之间可以 import 和 query：新导入使用新活动密钥，旧文档始终能被查到。只有全部文档及所需索引/密钥材料均不再依赖旧密钥时才报告 `completed=true`。成功状态严格为 `{schema_version:5, vault_id, active_key_id, migrated, remaining, completed}`，`migrated` 为本次迁移文档数，`remaining` 为仍需迁移的文档数；完成后再次同目标调用返回 `migrated=0, remaining=0, completed=true`。

每次 rekey 用单个 SQLite 事务提交本次进度：本批全部封装/索引密码学先在事务内、任何写入前完成（缺密钥或密钥值错误在此拒绝并回滚），提交前异常整体回滚；提交后进程终止时，再运行同目标即可继续——候选永远按 `key_id != 目标` 重新选取，不重复创建文档，不依赖进程内游标。合法的部分进度不漏查、不重查，也不会出现“用旧索引定位后因新密文而报错”。

## 退役保证与威胁边界

轮换完成后：只提供新密钥必须能读取全部文档并继续入仓；仅持有旧密钥必须不能从当前逻辑有效记录及其可达封装恢复任何 tenant 或 payload（旧密钥的封装、索引 token 与校验值都已随完成事务移除）。退役保证**不包括**旧 DB 快照以及 SQLite 空闲页、旧日志/WAL 中已失效密文/封装残留，不以逻辑删除声称物理擦除；前述“禁止写入业务明文”仍覆盖这些文件。仓库为单写者；不承诺抵御整库回滚、删除行或泄露访问频率/文档 id/条数。测试专用钩子 `INCIDENT_SLA_VAULT_TEST_FAULT` 在命名步骤注入崩溃（提交前/后），仅供测试在真实子进程中验证恢复语义，正常运行不读取该变量以外的任何环境状态。
