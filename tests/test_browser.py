from datetime import date

import pytest

from periods import DateRange
from pipeline import run_statement


@pytest.mark.parametrize("hidden", [False, True])
def test_duplicate_inventory_headings_do_not_skip_card(tmp_path, hidden):
    from playwright.sync_api import sync_playwright

    from browser_source import VtbAdapter
    from synthetic import synthetic_cabinet

    with synthetic_cabinet() as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        visits = 0

        def inventory(route):
            nonlocal visits
            visits += 1
            response = route.fetch()
            html = (
                response.text()
                .replace(
                    'data-test-id="debet_card_main_page"',
                    'data-test-id="debet_card_main_page" aria-label="Карта для жизни с последними цифрами 43 21, платёжная система Мир"',
                )
                .replace("Карта ****4321", "4321")
            )
            if visits >= 3:
                extra = "<aside HIDDEN><h2>Карты и счета</h2><h2>Вклады и счета</h2></aside>"
                html = html.replace(
                    "</main>",
                    extra.replace("HIDDEN", "hidden" if hidden else "") + "</main>",
                )
            route.fulfill(response=response, body=html)

        context.route("**/home/all-products", inventory)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        result = run_statement(
            VtbAdapter(page, url, wait_seconds=2),
            DateRange(date(2026, 10, 1), date(2026, 10, 8)),
            tmp_path,
        )
        context.close()
        browser.close()
    assert result["report"]["counts"]["cards"] == 1
    assert result["report"]["counts"]["documents"] == 3
    assert result["cards"][0]["status"] == "complete"


@pytest.mark.parametrize(
    "text,label,expected",
    [
        ("4321", "Карта с последними цифрами 43 21, платёжная система Мир", "4321"),
        ("4321", "Карта с последними цифрами 43\u00a021", "4321"),
        ("Карта ****4321", "", "4321"),
        ("4321", "", None),
        ("100.00 RUB", "Баланс 43 21", None),
        ("4321", "Карта с последними цифрами 43210", None),
    ],
)
def test_card_suffix_requires_explicit_source_evidence(text, label, expected):
    from browser_source import card_suffix

    assert card_suffix(text, label) == expected


def test_conflicting_card_suffixes_are_rejected():
    from adapters import SourceError
    from browser_source import card_suffix

    with pytest.raises(SourceError, match="ambiguous_card_mask_in_source"):
        card_suffix("4321", "Карта с последними цифрами 12 34, Мир")


def test_card_suffix_from_accessible_label_allows_card_pdf(tmp_path):
    from playwright.sync_api import sync_playwright

    from browser_source import VtbAdapter
    from synthetic import synthetic_cabinet

    with synthetic_cabinet() as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)

        def inventory(route):
            response = route.fetch()
            html = (
                response.text()
                .replace(
                    'data-test-id="debet_card_main_page"',
                    'data-test-id="debet_card_main_page" aria-label="Карта для жизни с последними цифрами 43 21, платёжная система Мир"',
                )
                .replace("Карта ****4321", "4321")
            )
            route.fulfill(response=response, body=html)

        context.route("**/home/all-products", inventory)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        result = run_statement(
            VtbAdapter(page, url, wait_seconds=2),
            DateRange(date(2026, 10, 1), date(2026, 10, 8)),
            tmp_path,
        )
        context.close()
        browser.close()
    assert result["report"]["counts"]["documents"] == 3
    assert result["cards"][0]["status"] == "complete"


@pytest.mark.parametrize(
    "operation,detail,expected",
    [
        (
            "Page.goto",
            'Navigation to "https://example.test/PRIVATE_TOKEN" is interrupted by another navigation',
            "navigation_interrupted",
        ),
        ("Page.evaluate", "TypeError: PRIVATE_DATA", "javascript_type_error"),
        (
            "Page.goto",
            "net::ERR_FAILED at https://example.test/PRIVATE_TOKEN",
            "ERR_FAILED",
        ),
        ("Page.goto", "PRIVATE_UNKNOWN_ERROR", "unclassified"),
    ],
)
def test_browser_diagnostics_preserve_signal_without_private_exception_text(
    operation, detail, expected
):
    import json

    from playwright.sync_api import Error as BrowserError

    from adapters import SourceError
    from browser_source import browser_diagnostic

    original = BrowserError(f"{operation}: {detail}\nCall log: PRIVATE_ACCOUNT")
    wrapped = SourceError("readiness", "inventory_browser_action_failed")
    wrapped.__cause__ = original
    diagnostic = browser_diagnostic(wrapped)
    assert diagnostic == {"browser_operation": operation, "browser_error": expected}
    assert "PRIVATE" not in json.dumps(diagnostic)


