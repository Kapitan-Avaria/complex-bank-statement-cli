from datetime import date
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from periods import DateRange


def account_pdf(path: Path, account="SYNTHETIC-001", period="01.10.2026 - 08.10.2026"):
    from synthetic import synthetic_font

    synthetic_font()
    c = canvas.Canvas(str(path))
    c.setFont("SyntheticFont", 11)
    lines = [
        f"Номер счёта {account} (RUB)",
        f"Период выписки {period}",
        "Баланс на начало периода 100.00 RUB",
        "Баланс на конец периода 100,00 RUB",
        "Операции по счету",
        "За заданный период операций по счету не проводилось",
    ]
    for i, line in enumerate(lines):
        c.drawString(30, 790 - i * 25, line)
    c.showPage()
    c.showPage()  # Blank pages count towards processing evidence.
    c.save()
    return path


def test_account_identity_period_and_blank_page(tmp_path):
    from parsing import parse_account_statement

    result = parse_account_statement(
        account_pdf(tmp_path / "account.pdf"),
        "SYNTHETIC-001",
        DateRange(date(2026, 10, 1), date(2026, 10, 8)),
    )
    assert result.account_number == "SYNTHETIC-001"
    assert result.opening_balance.amount.as_tuple().digits == (1, 0, 0, 0, 0)
    assert result.closing_balance.amount == 100
    assert result.pages_processed == 2
    assert result.history_confirmed
    assert result.operations == ()
    with pytest.raises(ValueError, match="identity"):
        parse_account_statement(tmp_path / "account.pdf", "OTHER", result.period)


def history_pdf(path, malformed=False, account="SYNTHETIC-001", description="Оплата"):
    from synthetic import synthetic_font

    synthetic_font()
    c = canvas.Canvas(str(path), pagesize=(840, 600))
    c.setFont("SyntheticFont", 10)
    for i, line in enumerate(
        [
            f"Номер счёта {account} (RUB)",
            "Период выписки 01.10.2026 - 08.10.2026",
            "Баланс на начало периода 100.00 RUB",
            "Баланс на конец периода 80.00 RUB",
        ]
    ):
        c.drawString(20, 570 - i * 20, line)
    xs = [20, 115, 210, 320, 410, 500, 710, 820]
    rows = [
        [
            "Дата операции",
            "Дата обработки",
            "Сумма операции",
            "Приход",
            "Расход",
            "Описание операции",
            "Получатель",
        ],
        [
            "02.10.2026",
            "03.10.2026",
            "10.00 RUB",
            "",
            "10.00 RUB",
            description + "\nПродолжение описания",
            "Магазин",
        ],
        [
            "02.10.2026",
            "03.10.2026",
            "10.00 RUB",
            "",
            "oops" if malformed else "10.00 RUB",
            "Оплата",
            "Магазин",
        ],
    ]
    top = 450
    for r, row in enumerate(rows):
        for j, cell in enumerate(row):
            for k, line in enumerate(cell.splitlines()):
                c.drawString(xs[j] + 3, top - r * 45 - 15 - k * 13, line)
    for x in xs:
        c.line(x, top, x, top - len(rows) * 45)
    for r in range(len(rows) + 1):
        c.line(xs[0], top - r * 45, xs[-1], top - r * 45)
    c.save()
    return path


def test_distinct_rows_and_rejected_amount_preserve_usable_history(tmp_path):
    from parsing import parse_account_statement

    period = DateRange(date(2026, 10, 1), date(2026, 10, 8))
    result = parse_account_statement(
        history_pdf(tmp_path / "history.pdf"), "SYNTHETIC-001", period
    )
    assert len(result.operations) == 2
    assert result.operations[0].amount.amount == -10
    assert result.operations[0].processed_at.value == "2026-10-03"
    assert result.operations[0].occurred_at.value == "2026-10-02"
    assert "Продолжение описания" in result.operations[0].description
    assert result.operations[0].category.status == "absent"
    assert result.operations[0].status.status == "absent"
    result = parse_account_statement(
        history_pdf(tmp_path / "bad.pdf", True), "SYNTHETIC-001", period
    )
    assert len(result.operations) == 1
    assert len(result.rejected_rows) == 1
    assert result.history_confirmed


