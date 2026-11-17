# 深度登记 航班时刻运行台

- 规模：中型
- 核心规则：用机场 IANA 时区解析 DST 重叠与跳时，所有航链在 UTC 上校验先后和衔接。
- 使用者与工作流：运行数据员导入航段，确认机场时区，处理当地时刻歧义，构建 UTC 航链并核对转机。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 航段采信 | 导入航段并隔离重复冲突 | POST /api/legs/import | `app/modules/m1_legs/parser.py` `app/modules/m1_legs/keys.py` | `tests/test_m1_legs.py` |
| M2 机场时区 | 查询日期对应的时区版本 | POST /api/airports | `app/modules/m2_airports/registry.py` `app/modules/m2_airports/contracts.py` | `tests/test_m2_airports.py` |
| M3 当地时刻 | 解析偏移、重叠与跳时 | POST /api/local-time/resolve | `app/modules/m3_localtime/resolve.py` `app/modules/m3_localtime/evidence.py` | `tests/test_m3_localtime.py` |
| M4 UTC 航链 | 换算航段并校验先后顺序 | POST /api/itineraries/build | `app/modules/m4_itinerary/builder.py` `app/modules/m4_itinerary/validate.py` | `tests/test_m4_itinerary.py` |
| M5 衔接判定 | 核对航站楼换乘与等待 | POST /api/connections/check | `app/modules/m5_connections/rules.py` `app/modules/m5_connections/windows.py` | `tests/test_m5_connections.py` |
| M6 批次导出 | 核对航段守恒并导出时刻 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` |
| M7 索赔结算 | 登记误机索赔、修订证据、结算与撤回 | POST /api/claims/register 等 | `app/modules/m7_claims/service.py` `app/modules/m7_claims/repository.py` | `tests/test_m7_service.py` `tests/test_m7_api.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-03 | 尾行记录不完整 | 半条航段进入换算 | 完整边界检查后隔离 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_truncated_tail_detected` |
| TRAP-05 | 必填时刻缺失 | 空时间在深层报错 | 采信阶段给原因码和行号 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_missing_time_quarantined` |
| TRAP-07 | 空当地时刻 | 被转换成午夜 | 空值不参与航链 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_missing_time_quarantined` |
| TRAP-09 | 超大外部行程号 | JSON 数字往返丢精度 | 始终按字符串保形 | `app/modules/m1_legs/keys.py` | `tests/test_m1_legs.py::test_external_id_keeps_large_digits` |
| TRAP-11 | 偏移与 Z 混用 | 同一时刻先后次序错误 | 统一到 UTC 后输出 Z | `app/modules/m3_localtime/resolve.py` | `tests/test_m3_localtime.py::test_utc_serializes_with_z` |
| TRAP-12 | DST 重叠与跳时 | 重复小时覆盖或虚构时刻 | 往返验证并显式 fold | `app/modules/m3_localtime/resolve.py` | `tests/test_m3_localtime.py::test_dst_overlap_has_two_candidates` |
| TRAP-13 | 航段乱序 | 输入顺序被当成航程顺序 | 按 UTC 起飞时刻稳定排序 | `app/modules/m4_itinerary/builder.py` | `tests/test_m4_itinerary.py::test_order_by_utc_is_stable` |
| TRAP-14 | 同航段时刻冲突 | 后写覆盖原计划 | 冲突版本进入隔离区 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_conflicting_duplicate_detected` |
| TRAP-23 | 衔接窗口端点 | 结束时刻被重复计入 | 使用半开区间 | `app/modules/m5_connections/windows.py` | `tests/test_m5_connections.py::test_half_open_window_excludes_end` |
| TRAP-25 | 时区版本重叠 | 随机选中一版偏移 | 多个生效版本立即拒绝 | `app/modules/m2_airports/registry.py` | `tests/test_m2_airports.py::test_overlapping_timezone_versions_rejected` |
| TRAP-31 | 已入账凭证被新事实覆盖 | 修订直接改原凭证，历史账目被毁 | 凭证只追加；已结算单按差额生成关联补赔/追偿，原凭证原样保留 | `app/modules/m7_claims/ledger.py` `app/modules/m7_claims/service.py` | `tests/test_m7_service.py::test_settled_revision_up_uses_supplement_voucher` |
| TRAP-32 | 重复登记跨批次重复赔 | 同一误机按批次各赔一次 | 自然键（行程+失接点）唯一约束，重复登记返回原索赔 | `app/modules/m7_claims/identity.py` `app/modules/m7_claims/repository.py` | `tests/test_m7_service.py::test_same_identity_cross_batch_never_duplicates` |
| TRAP-33 | 金额用浮点产生尾差 | 150.005 元舍入争议 | 全链路整数分，非整数金额拒绝 | `app/modules/m7_claims/money.py` | `tests/test_m7_domain.py::test_amounts_must_be_integer_fen` |
| TRAP-34 | 并发修订互相覆盖 | 后写静默覆盖先写事实 | 乐观版本号，旧版本更新 0 行并返回 409 | `app/modules/m7_claims/repository.py` | `tests/test_m7_service.py::test_concurrent_revisions_only_one_wins` |
| TRAP-35 | 入账成功回执丢失 | 客户端重试产生第二笔赔款 | 请求幂等键查回原分录与原回执，重试不重复入账 | `app/modules/m7_claims/service.py` | `tests/test_m7_service.py::test_lost_receipt_retry_returns_same_voucher` |
| TRAP-36 | 同号请求载荷不一致 | 第二次请求被误当成重试吞掉 | 同键异载荷按载荷哈希拒绝（409） | `app/modules/m7_claims/service.py` | `tests/test_m7_service.py::test_same_key_different_payload_rejected` |
| TRAP-37 | 撤回直接删除索赔 | 凭证链断裂、审计无法对账 | 撤回只追加反向分录（已结算）或标记状态（未结算），凭证链保留 | `app/modules/m7_claims/ledger.py` | `tests/test_m7_service.py::test_withdraw_settled_claim_posts_reverse_voucher` |
| TRAP-38 | 修订后衔接恢复正常 | 失接点消失导致身份校验误杀 | 身份按相邻航段绑定而非失接状态，状态可由失接变正常并触发追偿 | `app/modules/m7_claims/service.py` | `tests/test_m7_service.py::test_settled_revision_normal_connection_recovers_full` |
| TRAP-39 | 裸时间被当本地午夜 | 不同偏移的时刻次序错乱 | 无偏移时间直接拒绝，仅在 UTC 上比较，不改时区规则 | `app/modules/m7_claims/models.py` | `tests/test_m7_domain.py::test_naive_timestamp_rejected_no_timezone_rule_change` |

## 对账规则

- 输入航段 = 有效航段 + 冲突航段 + 隔离航段。
- 行程航段 = 可衔接航段 + 不可衔接航段。
- 到达 UTC 必须晚于起飞 UTC，飞行时长不得超过 18 小时。

## 已知不覆盖

- 不计算天气和空管延误，输入只描述计划时刻。