def test_unknown_inventory_error_is_reported_with_safe_browser_diagnostic(tmp_path):
    from playwright.sync_api import sync_playwright

    from browser_source import VtbAdapter
    from synthetic import synthetic_cabinet

    with synthetic_cabinet() as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        visits = 0

        def inventory(route):
            nonlocal visits
            visits += 1
            if visits >= 2:
                route.abort("failed")
            else:
                route.fulfill(response=route.fetch())

        context.route("**/home/all-products", inventory)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        result = run_statement(
            VtbAdapter(page, url, wait_seconds=2),
            DateRange(date(2026, 10, 1), date(2026, 10, 8)),
            tmp_path,
        )
        context.close()
        browser.close()
    failures = [
        r
        for r in result["report"]["reasons"]
        if r["reason"] == "inventory_browser_action_failed"
    ]
    assert len(failures) == 2
    assert all(
        r["browser_operation"] == "Page.goto" and r["browser_error"] == "ERR_FAILED"
        for r in failures
    )


@pytest.mark.parametrize("persistent", [False, True])
def test_inventory_connection_reset_before_card_is_retried(tmp_path, persistent):
    from playwright.sync_api import sync_playwright

    from browser_source import VtbAdapter
    from synthetic import synthetic_cabinet

    with synthetic_cabinet() as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        visits = 0

        def inventory(route):
            nonlocal visits
            visits += 1
            if visits == 3 or persistent and visits > 3:
                route.abort("connectionreset")
            else:
                route.fulfill(response=route.fetch())

        context.route("**/home/all-products", inventory)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        result = run_statement(
            VtbAdapter(page, url, wait_seconds=2),
            DateRange(date(2026, 10, 1), date(2026, 10, 8)),
            tmp_path,
        )
        context.close()
        browser.close()
    if persistent:
        assert visits == 5  # The failed card-inventory read has only three attempts.
        assert result["report"]["counts"]["cards"] == 0
        assert result["report"]["counts"]["documents"] == 2
        assert any(
            r["reason"] == "network_connection_reset"
            for r in result["report"]["reasons"]
        )
    else:
        assert visits == 4
        assert result["report"]["counts"]["cards"] == 1
        assert result["report"]["counts"]["documents"] == 3
        assert result["cards"][0]["status"] == "complete"


def test_product_navigation_does_not_reload_opened_details(tmp_path):
    from playwright.sync_api import sync_playwright

    from browser_source import VtbAdapter
    from synthetic import synthetic_cabinet

    with synthetic_cabinet() as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        visits = 0

        def details(route):
            nonlocal visits
            visits += 1
            if visits > 1:
                # A redundant reload loses the rendered page on a slow connection.
                route.fulfill(body="<html><body>Загрузка</body></html>")
            else:
                route.fulfill(response=route.fetch())

        context.route("**/details/MasterAccount/master-demo", details)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        adapter = VtbAdapter(page, url, wait_seconds=2)
        page.set_default_timeout(500)
        result = run_statement(
            adapter, DateRange(date(2026, 10, 1), date(2026, 10, 8)), tmp_path
        )
        context.close()
        browser.close()
    assert result["report"]["counts"]["documents"] == 3
    assert visits == 1


@pytest.mark.parametrize("restores_session", [True, False])
def test_inventory_handles_login_redirect_without_abandoning_restored_session(
    restores_session,
):
    from playwright.sync_api import sync_playwright

    from adapters import SessionExpired
    from browser_source import VtbAdapter
    from synthetic import synthetic_cabinet

    with synthetic_cabinet() as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()

        def inventory(route):
            response = route.fetch()
            script = "history.replaceState({}, '', '/login');"
            if restores_session:
                script += "setTimeout(() => history.replaceState({}, '', '/home/all-products'), 200);"
            route.fulfill(
                response=response,
                body=response.text().replace(
                    "</main>", f"<script>{script}</script></main>"
                ),
            )

        context.route("**/home/all-products", inventory)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        adapter = VtbAdapter(page, url)
        page.set_default_navigation_timeout(500)
        if restores_session:
            assert adapter._inventory()
        else:
            with pytest.raises(SessionExpired):
                adapter._inventory()
        context.close()
        browser.close()


