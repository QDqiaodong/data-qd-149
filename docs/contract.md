# Elapsed-time incident SLA v1

事故运营分析者按整数UTC秒核响应与解决时长。targets给非负response/resolution seconds。incident最多10000个唯一id，opened整数秒；first_response/resolved可null。响应不能早于opened；解决若有则要求响应存在、且resolved>=response/opened。开放事故可报告已发生的响应SLA，解决时长与判定为null。
pauses最多20000条incident/start/end，必须引用已知事故且end>start。对opened→响应、opened→解决分别将暂停区间裁剪到对应计时范围并按区间并集合并，重叠暂停不重复扣减；响应后暂停只影响解决时钟。有效时间=end-start-并集暂停秒。阈值采用elapsed<=target为满足。暂停原因与授权由调用方保证。
样例I-1墙钟解决4000秒，重叠暂停并集500秒，有效解决3500；响应有效300秒恰满足。所有时间是UTC绝对秒；不计算营业时间、节假日、时区墙钟、优先级升级或法定SLA结论。该计算不是事故管理系统。
