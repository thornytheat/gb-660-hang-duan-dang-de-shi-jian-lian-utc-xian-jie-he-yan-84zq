# M7 索赔结算数据字典

## 身份与状态

- natural_key（索赔自然键）：`行程标识|失接机场|入境航班|出境航班`，按字符串存储，全局唯一；同一身份跨批次登记只返回原索赔。
- claim_id：`CLM-` + natural_key 的 SHA-256 前 12 位大写，确定性生成，重复输入得到同一索赔号。
- point_index / status：失接点序号与判定结果，`TOO_SHORT`（实际衔接不足 MCT）、`OVERLAP`（时刻倒挂）、`VALID`（正常衔接，不登记/修订后拒付）。
- status（索赔状态）：`OPEN` 未结算、`SETTLED` 已结算、`WITHDRAWN` 已撤回。
- version：乐观版本号，初值 1，每次修订/结算/撤回 +1；写接口必须携带 expected_version，旧版本返回 409 STALE_VERSION。
- denial_reason：拒付原因码，`CONNECTION_NORMAL` 正常衔接、`EVIDENCE_INCOMPLETE` 证据不全；可赔付时为 null。

## 金额

- 全部金额为整数分（fen），非整数或负数直接拒绝（AMOUNT_NOT_INTEGER_FEN / NEGATIVE_AMOUNT）。
- 应赔 due_fen 按终到延误分钟分档：[120,240) -> 15000；[240,480) -> 30000；[480,+∞) -> 60000；不足 120 分钟为 0。
- net_posted_fen 净入账：DR 赔付支出为正，CR 赔付支出冲销与 CR 追偿收入为负，应付科目不计入。
- pending_difference_fen 待处理差额 = due_fen - net_posted_fen；已撤回索赔恒为 0。

## 证据

- 必填两类文档：`AIRLINE_DELAY_CERT` 航司延误证明、`ONWARD_BOARDING_DOC` 后续航段登机/订座凭证；缺类、缺编号或缺签发时间即证据不全。
- 证据修订保留全量历史（claim_revisions），每次修订记录版本、原因、行程快照、证据快照、延误、应赔与拒付原因。

## 凭证链（只追加，永不修改/删除）

- voucher_type：`SETTLEMENT` 首次结算、`SUPPLEMENT` 已结算后上调补赔、`RECOVERY` 已结算后下调/归零追偿、`WITHDRAWAL` 撤回反向分录。
- 每张凭证借贷必相等：赔付 DR `PAY_COMPENSATION_EXPENSE` / CR `PAY_COMPENSATION_PAYABLE`；追偿与撤回反向，追偿 CR `COMPENSATION_RECOVERY`。
- linked_voucher_no：补赔/追偿/撤回凭证关联的原凭证号，形成可追溯凭证链；原凭证金额与分录永远原样保留。
- 未结算单修订不产生凭证，只重算 due_fen；已结算单修订按新旧 due_fen 差额生成关联凭证。

## 幂等

- request_key（请求头 X-Request-Key 或 body.request_key）：同键同载荷返回首次回执，不重复入账；同键异载荷返回 409 IDEMPOTENCY_KEY_CONFLICT。
- 入账成功但回执丢失时，客户端持原 request_key 重试，服务端查回原分录与原回执，不产生第二张凭证。
- 批次结算（settle-batch）同样支持整键重放：已结算条目跳过，重试不重复赔款。

## 时间与航段

- 时间存储为带偏移 ISO 8601，无时区信息的裸时间拒绝（NAIVE_TIME_REJECTED）；仅在 UTC 上比较先后，不修改任何时区规则、不解释 DST。
- 航链按计划起飞 UTC 稳定排序，相邻航段必须首尾同机场（CHAIN_AIRPORT_MISMATCH）且时刻不倒挂（CHAIN_TIME_OVERLAP）。
- 衔接等待为半开区间：实际等待分钟 >= 机场 MCT 即正常；未知机场 MCT 返回 UNKNOWN_AIRPORT 拒绝登记。

## 边界声明

- 系统只生成会计凭证与结算回执，不发生真实支付。
- 不调整任何时区规则；输入偏移是什么就按什么换算到 UTC 比较。