@pytest.mark.parametrize("delay_ms", [0, 300])
def test_delayed_inventory_keeps_account_and_card_documents(tmp_path, delay_ms):
    from playwright.sync_api import sync_playwright

    from browser_source import VtbAdapter
    from synthetic import synthetic_cabinet

    with synthetic_cabinet() as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)

        def delay_master(route):
            response = route.fetch()
            html = response.text().replace(
                "</main>",
                """<script>
                const master = document.querySelector('[data-test-id="MASTER_ACCOUNT"]');
                const next = master.nextSibling;
                master.remove();
                setTimeout(() => next.before(master), DELAY);
                </script></main>""".replace("DELAY", str(delay_ms)),
            )
            route.fulfill(response=response, body=html)

        context.route("**/home/all-products", delay_master)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        result = run_statement(
            VtbAdapter(page, url, wait_seconds=2),
            DateRange(date(2026, 10, 1), date(2026, 10, 8)),
            tmp_path,
        )
        context.close()
        browser.close()
    assert result["report"]["counts"]["products"] == 2
    assert result["report"]["counts"]["documents"] == 3
    assert result["report"]["discovery"]["status"] == "uncertain"


@pytest.mark.parametrize("page_wait_seconds,documents", [(1, 1), (3, 3)])
def test_slow_details_use_configured_page_budget(
    tmp_path, page_wait_seconds, documents
):
    from playwright.sync_api import sync_playwright

    from browser_source import VtbAdapter
    from synthetic import synthetic_cabinet

    with synthetic_cabinet() as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)

        def details(route):
            response = route.fetch()
            route.fulfill(
                response=response,
                body=response.text().replace(
                    "</main>",
                    """<script>
                    const main = document.querySelector('main');
                    const content = main.innerHTML;
                    main.innerHTML = '<p>Загрузка</p>';
                    setTimeout(() => main.innerHTML = content, 1500);
                    </script></main>""",
                ),
            )

        context.route("**/details/MasterAccount/master-demo", details)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        result = run_statement(
            VtbAdapter(page, url, wait_seconds=2, page_wait_seconds=page_wait_seconds),
            DateRange(date(2026, 10, 1), date(2026, 10, 8)),
            tmp_path,
        )
        context.close()
        browser.close()
    assert result["report"]["counts"]["documents"] == documents
    if documents == 1:
        assert any(
            r["reason"] == "page_element_timeout" for r in result["report"]["reasons"]
        )


@pytest.mark.parametrize(
    "scenario", ["changed_inventory", "missing_detail_control", "delayed_modal"]
)
def test_discovery_reports_specific_safe_failures(tmp_path, scenario):
    import json

    from playwright.sync_api import sync_playwright

    from browser_source import VtbAdapter
    from synthetic import synthetic_cabinet

    with synthetic_cabinet() as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        visits = 0

        def inventory(route):
            nonlocal visits
            visits += 1
            response = route.fetch()
            html = response.text()
            if scenario == "changed_inventory" and visits > 1:
                html = html.replace("Дебетовый счет", "Изменённый счет")
            if scenario == "delayed_modal":
                html = html.replace(
                    "</main>",
                    """<div id="host-modals-portal"></div><script>
                    setTimeout(() => document.querySelector('#host-modals-portal')
                        .textContent = 'PRIVATE-TEST-LABEL', 200);
                    </script></main>""",
                )
            route.fulfill(response=response, body=html)

        def details(route):
            response = route.fetch()
            route.fulfill(
                response=response,
                body=response.text().replace("Реквизиты счета", "PRIVATE-TEST-LABEL"),
            )

        context.route("**/home/all-products", inventory)
        if scenario == "missing_detail_control":
            context.route("**/details/MasterAccount/master-demo", details)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        adapter = VtbAdapter(page, url, wait_seconds=2)
        page.set_default_timeout(500)
        result = run_statement(
            adapter, DateRange(date(2026, 10, 1), date(2026, 10, 8)), tmp_path
        )
        context.close()
        browser.close()
    reasons = result["report"]["reasons"]
    expected = {
        "changed_inventory": ("discovery", "inventory_changed_during_read"),
        "missing_detail_control": ("product_details", "page_element_timeout"),
        "delayed_modal": ("discovery", "unexpected_modal"),
    }[scenario]
    assert expected in {(r["stage"], r["reason"]) for r in reasons}
    assert result["report"]["status"] == "partial"
    assert "PRIVATE-TEST-LABEL" not in json.dumps(result["report"])
    if scenario == "missing_detail_control":
        assert result["report"]["counts"]["documents"] == 1
        assert any(r.get("family") == "MASTER_ACCOUNT" for r in reasons)


