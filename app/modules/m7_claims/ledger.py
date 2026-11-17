"""复式记账凭证。

约定：
- 每条分录带方向（DR 借 / CR 贷）与整数分金额，凭证内借贷必须相等。
- 赔付 DR 赔付支出 / CR 应付赔款；补赔同方向追加，追偿与撤回用反向分录冲销。
- 已入账凭证永不修改、永不删除，修订一律追加新凭证，保留完整凭证链。
"""
from dataclasses import dataclass

DR = "DR"
CR = "CR"

VOUCHER_SETTLEMENT = "SETTLEMENT"      # 首次结算入账
VOUCHER_SUPPLEMENT = "SUPPLEMENT"      # 已结算后应赔上调 -> 关联补赔
VOUCHER_RECOVERY = "RECOVERY"          # 已结算后应赔下调 -> 关联追偿
VOUCHER_WITHDRAWAL = "WITHDRAWAL"      # 撤回修订 -> 反向分录

EXPENSE_ACCOUNT = "PAY_COMPENSATION_EXPENSE"
PAYABLE_ACCOUNT = "PAY_COMPENSATION_PAYABLE"
RECOVERY_ACCOUNT = "COMPENSATION_RECOVERY"


@dataclass(frozen=True)
class EntrySpec:
    account: str
    direction: str
    amount_fen: int


@dataclass(frozen=True)
class VoucherSpec:
    voucher_type: str
    entries: tuple[EntrySpec, ...]
    summary: str
    linked_voucher_no: str | None = None


def _pair(voucher_type: str, amount_fen: int, summary: str,
          linked: str | None = None, reverse: bool = False) -> VoucherSpec:
    if amount_fen <= 0:
        raise ValueError("VOUCHER_AMOUNT_MUST_BE_POSITIVE")
    if reverse:
        entries = (
            EntrySpec(PAYABLE_ACCOUNT, DR, amount_fen),
            EntrySpec(RECOVERY_ACCOUNT if voucher_type == VOUCHER_RECOVERY else EXPENSE_ACCOUNT, CR, amount_fen),
        )
    else:
        entries = (
            EntrySpec(EXPENSE_ACCOUNT, DR, amount_fen),
            EntrySpec(PAYABLE_ACCOUNT, CR, amount_fen),
        )
    return VoucherSpec(voucher_type, entries, summary, linked)


def settlement_voucher(amount_fen: int, summary: str) -> VoucherSpec:
    return _pair(VOUCHER_SETTLEMENT, amount_fen, summary)


def supplement_voucher(amount_fen: int, summary: str, linked: str) -> VoucherSpec:
    return _pair(VOUCHER_SUPPLEMENT, amount_fen, summary, linked)


def recovery_voucher(amount_fen: int, summary: str, linked: str) -> VoucherSpec:
    return _pair(VOUCHER_RECOVERY, amount_fen, summary, linked, reverse=True)


def withdrawal_voucher(amount_fen: int, summary: str, linked: str) -> VoucherSpec:
    return _pair(VOUCHER_WITHDRAWAL, amount_fen, summary, linked, reverse=True)


def voucher_balanced(entries) -> bool:
    debit = sum(e.amount_fen for e in entries if e.direction == DR)
    credit = sum(e.amount_fen for e in entries if e.direction == CR)
    return debit > 0 and debit == credit


def signed_expense_fen(entries) -> int:
    """净入账（赔付支出净额，分）。

    DR 赔付支出为正；CR 赔付支出（冲销）与 CR 追偿收入为负；应付科目不计入。
    分录同时支持 EntrySpec 对象与 sqlite 行字典。
    """
    total = 0
    for entry in entries:
        if hasattr(entry, "direction"):                      # EntrySpec
            direction, account, amount = entry.direction, entry.account, entry.amount_fen
        else:                                                # dict 或 sqlite3.Row
            direction, account, amount = entry["direction"], entry["account"], entry["amount_fen"]
        if direction == DR and account == EXPENSE_ACCOUNT:
            total += amount
        elif direction == CR and account in (EXPENSE_ACCOUNT, RECOVERY_ACCOUNT):
            total -= amount
    return total
