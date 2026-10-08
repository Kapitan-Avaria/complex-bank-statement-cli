"""Common acquisition/parsing/export pipeline, independent of browser selectors."""

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from adapters import DiscoveryResult, SessionExpired, SourceAdapter, SourceError
from fields import FieldValue
from operations import account_number, bind_operations
from parsing import parse_account_statement, parse_card_statement
from periods import DateRange, plan_extraction_for_active_product
from products import CardAvailableBalance
from storage import SCHEMA_VERSION, Checkpoints, atomic_json, json_value


def run_statement(
    adapter: SourceAdapter, requested: DateRange, output: Path, resume: bool = False
) -> dict[str, Any]:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    documents_dir = output / "documents"
    documents_dir.mkdir(exist_ok=True, mode=0o700)
    checkpoints = Checkpoints(output, requested, resume)
    try:
        discovery = adapter.discover()
    except SourceError as exc:
        discovery = DiscoveryResult((), (), "failed", exc.reason)
    except KeyboardInterrupt:
        discovery = DiscoveryResult((), (), "failed", "interrupted")
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "requested_period": json_value(requested),
        "products": [],
        "cards": [],
        "operations": [],
        "documents": [],
    }
    reasons: list[dict] = []
    coverage: list[dict] = []
    stopped = False
    if discovery.status != "complete":
        reasons.append({"stage": "discovery", "reason": discovery.evidence})
    for product in discovery.products:
        plan = plan_extraction_for_active_product(requested, product.opened_on.value)
        entry: dict[str, Any] = {
            "product": json_value(product),
            "status": "partial",
            "extraction_period": json_value(plan.extraction_period),
            "statement": None,
        }
        result["products"].append(entry)
        for name in product.__dataclass_fields__:
            value = getattr(product, name)
            if isinstance(value, FieldValue) and value.status != "known":
                coverage.append(
                    {
                        "product_id": product.product_id,
                        "field": name,
                        "status": value.status,
                        "reason": value.reason,
                    }
                )
                if value.status == "failed":
                    reasons.append(
                        {
                            "product_id": product.product_id,
                            "stage": "metadata",
                            "reason": value.reason,
                        }
                    )
        if plan.status == "not_applicable":
            entry["status"] = "not_applicable"
            continue
        if stopped or plan.extraction_period is None:
            reasons.append(
                {
                    "product_id": product.product_id,
                    "stage": "planning",
                    "reason": "remaining_work_after_interruption"
                    if stopped
                    else "unconfirmed_product_lifetime",
                }
            )
            continue
        number = account_number(product)
        if number is None or product.source_id.value is None:
            reasons.append(
                {
                    "product_id": product.product_id,
                    "stage": "identity",
                    "reason": "unconfirmed_product_identity",
                }
            )
            continue
        binding = {
            "source_id": product.source_id.value,
            "account_number": number,
            "kind": "account_statement",
        }
        key = "account:" + product.product_id
        try:
            doc = checkpoints.document(key, binding, plan.extraction_period)
            if doc is None:
                destination = documents_dir / f"{uuid4().hex}.pdf"
                if checkpoints.order_pending(key, binding, plan.extraction_period):
                    doc = adapter.acquire_account(
                        product,
                        plan.extraction_period,
                        destination,
                        previously_ordered=True,
                    )
                else:
                    doc = adapter.acquire_account(
                        product, plan.extraction_period, destination
                    )
                checkpoints.save(key, binding, doc)
            if (
                doc.product_id != product.product_id
                or doc.kind != "account_statement"
                or doc.period != plan.extraction_period
            ):
                raise ValueError("document_binding_mismatch")
            parsed = parse_account_statement(
                doc.local_path, number, plan.extraction_period
            )
            if product.currency.value != parsed.currency:
                raise ValueError("product_currency_mismatch")
            operations = bind_operations(
                parsed.operations,
                product.product_id,
                doc.document_id,
                discovery.products,
            )
            entry["statement"] = json_value(replace(parsed, operations=()))
            entry["status"] = (
                "complete"
                if parsed.history_confirmed and not parsed.rejected_rows
                else "partial"
            )
            result["operations"].extend(json_value(operations))
            for op in operations:
                for field_name in (
                    "original_amount",
                    "counterparty",
                    "operation_type",
                    "category",
                    "status",
                    "related_product_id",
                ):
                    field = getattr(op, field_name)
                    if field.status != "known":
                        item = {
                            "product_id": product.product_id,
                            "field": "operations." + field_name,
                            "status": field.status,
                            "reason": field.reason,
                        }
                        if item not in coverage:
                            coverage.append(item)
            result["documents"].append(json_value(doc))
            result_path = output / f"result-{len(result['products'])}.json"
            atomic_json(result_path, {"product": entry, "operations": operations})
            checkpoints.save(key, binding, doc, result_path)
            if entry["status"] == "partial":
                reasons.append(
                    {
                        "product_id": product.product_id,
                        "stage": "parsing",
                        "reason": "rejected_rows"
                        if parsed.rejected_rows
                        else "history_coverage_unconfirmed",
                    }
                )
        except (SourceError, ValueError, OSError, KeyboardInterrupt) as exc:
            if isinstance(exc, SourceError) and exc.submitted:
                checkpoints.save_pending(key, binding, plan.extraction_period)
            stage, reason, stop = _failure(exc, "parsing")
            reasons.append(
                {"product_id": product.product_id, "stage": stage, "reason": reason}
            )
            stopped = stopped or stop
        # Every unit exports usable data, including before a later interruption.
        _export(result, discovery, reasons, coverage, output)
    for card in discovery.cards:
        card_entry: dict[str, Any] = {"card": json_value(card), "status": "partial"}
        result["cards"].append(card_entry)
        link = card.account_link.value
        product = next(
            (p for p in discovery.products if link and p.product_id == link.product_id),
            None,
        )
        if product is None:
            reasons.append(
                {
                    "card_id": card.card_id,
                    "stage": "card_link",
                    "reason": "unconfirmed_card_account_link",
                }
            )
            continue
        plan = plan_extraction_for_active_product(requested, product.opened_on.value)
        if plan.status == "not_applicable":
            card_entry["status"] = "not_applicable"
            continue
        number = account_number(product)
        if (
            stopped
            or not plan.extraction_period
            or not number
            or not card.source_id.value
            or not card.masked_number.value
            or not product.source_id.value
        ):
            reasons.append(
                {
                    "card_id": card.card_id,
                    "stage": "card_identity",
                    "reason": "card_identity_or_period_unconfirmed",
                }
            )
            continue
        last_four = card.masked_number.value[-4:]
        # A last-four mask must uniquely identify a currently discovered linked card.
        if (
            sum(
                c.masked_number.value is not None
                and c.masked_number.value[-4:] == last_four
                for c in discovery.cards
            )
            != 1
        ):
            reasons.append(
                {
                    "card_id": card.card_id,
                    "stage": "card_identity",
                    "reason": "ambiguous_card_mask",
                }
            )
            continue
        binding = {
            "source_id": card.source_id.value,
            "product_source_id": product.source_id.value,
            "account_number": number,
            "last_four": last_four,
            "kind": "card_statement",
        }
        key = "card:" + card.card_id
        try:
            doc = checkpoints.document(key, binding, plan.extraction_period)
            if doc is None:
                doc = adapter.acquire_card(
                    card,
                    product,
                    plan.extraction_period,
                    documents_dir / f"{uuid4().hex}.pdf",
                )
                checkpoints.save(key, binding, doc)
            if (
                doc.product_id != product.product_id
                or doc.card_id != card.card_id
                or doc.kind != "card_statement"
                or doc.period != plan.extraction_period
            ):
                raise ValueError("card_document_binding_mismatch")
            parsed_card = parse_card_statement(
                doc.local_path, last_four, plan.extraction_period, True
            )
            if parsed_card.available_balance.currency != product.currency.value:
                raise ValueError("card_currency_mismatch")
            availability = CardAvailableBalance(
                parsed_card.available_balance,
                doc.document_id,
                parsed_card.period,
                doc.acquired_at,
                FieldValue(None, "absent", "balance_as_of_not_in_document"),
            )
            card_entry["card"] = json_value(
                replace(card, available_balance=FieldValue(availability, "known", None))
            )
            card_entry["pages_processed"] = parsed_card.pages_processed
            card_entry["status"] = "complete"
            result["documents"].append(json_value(doc))
            result_path = output / f"card-result-{len(result['cards'])}.json"
            atomic_json(result_path, card_entry)
            checkpoints.save(key, binding, doc, result_path)
            coverage.append(
                {
                    "card_id": card.card_id,
                    "field": "available_balance.as_of",
                    "status": "absent",
                    "reason": "balance_as_of_not_in_document",
                }
            )
        except (SourceError, ValueError, OSError, KeyboardInterrupt) as exc:
            stage, reason, stop = _failure(exc, "card_parsing")
            reasons.append({"card_id": card.card_id, "stage": stage, "reason": reason})
            stopped = stopped or stop
        _export(result, discovery, reasons, coverage, output)
    _export(result, discovery, reasons, coverage, output)
    return result