@pytest.mark.parametrize("revoke", [False, True])
@pytest.mark.parametrize("acquisition_only", [False, True])
@pytest.mark.parametrize("cached_creator", [False, True])
def test_savings_blob_pdf_survives_url_revocation(
    tmp_path,
    revoke,
    acquisition_only,
    cached_creator,
    redirect_shell=False,
    cross_realm=False,
):
    from playwright.sync_api import sync_playwright

    from browser_source import VtbAdapter
    from synthetic import synthetic_cabinet

    revoked = []
    with synthetic_cabinet() as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)

        def statement(route):
            response = route.fetch()
            # Headless Chromium downloads PDF viewers automatically. An inline
            # MIME forces this test through the adapter's blob-reader fallback.
            html = response.text().replace(
                "window.URL.createObjectURL(b)",
                "window.URL.createObjectURL(new Blob([b],{type:'text/plain'}))",
            )
            if cross_realm:
                html = html.replace(
                    "new Blob([b]",
                    "new (document.querySelector('#blob-realm').contentWindow.Blob)([b]",
                ).replace(
                    "<main>",
                    '<main><iframe id="blob-realm" src="about:blank"></iframe>',
                )
            if cached_creator:
                html = html.replace(
                    "window.URL.createObjectURL(new Blob",
                    "window.savedCreateBlobURL(new Blob",
                )
                html = html.replace(
                    "</main>",
                    "</main><script>window.savedCreateBlobURL=URL.createObjectURL;</script>",
                )
            route.fulfill(response=response, body=html)

        def pdf(route):
            response = route.fetch()
            route.fulfill(body=response.body(), content_type="application/octet-stream")

        context.route("**/details/SavingsAccount/*/statement", statement)
        context.route("**/generate/savings-demo", pdf)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        if revoke:

            def revoke_blob(popup):
                if "/SavingsAccount/" in page.url:
                    popup.wait_for_url("blob:**", wait_until="domcontentloaded")
                    page.evaluate("url => URL.revokeObjectURL(url)", popup.url)
                    revoked.append(True)

            context.on("page", revoke_blob)
        original_create = page.evaluate("URL.createObjectURL.toString()")
        adapter = VtbAdapter(page, url, wait_seconds=2)
        if redirect_shell:
            # Simulate an SPA shell that caches the browser API before setting
            # its final statement route. Acquisition still uses the real adapter.
            def shell(route):
                response = context.request.get(
                    url + "/details/SavingsAccount/savings-demo/statement"
                )
                html = (
                    response.text()
                    .replace(
                        "window.URL.createObjectURL(b)",
                        "window.savedCreateBlobURL(new Blob([b],{type:'text/plain'}))",
                    )
                    .replace(
                        "</main>",
                        "</main><script>window.savedCreateBlobURL=URL.createObjectURL;"
                        'history.replaceState(null,"",'
                        '"/details/SavingsAccount/savings-demo/statement");</script>',
                    )
                )
                route.fulfill(content_type="text/html; charset=utf-8", body=html)

            context.route("**/statement-shell", shell)
            original_goto = adapter._goto

            def through_shell(route, *, force_reload=False):
                if route == "/details/SavingsAccount/savings-demo/statement":
                    page.goto(url + "/statement-shell")
                    page.wait_for_url(url + route)
                else:
                    original_goto(route, force_reload=force_reload)

            adapter._goto = through_shell
        period = DateRange(date(2026, 10, 1), date(2026, 10, 8))
        if acquisition_only:
            inventory = adapter.discover()
            adapter.acquire_account(
                inventory.products[1], period, tmp_path / "savings.pdf"
            )
        else:
            result = run_statement(adapter, period, tmp_path)
        assert page.evaluate("URL.createObjectURL.toString()") == original_create
        context.close()
        browser.close()

    if revoke:
        assert revoked
    if acquisition_only:
        assert (tmp_path / "savings.pdf").read_bytes().startswith(b"%PDF-")
    else:
        assert result["report"]["counts"]["documents"] == 3
        assert result["products"][1]["status"] == "complete"
        assert not any(
            r["reason"] == "browser_action_failed" for r in result["report"]["reasons"]
        )