def test_card_availability_is_separate_and_requires_confirmed_identity(tmp_path):

    from parsing import parse_card_statement
    from synthetic import synthetic_font

    synthetic_font()
    path = tmp_path / "card.pdf"
    c = canvas.Canvas(str(path))
    c.setFont("SyntheticFont", 11)
    for i, line in enumerate(
        [
            "Карта **** 4321",
            "Период выписки 01.10.2026 - 08.10.2026",
            "Доступный остаток 45.5 RUB",
        ]
    ):
        c.drawString(30, 790 - i * 25, line)
    c.save()
    period = DateRange(date(2026, 10, 1), date(2026, 10, 8))
    result = parse_card_statement(path, "4321", period, True)
    assert result.available_balance.amount == 45.5
    assert result.pages_processed == 1
    with pytest.raises(ValueError, match="link"):
        parse_card_statement(path, "4321", period, False)
    with pytest.raises(ValueError, match="identity"):
        parse_card_statement(path, "9876", period, True)


def test_zero_opposite_column_is_not_an_ambiguous_direction(tmp_path):
    from parsing import parse_account_statement

    # Independent supported source layout: both columns are filled, one with zero.
    from synthetic import synthetic_font

    synthetic_font()
    path = tmp_path / "zero.pdf"
    c = canvas.Canvas(str(path), pagesize=(840, 600))
    c.setFont("SyntheticFont", 10)
    for i, line in enumerate(
        [
            "Номер счёта SYNTHETIC-001 (RUB)",
            "Период выписки 01.10.2026 - 08.10.2026",
            "Баланс на начало периода 100.00 RUB",
            "Баланс на конец периода 90.00 RUB",
        ]
    ):
        c.drawString(20, 570 - i * 20, line)
    xs = [20, 115, 210, 320, 410, 500, 710, 820]
    top = 450
    rows = [
        [
            "Дата операции",
            "Дата обработки",
            "Сумма операции",
            "Приход",
            "Расход",
            "Описание операции",
            "Получатель",
        ],
        [
            "02.10.2026",
            "03.10.2026",
            "-10.00 RUB",
            "0.00 RUB",
            "-10.00 RUB",
            "Оплата",
            "Магазин",
        ],
    ]
    for r, row in enumerate(rows):
        for j, cell in enumerate(row):
            c.drawString(xs[j] + 3, top - r * 45 - 15, cell)
    for x in xs:
        c.line(x, top, x, top - 90)
    for r in range(3):
        c.line(xs[0], top - r * 45, xs[-1], top - r * 45)
    c.save()
    result = parse_account_statement(
        path, "SYNTHETIC-001", DateRange(date(2026, 10, 1), date(2026, 10, 8))
    )
    assert len(result.operations) == 1
    assert result.operations[0].amount.amount == -10


def test_image_only_page_cannot_pass_as_blank_history_page(tmp_path):
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    from parsing import parse_account_statement
    from synthetic import synthetic_font

    path = tmp_path / "scanned.pdf"
    c = canvas.Canvas(str(path))
    c.setFont(synthetic_font(), 11)
    for i, line in enumerate(
        [
            "Номер счёта SYNTHETIC-001 (RUB)",
            "Период выписки 01.10.2026 - 08.10.2026",
            "Баланс на начало периода 100.00 RUB",
            "Баланс на конец периода 100.00 RUB",
            "За заданный период операций по счету не проводилось",
        ]
    ):
        c.drawString(20, 790 - i * 20, line)
    c.showPage()
    c.drawImage(
        ImageReader(Image.new("RGB", (20, 20), "black")), 20, 20, width=200, height=200
    )
    c.save()
    result = parse_account_statement(
        path, "SYNTHETIC-001", DateRange(date(2026, 10, 1), date(2026, 10, 8))
    )
    assert len(result.rejected_rows) == 1
    assert result.rejected_rows[0].reason == "unsupported_image_only_page"


