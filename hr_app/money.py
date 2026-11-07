from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MONEY_QUANTUM = Decimal("0.01")


def money(value=0) -> Decimal:
    """Return a currency-safe Decimal rounded to two places."""
    try:
        number = Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("قيمة مالية غير صالحة.") from exc
    if not number.is_finite():
        raise ValueError("القيمة المالية يجب أن تكون رقمًا محدودًا.")
    return number.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def money_float(value=0) -> float:
    """SQLite boundary helper; calculations must happen before this conversion."""
    return float(money(value))
