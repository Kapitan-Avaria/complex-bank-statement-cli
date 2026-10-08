"""Read supported VTB PDF layouts; reject unverified identity and coverage."""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pdfplumber

from money import Money, parse_amount
from operations import Operation, RejectedRow, RowProvenance, normalize_row
from periods import DateRange


@dataclass(frozen=True)
class PdfPage:
    number: int
    text: str
    words: tuple[dict, ...]
    tables: tuple[list, ...]
    image_only: bool = False
    table_columns: tuple[tuple[float, ...], ...] = ()


def read_pages(path: Path) -> tuple[PdfPage, ...]:
    try:
        with pdfplumber.open(path) as pdf:
            content = []
            for index, original in enumerate(pdf.pages):
                page = original.dedupe_chars()
                tables = page.find_tables()
                text = page.extract_text() or ""
                columns = tuple(
                    tuple(
                        sorted({x for cell in table.cells for x in (cell[0], cell[2])})
                    )
                    for table in tables
                )
                content.append(
                    PdfPage(
                        index + 1,
                        text,
                        tuple(page.extract_words()),
                        tuple(table.extract() for table in tables),
                        bool(page.images) and not text.strip(),
                        columns,
                    )
                )
            pages = tuple(content)
    except Exception as exc:
        raise ValueError("unsupported_or_unreadable_pdf") from exc
    if not pages or not any(p.text.strip() for p in pages):
        raise ValueError("unsupported_pdf_without_text")
    return pages


def parse_date(raw: str) -> date:
    from datetime import datetime

    return datetime.strptime(raw.strip(), "%d.%m.%Y").date()


def document_period(text: str) -> DateRange:
    matches = re.findall(
        r"Период выписки\s+(\d{2}\.\d{2}\.\d{4})\s*[-–]\s*(\d{2}\.\d{2}\.\d{4})", text
    )
    if len(set(matches)) != 1:
        raise ValueError("document_period_missing_or_ambiguous")
    start, end = matches[0]
    return DateRange(parse_date(start), parse_date(end))


def read_money(text: str, label: str) -> Money:
    matches = re.findall(
        re.escape(label) + r"\s+([+\-−]?[\d\s\u00a0\u202f]+[.,]\d+)\s+([A-Z]{3})", text
    )
    matches = list(set(matches))
    if len(matches) != 1:
        raise ValueError("summary_field_missing_or_ambiguous")
    amount, currency = matches[0]
    return Money(parse_amount(amount), currency)


@dataclass(frozen=True)
class AccountStatement:
    account_number: str
    currency: str
    period: DateRange
    opening_balance: Money
    closing_balance: Money
    pages_processed: int
    history_confirmed: bool
    operations: tuple[Operation, ...] = ()
    rejected_rows: tuple[RejectedRow, ...] = ()


def parse_account_statement(
    path: Path, expected_account: str, expected_period: DateRange
) -> AccountStatement:
    pages = read_pages(path)
    text = "\n".join(p.text for p in pages)
    matches = re.findall(r"Номер сч[её]та\s+(\S+)\s+\(([A-Z]{3})\)", text)
    if len(set(matches)) != 1 or matches[0][0] != expected_account:
        raise ValueError("account_identity_mismatch_or_missing")
    account, currency = matches[0]
    period = document_period(text)
    if period != expected_period:
        raise ValueError("document_period_mismatch")
    opening = read_money(text, "Баланс на начало периода")
    closing = read_money(text, "Баланс на конец периода")
    if opening.currency != currency or closing.currency != currency:
        raise ValueError("summary_currency_mismatch")
    empty = bool(
        re.search(r"За заданный период операций по сч[её]ту не проводилось", text)
    )
    rows, unsupported = operation_rows(pages)
    operations: list[Operation] = []
    rejected: list[RejectedRow] = [
        RejectedRow(RowProvenance(p.number, 0, ()), "unsupported_image_only_page")
        for p in pages
        if p.image_only
    ]
    rejected.extend(unsupported)
    if empty and rows:
        rejected.append(
            RejectedRow(RowProvenance(1, 0, ()), "empty_history_conflicts_with_rows")
        )
    for row in rows:
        try:
            operations.append(normalize_row(row, currency))
        except ValueError:
            rejected.append(RejectedRow(row, "invalid_date_amount_or_row_layout"))
    return AccountStatement(
        account,
        currency,
        period,
        opening,
        closing,
        len(pages),
        empty or bool(rows),
        tuple(operations),
        tuple(rejected),
    )


