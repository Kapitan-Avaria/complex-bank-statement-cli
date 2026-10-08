import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str


def parse_amount(raw: str) -> Decimal:
    stripped_string = raw.strip()
    normalized_spaces_string = re.sub(r"[\u00a0\u202f]", " ", stripped_string)
    normalized_sign_string = re.sub(r"\u2212", "-", normalized_spaces_string)

    re_sign = r"[+-]?"
    re_integer_part_ordinary = r"[0-9]+"
    re_integer_part_spaced = r"[0-9]{1,3}( [0-9]{3})+"
    re_integer_part = f"(?:({re_integer_part_ordinary})|({re_integer_part_spaced}))"
    re_fractional_part = r"([.,][0-9]+)?"
    re_filter = f"{re_sign}{re_integer_part}{re_fractional_part}"
    if not re.fullmatch(re_filter, normalized_sign_string):
        raise ValueError("The raw string must represent a valid financial value")

    removed_spaces_string = normalized_sign_string.replace(" ", "")
    dotted_string = removed_spaces_string.replace(",", ".")
    decimal_value = Decimal(dotted_string)
    return decimal_value


def amount_to_string(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("The value must be finite")
    return f"{value:f}"


def money_to_dict(value: Money) -> dict[str, str]:
    return {"amount": amount_to_string(value.amount), "currency": value.currency}


def normalize_operation_amount(
    amount: Decimal, direction: Literal["receipt", "expense"]
) -> Decimal:
    if direction not in ("receipt", "expense"):
        raise ValueError("The direction must be 'receipt' or 'expense'")
    if not amount.is_finite():
        raise ValueError("The amount must be finite")
    if direction == "receipt" and amount < 0:
        raise ValueError("The amount of receipt must be non-negative")

    if direction == "expense" and amount > 0:
        return amount.copy_negate()

    return amount
