"""金额规则：索赔域所有金额一律使用整数分（人民币分），禁止浮点。"""

# 终到延误分钟 -> 赔付标准（整数分）。区间为左闭右开，兜底档为 [480, +inf)。
COMPENSATION_TIERS: tuple[tuple[int, int], ...] = (
    (120, 15000),
    (240, 30000),
    (480, 60000),
)
MAX_COMPENSATION_FEN = 60000


def compensation_for_delay(delay_minutes: int) -> int:
    """按终到延误时长计算应赔金额，不足 120 分钟不应赔，返回 0。"""
    if not isinstance(delay_minutes, int) or isinstance(delay_minutes, bool):
        raise TypeError("delay_minutes must be int")
    if delay_minutes < COMPENSATION_TIERS[0][0]:
        return 0
    amount = MAX_COMPENSATION_FEN
    for threshold, tier_amount in COMPENSATION_TIERS:
        if delay_minutes < threshold:
            break
        amount = tier_amount
    return amount


def require_fen(value) -> int:
    """校验对外传入金额为非负整数分。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("AMOUNT_NOT_INTEGER_FEN")
    if value < 0:
        raise ValueError("NEGATIVE_AMOUNT")
    return value
