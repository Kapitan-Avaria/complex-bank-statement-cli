"""Common operation values and evidence-based repetition/transfer handling."""

import re
from dataclasses import dataclass, replace
from datetime import datetime

from fields import FieldValue
from money import Money, normalize_operation_amount, parse_amount
from products import Product


def absent(reason: str) -> FieldValue:
    return FieldValue(None, "absent", reason)


@dataclass(frozen=True)
class RowProvenance:
    page: int
    row: int
    cells: tuple[str, ...]
    table: int = 1
    continuations: tuple[tuple[int, int, int, tuple[str, ...]], ...] = ()


@dataclass(frozen=True)
class Operation:
    product_id: str
    document_id: str
    provenance: RowProvenance
    occurred_at: FieldValue[str]
    processed_at: FieldValue[str]
    amount: Money
    original_amount: FieldValue[Money]
    description: str
    counterparty: FieldValue[str]
    operation_type: FieldValue[str]
    category: FieldValue[str]
    status: FieldValue[str]
    related_product_id: FieldValue[str]
    selection_date_basis: str = "bank_document_period; operation_date_basis_unconfirmed"


@dataclass(frozen=True)
class RejectedRow:
    provenance: RowProvenance
    reason: str


def operation_date(raw: str) -> str:
    for fmt in ("%d.%m.%Y", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            value = datetime.strptime(raw.strip(), fmt)
            return value.date().isoformat() if fmt == "%d.%m.%Y" else value.isoformat()
        except ValueError:
            pass
    raise ValueError("ambiguous_operation_date")


def row_money(raw: str, currency: str) -> Money:
    match = re.fullmatch(r"\s*(.*?)\s*([A-Z]{3})?\s*", raw)
    if match is None:
        raise ValueError("ambiguous_operation_amount")
    return Money(parse_amount(match[1]), match[2] or currency)


def normalize_row(row: RowProvenance, currency: str) -> Operation:
    cells = row.cells
    if len(cells) not in (6, 7):
        raise ValueError("unsupported_operation_columns")
    occurred, processed, original, receipt, expense, description = cells[:6]
    incoming = row_money(receipt, currency) if receipt.strip() else None
    outgoing = row_money(expense, currency) if expense.strip() else None
    if incoming and outgoing:
        if incoming.amount == 0 and outgoing.amount != 0:
            incoming = None
        elif outgoing.amount == 0 and incoming.amount != 0:
            outgoing = None
        else:
            raise ValueError("ambiguous_operation_direction")
    amount = incoming or outgoing
    if amount is None:
        raise ValueError("ambiguous_operation_direction")
    if amount.currency != currency:
        raise ValueError("operation_account_currency_mismatch")
    signed = normalize_operation_amount(
        amount.amount, "receipt" if incoming else "expense"
    )
    return Operation(
        "",
        "",
        row,
        FieldValue(operation_date(occurred), "known", None),
        FieldValue(operation_date(processed), "known", None),
        Money(signed, currency),
        FieldValue(row_money(original, currency), "known", None)
        if original.strip()
        else absent("not_in_source"),
        description,
        FieldValue(cells[6], "known", None)
        if len(cells) == 7 and cells[6]
        else absent("not_in_source"),
        absent("not_in_source"),
        absent("not_in_source"),
        absent("not_in_source"),
        absent("no_unambiguous_source_account_reference"),
    )


def account_number(product: Product) -> str | None:
    values = {
        r.value.replace(" ", "")
        for r in product.requisites.value or ()
        if r.name
        in ("Номер счета", "Номер счёта", "Счет получателя в банке получателя")
    }
    return next(iter(values)) if len(values) == 1 else None


def bind_operations(
    operations: tuple[Operation, ...],
    product_id: str,
    document_id: str,
    products: tuple[Product, ...],
) -> tuple[Operation, ...]:
    result = []
    seen: set[tuple[str, int, int, int]] = set()
    for op in operations:
        key = (document_id, op.provenance.page, op.provenance.table, op.provenance.row)
        if key in seen:
            continue  # Same document/page/table/row, never merely equal amounts or descriptions.
        seen.add(key)
        references = set(
            re.findall(
                r"(?<!\d)\d{20}(?!\d)",
                op.description + " " + (op.counterparty.value or ""),
            )
        )
        candidates = [
            p.product_id
            for p in products
            if p.product_id != product_id and account_number(p) in references
        ]
        link = (
            FieldValue(candidates[0], "known", None)
            if len(candidates) == 1
            else absent("no_unambiguous_source_account_reference")
        )
        result.append(
            replace(
                op,
                product_id=product_id,
                document_id=document_id,
                related_product_id=link,
            )
        )
    return tuple(result)
