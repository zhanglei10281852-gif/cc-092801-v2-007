from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.core.errors import ValidationError

CENT = Decimal("0.01")


def to_cents(value: object, *, field: str) -> int:
    """把外部金额（数字或字符串）规范为整数分，拒绝非有限数与多于两位小数。"""
    if isinstance(value, bool):
        raise ValidationError(f"{field} 金额不合法", context={"field": field, "value": value})
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{field} 金额不合法", context={"field": field, "value": value}) from None
    if not amount.is_finite():
        raise ValidationError(f"{field} 金额必须是有限数", context={"field": field, "value": value})
    quantized = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if quantized != amount:
        raise ValidationError(f"{field} 金额最多保留两位小数", context={"field": field, "value": value})
    return int(quantized.scaleb(2))


def yuan(cents: int | None) -> str | None:
    if cents is None:
        return None
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(cents), 100)
    return f"{sign}{whole}.{part:02d}"