def continued_history_pdf(
    path, repeated_header=True, split_page=True, changed_columns=False, plain_text=None
):
    from synthetic import synthetic_font

    c = canvas.Canvas(str(path), pagesize=(840, 600))
    font = synthetic_font()
    c.setFont(font, 10)
    for i, line in enumerate(
        [
            "Номер счёта SYNTHETIC-001 (RUB)",
            "Период выписки 01.10.2026 - 08.10.2026",
            "Баланс на начало периода 100.00 RUB",
            "Баланс на конец периода 80.00 RUB",
        ]
    ):
        c.drawString(20, 570 - i * 20, line)
    xs = [20, 115, 210, 320, 410, 500, 710, 820]
    headers = [
        "Дата операции",
        "Дата обработки",
        "Сумма операции",
        "Приход",
        "Расход",
        "Описание операции",
        "Получатель",
    ]

    def table(top, description, header):
        rows = ([headers] if header else []) + [
            [
                "02.10.2026",
                "03.10.2026",
                "10.00 RUB",
                "",
                "10.00 RUB",
                description,
                "Магазин",
            ]
        ]
        c.setFont(font, 10)
        for r, row in enumerate(rows):
            for j, cell in enumerate(row):
                c.drawString(xs[j] + 3, top - r * 40 - 15, cell)
        for x in xs:
            c.line(x, top, x, top - 40 * len(rows))
        for r in range(len(rows) + 1):
            c.line(xs[0], top - r * 40, xs[-1], top - r * 40)

    table(450, "Первая", True)
    if split_page:
        c.showPage()
    if changed_columns:
        xs[3] += 15
    if plain_text is not None:
        c.setFont(font, 10)
        c.drawString(20, 450, plain_text)
    else:
        table(450 if split_page else 250, "Вторая", repeated_header)
    c.save()
    return path


def test_continuation_without_header_preserves_all_rows(tmp_path):
    from parsing import parse_account_statement

    result = parse_account_statement(
        continued_history_pdf(tmp_path / "continued.pdf", False),
        "SYNTHETIC-001",
        DateRange(date(2026, 10, 1), date(2026, 10, 8)),
    )
    assert [o.description for o in result.operations] == ["Первая", "Вторая"]
    assert not result.rejected_rows


def test_unknown_continuation_geometry_is_reported_instead_of_omitted(tmp_path):
    from parsing import parse_account_statement

    result = parse_account_statement(
        continued_history_pdf(tmp_path / "unknown.pdf", False, changed_columns=True),
        "SYNTHETIC-001",
        DateRange(date(2026, 10, 1), date(2026, 10, 8)),
    )
    assert len(result.operations) == 1
    assert any(r.reason == "unsupported_history_layout" for r in result.rejected_rows)


@pytest.mark.parametrize(
    "raw", ["12 345,6789", "12\u00a0345.6789", "12\u202f345,6789", "−12345.6789"]
)
def test_summary_decimal_strings_preserve_grouping_and_precision(tmp_path, raw):
    from decimal import Decimal

    from parsing import parse_account_statement
    from synthetic import synthetic_font

    path = tmp_path / "decimal.pdf"
    c = canvas.Canvas(str(path))
    c.setFont(synthetic_font(), 11)
    for i, line in enumerate(
        [
            "Номер счёта SYNTHETIC-001 (RUB)",
            "Период выписки 01.10.2026 - 08.10.2026",
            f"Баланс на начало периода {raw} RUB",
            f"Баланс на конец периода {raw} RUB",
            "За заданный период операций по счету не проводилось",
        ]
    ):
        c.drawString(20, 790 - i * 20, line)
    c.save()
    result = parse_account_statement(
        path, "SYNTHETIC-001", DateRange(date(2026, 10, 1), date(2026, 10, 8))
    )
    assert result.opening_balance.amount == Decimal(
        "-12345.6789" if raw.startswith("−") else "12345.6789"
    )


def test_document_period_mismatch_is_rejected(tmp_path):
    from parsing import parse_account_statement

    with pytest.raises(ValueError, match="period_mismatch"):
        parse_account_statement(
            account_pdf(tmp_path / "wrong-period.pdf"),
            "SYNTHETIC-001",
            DateRange(date(2026, 10, 2), date(2026, 10, 8)),
        )


def test_unrecognized_text_only_history_with_times_is_partial(tmp_path):
    from parsing import parse_account_statement

    result = parse_account_statement(
        continued_history_pdf(
            tmp_path / "timed-plain.pdf",
            plain_text="04.10.2026 10:30:00 05.10.2026 12:00:00 20.00 RUB Операция",
        ),
        "SYNTHETIC-001",
        DateRange(date(2026, 10, 1), date(2026, 10, 8)),
    )
    assert len(result.operations) == 1
    assert any(r.reason == "unsupported_history_layout" for r in result.rejected_rows)