def _export(
    result: dict,
    discovery: DiscoveryResult,
    reasons: list,
    coverage: list,
    output: Path,
) -> None:
    report = {
        "status": "partial" if reasons else "full",
        "scope": "active_accounts_and_deposits_only; closed_products_excluded",
        "discovery": {"status": discovery.status, "evidence": discovery.evidence},
        "snapshot": "parts_acquired_at_individual_times",
        "date_basis": "bank_document_period; operation_filter_date_unconfirmed",
        "counts": {
            "products": len(result["products"]),
            "cards": len(result["cards"]),
            "operations": len(result["operations"]),
            "documents": len(result["documents"]),
            "rejected_rows": sum(
                len(p["statement"]["rejected_rows"])
                for p in result["products"]
                if p["statement"]
            ),
        },
        "products": [
            {
                "product_id": p["product"]["product_id"],
                "account_history_status": p["status"],
            }
            for p in result["products"]
        ],
        "cards": [
            {"card_id": c["card"]["card_id"], "indicator_status": c["status"]}
            for c in result["cards"]
        ],
        "field_coverage": coverage,
        "reasons": reasons,
    }
    result["report"] = report
    atomic_json(output / "statement.json", result)
    atomic_json(output / "report.json", report)


def _failure(exc: BaseException, default_stage: str) -> tuple[str, str, bool]:
    if isinstance(exc, SourceError):
        return (
            exc.stage,
            exc.reason,
            isinstance(exc, SessionExpired) or exc.reason == "interrupted",
        )
    if isinstance(exc, KeyboardInterrupt):
        return "interruption", "interrupted", True
    return default_stage, "document_validation_or_storage_failed", False