def operation_rows(
    pages: tuple[PdfPage, ...],
) -> tuple[tuple[RowProvenance, ...], tuple[RejectedRow, ...]]:
    rows: list[RowProvenance] = []
    rejected: list[RejectedRow] = []
    signature: tuple[float, ...] | None = None
    date_pattern = r"\d{2}\.\d{2}\.\d{4}"
    date_time_pattern = date_pattern + r"(?:\s+\d{2}:\d{2}(?::\d{2})?)?"
    for page in pages:
        covered_date_rows = 0
        for table_index, table in enumerate(page.tables):
            clean_rows = [
                tuple(str(cell or "").strip() for cell in row) for row in table
            ]
            columns = page.table_columns[table_index]
            width = max((len(row) for row in clean_rows), default=0)
            header_columns = [
                " ".join(row[i] for row in clean_rows[:3] if i < len(row))
                for i in range(width)
            ]
            known_header = (
                width in (6, 7)
                and "Дата" in header_columns[0]
                and "Дата" in header_columns[1]
                and "Сумма" in header_columns[2]
                and "Приход" in header_columns[3]
                and "Расход" in header_columns[4]
                and "Описание" in header_columns[5]
            )
            same_columns = (
                signature is not None
                and len(columns) == len(signature)
                and all(abs(a - b) < 1 for a, b in zip(columns, signature))
            )
            if known_header:
                signature = columns
            compatible = known_header or same_columns
            for index, clean in enumerate(clean_rows):
                if not any(clean):
                    continue
                if clean[0].startswith(("Дата", "Операции по")) or (
                    len(clean) >= 5
                    and not clean[0]
                    and not clean[1]
                    and clean[3] == "Приход"
                    and clean[4] == "Расход"
                ):
                    continue
                looks_like_history = len(clean) >= 2 and any(
                    re.search(date_pattern, cell) for cell in clean[:2]
                )
                provenance = RowProvenance(
                    page.number, index + 1, clean, table_index + 1
                )
                if not compatible:
                    if looks_like_history:
                        rejected.append(
                            RejectedRow(provenance, "unsupported_history_layout")
                        )
                        covered_date_rows += 1
                    continue
                if looks_like_history:
                    covered_date_rows += 1
                if (
                    len(clean) >= 6
                    and not clean[0]
                    and not clean[1]
                    and not any(clean[2:5])
                    and rows
                    and any(clean[5:])
                ):
                    previous = rows[-1]
                    if len(previous.cells) == len(clean):
                        combined = tuple(
                            (a + "\n" + b).strip() if b else a
                            for a, b in zip(previous.cells, clean)
                        )
                        rows[-1] = RowProvenance(
                            previous.page,
                            previous.row,
                            combined,
                            previous.table,
                            previous.continuations
                            + ((page.number, table_index + 1, index + 1, clean),),
                        )
                        continue
                rows.append(provenance)
        # Dates outside a recognized geometric table cannot silently disappear.
        text_date_rows = len(
            re.findall(date_time_pattern + r"\s+" + date_time_pattern, page.text)
        )
        opaque_continuation = (
            page.number > 1 and bool(page.text.strip()) and not page.tables
        )
        if text_date_rows > covered_date_rows or opaque_continuation:
            rejected.append(
                RejectedRow(
                    RowProvenance(page.number, 0, ()), "unsupported_history_layout"
                )
            )
    return tuple(rows), tuple(rejected)


@dataclass(frozen=True)
class CardStatement:
    period: DateRange
    available_balance: Money
    pages_processed: int


def parse_card_statement(
    path: Path,
    expected_last_four: str,
    expected_period: DateRange,
    confirmed_link: bool,
) -> CardStatement:
    if not confirmed_link:
        raise ValueError("card_account_link_unconfirmed")
    pages = read_pages(path)
    text = "\n".join(p.text for p in pages)
    if any(p.image_only for p in pages):
        raise ValueError("unsupported_image_only_page")
    period = document_period(text)
    if period != expected_period:
        raise ValueError("document_period_mismatch")
    # Card sample has only the final four digits, never a full account identity.
    header = "\n".join(pages[0].text.split("Период выписки")[0].splitlines())
    if not re.fullmatch(r"\d{4}", expected_last_four) or not re.search(
        r"(?<!\d)" + re.escape(expected_last_four) + r"(?!\d)", header
    ):
        raise ValueError("card_identity_mismatch_or_missing")
    return CardStatement(period, read_money(text, "Доступный остаток"), len(pages))
