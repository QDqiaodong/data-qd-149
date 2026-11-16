# data-qd-149 · 事故响应SLA核算

Elapsed-time SLA accounting with explicitly excluded pause intervals。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/incident-sla --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/incident_sla/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## Scope and sample
# Elapsed-time incident SLA v1

事故运营分析者按整数UTC秒核响应与解决时长。targets给非负response/resolution seconds。incident最多10000个唯一id，opened整数秒；first_response/resolved可null。响应不能早于opened；解决若有则要求响应存在、且resolved>=response/opened。开放事故可报告已发生的响应SLA，解决时长与判定为null。
pauses最多20000条incident/start/end，必须引用已知事故且end>start。对opened→响应、opened→解决分别将暂停区间裁剪到对应计时范围并按区间并集合并，重叠暂停不重复扣减；响应后暂停只影响解决时钟。有效时间=end-start-并集暂停秒。阈值采用elapsed<=target为满足。暂停原因与授权由调用方保证。
样例I-1墙钟解决4000秒，重叠暂停并集500秒，有效解决3500；响应有效300秒恰满足。所有时间是UTC绝对秒；不计算营业时间、节假日、时区墙钟、优先级升级或法定SLA结论。该计算不是事故管理系统。


## Scope and sample
# Elapsed-time incident SLA v1

事故运营分析者按整数UTC秒核响应与解决时长。targets给非负response/resolution seconds。incident最多10000个唯一id，opened整数秒；first_response/resolved可null。响应不能早于opened；解决若有则要求响应存在、且resolved>=response/opened。开放事故可报告已发生的响应SLA，解决时长与判定为null。
pauses最多20000条incident/start/end，必须引用已知事故且end>start。对opened→响应、opened→解决分别将暂停区间裁剪到对应计时范围并按区间并集合并，重叠暂停不重复扣减；响应后暂停只影响解决时钟。有效时间=end-start-并集暂停秒。阈值采用elapsed<=target为满足。暂停原因与授权由调用方保证。
样例I-1墙钟解决4000秒，重叠暂停并集500秒，有效解决3500；响应有效300秒恰满足。所有时间是UTC绝对秒；不计算营业时间、节假日、时区墙钟、优先级升级或法定SLA结论。该计算不是事故管理系统。


## Scope and sample
# Elapsed-time incident SLA v1

事故运营分析者按整数UTC秒核响应与解决时长。targets给非负response/resolution seconds。incident最多10000个唯一id，opened整数秒；first_response/resolved可null。响应不能早于opened；解决若有则要求响应存在、且resolved>=response/opened。开放事故可报告已发生的响应SLA，解决时长与判定为null。
pauses最多20000条incident/start/end，必须引用已知事故且end>start。对opened→响应、opened→解决分别将暂停区间裁剪到对应计时范围并按区间并集合并，重叠暂停不重复扣减；响应后暂停只影响解决时钟。有效时间=end-start-并集暂停秒。阈值采用elapsed<=target为满足。暂停原因与授权由调用方保证。
样例I-1墙钟解决4000秒，重叠暂停并集500秒，有效解决3500；响应有效300秒恰满足。所有时间是UTC绝对秒；不计算营业时间、节假日、时区墙钟、优先级升级或法定SLA结论。该计算不是事故管理系统。