def test_savings_blob_cached_before_spa_statement_route(tmp_path):
    test_savings_blob_pdf_survives_url_revocation(tmp_path, True, True, True, True)


def test_savings_blob_created_in_another_javascript_realm(tmp_path):
    test_savings_blob_pdf_survives_url_revocation(
        tmp_path, True, True, True, cross_realm=True
    )


@pytest.mark.parametrize(
    "failure,operation,code",
    [
        ("period_input", "Locator.fill", "ambiguous_locator"),
        ("submit", "Locator.click", "ambiguous_locator"),
    ],
)
def test_savings_acquisition_error_keeps_safe_browser_diagnostic(
    tmp_path, failure, operation, code
):
    import json

    from playwright.sync_api import sync_playwright

    from browser_source import VtbAdapter
    from synthetic import synthetic_cabinet

    with synthetic_cabinet() as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)

        def fail_savings(route):
            response = route.fetch()
            extra = (
                '<input aria-label="Период выписки" value="PRIVATE-ACCOUNT">'
                if failure == "period_input"
                else '<button value="PRIVATE-ACCOUNT">Скачать</button>'
            )
            route.fulfill(
                response=response,
                body=response.text().replace("</main>", extra + "</main>"),
            )

        context.route("**/details/SavingsAccount/*/statement", fail_savings)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        result = run_statement(
            VtbAdapter(page, url, wait_seconds=2),
            DateRange(date(2026, 10, 1), date(2026, 10, 8)),
            tmp_path,
        )
        context.close()
        browser.close()

    errors = [
        r for r in result["report"]["reasons"] if r["reason"] == "browser_action_failed"
    ]
    assert len(errors) == 1
    assert errors[0].get("browser_operation") == operation
    assert errors[0].get("browser_error") == code
    assert errors[0].get("acquisition_step") == failure
    assert result["report"]["counts"]["documents"] == 2
    persisted = (tmp_path / "report.json").read_text()
    assert "PRIVATE" not in persisted
    assert url not in persisted
    assert json.loads(persisted)["reasons"] == result["report"]["reasons"]


@pytest.mark.parametrize("acquisition_only", [True, False])
def test_card_already_ready_opens_history_and_downloads_pdf(tmp_path, acquisition_only):
    from playwright.sync_api import sync_playwright

    from adapters import SourceError
    from browser_source import VtbAdapter
    from synthetic import document_page, synthetic_cabinet

    history_visits = []
    with synthetic_cabinet() as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)

        def already_ready(route):
            # Let the cabinet store the order, then show the existing-document UI.
            route.fetch()
            route.fulfill(
                content_type="text/html; charset=utf-8",
                body=document_page(
                    '<h1>Выписка уже готова</h1><button onclick="'
                    "fetch('/history/card-demo').then(r=>r.text()).then(t=>"
                    "document.querySelector('main').innerHTML=t)"
                    '">К выпискам</button>'
                ),
            )

        def history(route):
            history_visits.append(route.request.url)
            route.fulfill(
                content_type="text/html; charset=utf-8",
                body='<button role="radio" id="ORDERED" aria-checked="true">'
                "Мои выписки</button><button onclick=\"window.open('/viewer/card-demo')\">"
                "Выписка с 01.10.2026 по 08.10.2026 Готова с 08.10.2026</button>",
            )

        context.route("**/generate/card-demo", already_ready)
        context.route("**/history/card-demo", history)
        page = context.new_page()
        page.goto(url + "/login")
        page.get_by_role("button", name="Войти").click()
        adapter = VtbAdapter(page, url, wait_seconds=1)
        period = DateRange(date(2026, 10, 1), date(2026, 10, 8))
        if acquisition_only:
            inventory = adapter.discover()
            card = inventory.cards[0]
            product = next(
                p
                for p in inventory.products
                if p.product_id == card.account_link.value.product_id
            )
            error = None
            try:
                adapter.acquire_card(card, product, period, tmp_path / "card.pdf")
            except SourceError as exc:
                error = exc
        else:
            result = run_statement(adapter, period, tmp_path)
        context.close()
        browser.close()

    assert history_visits, "The ready-card screen must open К выпискам"
    if acquisition_only:
        assert error is None
        assert (tmp_path / "card.pdf").read_bytes().startswith(b"%PDF-")
    else:
        assert result["cards"][0]["status"] == "complete"
        assert result["report"]["counts"]["documents"] == 3


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
