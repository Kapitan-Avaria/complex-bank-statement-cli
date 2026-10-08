from datetime import date

from periods import DateRange
from pipeline import run_statement


def test_browser_orders_pdf_and_exports_all_synthetic_products(tmp_path):
    from playwright.sync_api import sync_playwright

    from browser_source import SyntheticAdapter
    from synthetic import synthetic_cabinet

    with synthetic_cabinet() as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        adapter = SyntheticAdapter(page, url, wait_seconds=2)
        result = run_statement(
            adapter, DateRange(date(2026, 10, 1), date(2026, 10, 8)), tmp_path
        )
        context.close()
        browser.close()
    assert result["report"]["status"] == "full"
    assert len(result["products"]) == 3
    assert len(result["cards"]) == 1
    assert len(result["operations"]) == 3
    assert result["products"][1]["statement"]["pages_processed"] == 2
    assert (
        result["cards"][0]["card"]["account_link"]["value"]["product_id"]
        == result["products"][0]["product"]["product_id"]
    )
    assert (
        result["cards"][0]["card"]["available_balance"]["value"]["value"]["amount"]
        == "75.50"
    )
    assert result["operations"][0]["amount"]["amount"] == "-10.00"


import pytest


@pytest.mark.parametrize(
    "scenario,reason",
    [
        ("download-failure", "pdf_not_received_within_budget"),
        ("card-failure", "pdf_not_received_within_budget"),
        ("malformed", "rejected_rows"),
        ("expiry", "session_expired"),
        ("pending", "generation_pending"),
        ("uncertain", "synthetic_inventory_end_missing"),
    ],
)
def test_browser_failures_preserve_available_data(tmp_path, scenario, reason):
    from playwright.sync_api import sync_playwright

    from browser_source import SyntheticAdapter
    from synthetic import synthetic_cabinet

    with synthetic_cabinet(scenario) as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        result = run_statement(
            SyntheticAdapter(page, url, wait_seconds=1),
            DateRange(date(2026, 10, 1), date(2026, 10, 8)),
            tmp_path,
        )
        context.close()
        browser.close()
    assert result["report"]["status"] == "partial"
    assert reason in {r["reason"] for r in result["report"]["reasons"]}
    assert result["report"]["counts"]["documents"] >= 1
    if scenario == "card-failure":
        assert len(result["operations"]) == 3
        assert result["cards"][0]["status"] == "partial"
    if scenario == "malformed":
        assert len(result["operations"]) == 2
        assert result["report"]["counts"]["rejected_rows"] == 1
    if scenario == "expiry":
        assert result["products"][0]["status"] == "complete"
        assert result["products"][2]["status"] == "partial"


def test_fresh_login_resume_and_changed_account_rejection(tmp_path):
    from playwright.sync_api import sync_playwright

    from browser_source import SyntheticAdapter
    from synthetic import synthetic_cabinet

    results = []
    for scenario, resume in [
        ("complete", False),
        ("complete", True),
        ("identity-mismatch", True),
    ]:
        with synthetic_cabinet(scenario) as url, sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.goto(url + "/login")
            page.get_by_role("button", name="Войти").click()
            results.append(
                run_statement(
                    SyntheticAdapter(page, url, wait_seconds=2),
                    DateRange(date(2026, 10, 1), date(2026, 10, 8)),
                    tmp_path,
                    resume=resume,
                )
            )
            context.close()
            browser.close()
    assert results[1]["report"]["status"] == "full"
    assert [d["document_id"] for d in results[0]["documents"]] == [
        d["document_id"] for d in results[1]["documents"]
    ]
    assert results[2]["report"]["status"] == "partial"
    assert results[2]["products"][0]["statement"] is None
    assert results[2]["products"][1]["status"] == "complete"


def test_pending_generation_resume_reads_history_without_another_order(tmp_path):
    from playwright.sync_api import sync_playwright

    from browser_source import SyntheticAdapter
    from synthetic import synthetic_cabinet

    period = DateRange(date(2026, 10, 1), date(2026, 10, 8))
    with synthetic_cabinet("pending") as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for resume in (False, True):
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.goto(url + "/login")
            page.get_by_role("button", name="Войти").click()
            result = run_statement(
                SyntheticAdapter(page, url, wait_seconds=0.5),
                period,
                tmp_path,
                resume=resume,
            )
            context.close()
        browser.close()
    import json

    progress = json.loads((tmp_path / "progress.json").read_text())
    assert progress["pending_orders"]
    assert result["report"]["reasons"][0]["reason"] == "generation_pending"


def test_disabled_order_button_does_not_create_pending_order(tmp_path):
    import json

    from playwright.sync_api import sync_playwright

    from browser_source import SyntheticAdapter
    from synthetic import synthetic_cabinet

    results = []
    for scenario, resume in [("order-disabled", False), ("complete", True)]:
        with synthetic_cabinet(scenario) as url, sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.goto(url + "/login")
            page.get_by_role("button", name="Войти").click()
            adapter = SyntheticAdapter(page, url, wait_seconds=2)
            page.set_default_timeout(500)
            results.append(
                run_statement(
                    adapter,
                    DateRange(date(2026, 10, 1), date(2026, 10, 8)),
                    tmp_path,
                    resume=resume,
                )
            )
            if not resume:
                assert not json.loads((tmp_path / "progress.json").read_text())[
                    "pending_orders"
                ]
            context.close()
            browser.close()
    assert results[0]["products"][0]["status"] == "partial"
    assert results[1]["report"]["status"] == "full"
