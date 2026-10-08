import json
from datetime import date, datetime, timezone
from pathlib import Path

from test_parsing import account_pdf

from adapters import DiscoveryResult, SessionExpired, SourceError
from documents import StatementDocument
from fields import FieldValue
from periods import DateRange
from products import Product, Requisite

PERIOD = DateRange(date(2026, 10, 1), date(2026, 10, 8))


def product(identifier="p1", number="SYNTHETIC-001", opened=date(2026, 1, 1)):
    def known(value):
        return FieldValue(value, "known", None)

    def absent():
        return FieldValue(None, "absent", "not_in_source")

    return Product(
        identifier,
        known(identifier),
        known("Synthetic account"),
        known("account"),
        known("***001"),
        known("RUB"),
        absent(),
        absent(),
        known((Requisite("Номер счета", number),)),
        known(opened),
    )


class Cabinet:
    def __init__(self, fail=False, expiry=False):
        self.fail = fail
        self.expiry = expiry
        self.requests = []

    def discover(self):
        return DiscoveryResult(
            (product(), product("p2", "SYNTHETIC-002")),
            (),
            "complete",
            "explicit_synthetic_end",
        )

    def acquire_account(self, p, period, destination):
        self.requests.append(p.product_id)
        if p.product_id == "p2" and self.fail:
            raise SourceError("acquisition", "download_failed")
        if p.product_id == "p2" and self.expiry:
            raise SessionExpired()
        account_pdf(
            destination, "SYNTHETIC-001" if p.product_id == "p1" else "SYNTHETIC-002"
        )
        return StatementDocument(
            p.product_id,
            "account_statement",
            p.product_id,
            None,
            period,
            datetime.now(timezone.utc),
            destination,
        )


def test_run_exports_correct_data_and_reuses_verified_documents(tmp_path):
    from pipeline import run_statement

    cabinet = Cabinet()
    result = run_statement(cabinet, PERIOD, tmp_path)
    assert result["report"]["status"] == "full"
    assert result["report"]["counts"]["products"] == 2
    assert (
        json.loads((tmp_path / "statement.json").read_text())["products"][0][
            "statement"
        ]["opening_balance"]["amount"]
        == "100.00"
    )
    original = result["documents"][0]["acquired_at"]
    resumed = Cabinet()
    result = run_statement(resumed, PERIOD, tmp_path, resume=True)
    assert resumed.requests == []
    assert result["documents"][0]["acquired_at"] == original
    assert result["report"]["snapshot"] == "parts_acquired_at_individual_times"


def test_product_failure_or_expiry_preserves_first_product(tmp_path):
    from pipeline import run_statement

    for label, cabinet in [
        ("download", Cabinet(fail=True)),
        ("session", Cabinet(expiry=True)),
    ]:
        result = run_statement(cabinet, PERIOD, tmp_path / label)
        assert result["report"]["status"] == "partial"
        assert result["products"][0]["status"] == "complete"
        assert result["products"][1]["status"] == "partial"
        assert len(result["documents"]) == 1
        assert result["report"]["reasons"][0]["reason"] in (
            "download_failed",
            "session_expired",
        )


def test_resume_rejects_changed_identity_and_corrupt_document(tmp_path):
    from pipeline import run_statement

    run_statement(Cabinet(), PERIOD, tmp_path)
    data = json.loads((tmp_path / "progress.json").read_text())
    data["documents"]["account:p1"]["binding"]["account_number"] = "OTHER"
    (tmp_path / "progress.json").write_text(json.dumps(data))
    resumed = Cabinet()
    result = run_statement(resumed, PERIOD, tmp_path, resume=True)
    assert resumed.requests == ["p1"]
    assert result["report"]["status"] == "full"
    Path(result["documents"][0]["local_path"]).write_bytes(b"broken")
    resumed = Cabinet()
    run_statement(resumed, PERIOD, tmp_path, resume=True)
    assert resumed.requests == ["p1"]


def test_outside_product_lifetime_skips_acquisition(tmp_path):
    from pipeline import run_statement

    class Later(Cabinet):
        def discover(self):
            return DiscoveryResult(
                (product(opened=date(2026, 10, 9)),), (), "complete", "explicit_end"
            )

    source = Later()
    result = run_statement(source, PERIOD, tmp_path)
    assert source.requests == []
    assert result["products"][0]["status"] == "not_applicable"
    assert result["report"]["status"] == "full"


def test_corrupt_checkpoint_schema_is_not_trusted(tmp_path):
    from pipeline import run_statement

    (tmp_path / "progress.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "requested_period": {"start": "2026-10-01", "end": "2026-10-08"},
                "documents": None,
            }
        )
    )
    source = Cabinet()
    result = run_statement(source, PERIOD, tmp_path, resume=True)
    assert source.requests == ["p1", "p2"]
    assert result["report"]["status"] == "full"


def test_transfer_link_uses_full_source_account_evidence_only(tmp_path):
    from test_parsing import history_pdf

    from pipeline import run_statement

    class Transfers(Cabinet):
        def discover(self):
            return DiscoveryResult(
                (
                    product("p1", "40817810000000000001"),
                    product("p2", "40817810000000000002"),
                ),
                (),
                "complete",
                "explicit_end",
            )

        def acquire_account(self, p, period, destination):
            if p.product_id == "p1":
                history_pdf(
                    destination,
                    account="40817810000000000001",
                    description="Перевод 40817810000000000002",
                )
            else:
                account_pdf(destination, "40817810000000000002")
            return StatementDocument(
                p.product_id,
                "account_statement",
                p.product_id,
                None,
                period,
                datetime.now(timezone.utc),
                destination,
            )

    result = run_statement(Transfers(), PERIOD, tmp_path)
    assert result["operations"][0]["related_product_id"]["value"] == "p2"
    assert result["operations"][1]["related_product_id"]["value"] is None
    assert len(result["operations"]) == 2


def test_rows_in_distinct_tables_are_not_technical_duplicates(tmp_path):
    from test_parsing import continued_history_pdf

    from pipeline import run_statement

    class MultipleTables(Cabinet):
        def discover(self):
            return DiscoveryResult((product(),), (), "complete", "explicit_end")

        def acquire_account(self, p, period, destination):
            continued_history_pdf(destination, split_page=False)
            return StatementDocument(
                "doc",
                "account_statement",
                p.product_id,
                None,
                period,
                datetime.now(timezone.utc),
                destination,
            )

    result = run_statement(MultipleTables(), PERIOD, tmp_path)
    assert [o["description"] for o in result["operations"]] == ["Первая", "Вторая"]
