"""Known VTB routes; synthetic-only deposit and completion contracts are isolated."""

import base64
import hashlib
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

from playwright.sync_api import Download, Page, Response
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import TimeoutError as BrowserTimeout

from adapters import DiscoveryFailure, DiscoveryResult, SessionExpired, SourceError
from documents import StatementDocument
from fields import FieldValue
from money import Money, parse_amount
from operations import account_number
from parsing import parse_date
from periods import DateRange
from products import BalanceReading, Card, CardAccountLink, Product, Requisite


def known(value):
    return FieldValue(value, "known", None)


def missing(reason: str, failed: bool = False):
    return FieldValue(None, "failed" if failed else "absent", reason)


def local_id(kind: str, source_id: str) -> str:
    return kind + "-" + hashlib.sha256(source_id.encode()).hexdigest()[:16]


def period_input(period: DateRange) -> str:
    return f"{period.start:%d.%m.%Y} – {period.end:%d.%m.%Y}"


def card_suffix(text: str, accessible_label: str) -> str | None:
    suffixes = set(re.findall(r"[*•·]+\s*(\d{4})(?!\d)", text))
    labelled = re.findall(
        r"с последними цифрами\s+(\d{2}\s*\d{2})(?=\s*[,.;]|$)",
        accessible_label,
        re.IGNORECASE,
    )
    suffixes.update(re.sub(r"\s", "", value) for value in labelled)
    # The observed VTB control displays the suffix alone, without mask symbols.
    # Use bare digits only to cross-check the explicitly labelled suffix.
    if labelled and re.fullmatch(r"\s*\d{4}\s*", text):
        suffixes.add(text.strip())
    if len(suffixes) > 1:
        raise SourceError("card_identity", "ambiguous_card_mask_in_source")
    return next(iter(suffixes), None)


def transient_browser_failure(exc: BrowserError) -> str | None:
    # Match fixed signatures only. Never persist raw exception text or URLs.
    signatures = {
        "net::ERR_CONNECTION_RESET": "network_connection_reset",
        "net::ERR_CONNECTION_CLOSED": "network_connection_closed",
        "net::ERR_INTERNET_DISCONNECTED": "network_disconnected",
        "net::ERR_TIMED_OUT": "network_timeout",
        "net::ERR_ABORTED": "navigation_interrupted",
        "Navigation interrupted": "navigation_interrupted",
        "Execution context was destroyed": "page_context_changed",
    }
    return next(
        (reason for signature, reason in signatures.items() if signature in str(exc)),
        None,
    )


def browser_diagnostic(exc: Exception) -> dict[str, str | None]:
    while not isinstance(exc, BrowserError) and isinstance(exc.__cause__, Exception):
        exc = exc.__cause__
    if not isinstance(exc, BrowserError):
        return {"browser_operation": None, "browser_error": None}
    message = str(exc)
    operations = (
        "Page.goto",
        "Page.wait_for_url",
        "Page.evaluate",
        "Page.wait_for_timeout",
        "Locator.wait_for",
        "Locator.inner_text",
        "Locator.count",
        "Locator.click",
        "Locator.fill",
        "Locator.input_value",
        "Locator.is_visible",
        "Download.save_as",
        "Response.body",
    )
    operation = next(
        (name for name in operations if message.startswith(name + ":")), "unknown"
    )
    codes = (
        "ERR_FAILED",
        "ERR_NAME_NOT_RESOLVED",
        "ERR_HTTP_RESPONSE_CODE_FAILURE",
        "ERR_BLOCKED_BY_CLIENT",
        "ERR_CERT_AUTHORITY_INVALID",
        "ERR_CONNECTION_RESET",
        "ERR_CONNECTION_CLOSED",
        "ERR_INTERNET_DISCONNECTED",
        "ERR_TIMED_OUT",
        "ERR_ABORTED",
    )
    code = next((name for name in codes if "net::" + name in message), None)
    signatures = {
        "is interrupted by another navigation": "navigation_interrupted",
        "Execution context was destroyed": "page_context_changed",
        "Cannot find context with specified id": "page_context_missing",
        "Target page, context or browser has been closed": "target_closed",
        "strict mode violation": "ambiguous_locator",
        "Element is not attached": "element_detached",
        "TypeError:": "javascript_type_error",
        "ReferenceError:": "javascript_reference_error",
    }
    if code is None:
        code = next(
            (name for signature, name in signatures.items() if signature in message),
            None,
        )
    if code is None:
        code = "timeout" if isinstance(exc, BrowserTimeout) else "unclassified"
    return {"browser_operation": operation, "browser_error": code}


class VtbAdapter:
    inventory_families = (
        "MASTER_ACCOUNT",
        "saving_account_savings",
        "debet_card_main_page",
    )

    def __init__(
        self,
        page: Page,
        base_url: str = "https://online.vtb.ru",
        wait_seconds: float = 60,
        page_wait_seconds: float = 60,
    ) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.wait_seconds = wait_seconds
        self.page_wait_seconds = page_wait_seconds
        self.page.set_default_timeout(page_wait_seconds * 1000)
        self.page.set_default_navigation_timeout(page_wait_seconds * 1000)
        self.routes: dict[str, str] = {}
        self.card_routes: dict[str, str] = {}
        self._blob_capture_key = "__vtb_statement_blob_" + uuid4().hex
        # Install before the bank's scripts can cache createObjectURL. Capture is
        # enabled only while acquiring a savings statement, then released.
        self._blob_capture_script = """(() => {
            const key = 'CAPTURE_KEY';
            if (window[key]) return;
            const original = URL.createObjectURL;
            const capture = {original, blobs: new Map(), active: false, calls: 0};
            capture.wrapped = function(blob) {
                const url = original.call(this, blob);
                if (capture.active) capture.calls++;
                // Blob identity must also work for objects from another frame.
                if (capture.active && Object.prototype.toString.call(blob) === '[object Blob]')
                    capture.blobs.set(url, blob);
                return url;
            };
            window[key] = capture;
            URL.createObjectURL = capture.wrapped;
        })()""".replace("CAPTURE_KEY", self._blob_capture_key)
        self.page.add_init_script(self._blob_capture_script)

    def _check_session(self) -> None:
        if urlparse(self.page.url).path.startswith("/login"):
            try:
                self.page.wait_for_url(
                    lambda url: not urlparse(url).path.startswith("/login"),
                    wait_until="domcontentloaded",
                )
            except BrowserTimeout as exc:
                raise SessionExpired() from exc

    def _goto(self, route: str, *, force_reload: bool = False) -> None:
        target = self.base_url + route
        try:
            # Discovery already clicked into details. Reloading that route again
            # discards the SPA's rendered state and repeats authentication work.
            if (
                force_reload
                or self.page.url.split("?", 1)[0].split("#", 1)[0] != target
            ):
                self.page.goto(target, wait_until="domcontentloaded")
            self.page.wait_for_url(
                lambda url: url.split("?", 1)[0].split("#", 1)[0] == target,
                wait_until="domcontentloaded",
            )
        except BrowserTimeout as exc:
            if urlparse(self.page.url).path.startswith("/login"):
                raise SessionExpired() from exc
            raise SourceError("navigation", "page_navigation_timeout") from exc
        portal = self.page.locator("#host-modals-portal")
        if portal.count() and portal.inner_text().strip():
            raise SourceError("readiness", "unexpected_modal")

    def _inventory(self) -> str:
        for attempt in range(3):
            try:
                return self._read_inventory(force_reload=attempt > 0)
            except BrowserError as exc:
                self._check_session()
                reason = transient_browser_failure(exc)
                if reason is None:
                    raise SourceError(
                        "readiness", "inventory_browser_action_failed"
                    ) from exc
                if attempt == 2:
                    raise SourceError("readiness", reason) from exc
                self.page.wait_for_timeout(250 * (attempt + 1))
        raise AssertionError("Unreachable inventory retry state")

    def _read_inventory(self, *, force_reload: bool = False) -> str:
        self._goto("/home/all-products", force_reload=force_reload)
        try:
            self.page.get_by_text("Карты и счета", exact=True).filter(
                visible=True
            ).first.wait_for()
            self.page.get_by_text("Вклады и счета", exact=True).filter(
                visible=True
            ).first.wait_for()
        except BrowserTimeout as exc:
            self._check_session()
            raise SourceError("readiness", "inventory_sections_timeout") from exc
        # Section headings can render before the product controls. A quiet window
        # prevents reading an intermediate list; it is not proof of completeness.
        deadline = time.monotonic() + self.page_wait_seconds
        previous = None
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            self._check_session()
            portal = self.page.locator("#host-modals-portal")
            if portal.count() and portal.inner_text().strip():
                raise SourceError("readiness", "unexpected_modal")
            snapshot = self.page.evaluate(
                """families => {
                    const groups = families.map(family =>
                        Array.from(document.querySelectorAll(`[data-test-id="${family}"]`))
                            .map(node => [node.getAttribute('data-id'), node.textContent,
                                node.getAttribute('aria-label')]));
                    return groups.some(group => group.length)
                        ? JSON.stringify(groups) : null;
                }""",
                self.inventory_families,
            )
            now = time.monotonic()
            if snapshot != previous or snapshot is None:
                previous = snapshot
                stable_since = now
            elif now - stable_since >= 0.5:
                return snapshot
            self.page.wait_for_timeout(100)
        raise SourceError("readiness", "inventory_not_stable_within_budget")

    def _completion(self) -> tuple[Literal["complete", "uncertain"], str]:
        return "uncertain", "vtb_inventory_end_not_confirmed"

    def _requisites(self, route: str) -> tuple[Requisite, ...]:
        self._goto(route)
        label = self.page.get_by_text("Дата открытия", exact=True)
        label.wait_for()
        values = self.page.locator("div").evaluate_all("""nodes => nodes.filter(n => {
            const children = Array.from(n.children);
            return children.length === 2 && children.every(c => c.tagName === 'SPAN');
        }).map(n => Array.from(n.children).map(c => c.textContent.trim()))""")
        recognized = {
            "Счет получателя в банке получателя",
            "Номер счета",
            "Дата открытия",
            "Валюта счета",
            "Наименование Банка получателя",
            "Банк получателя",
            "БИК Банка получателя",
            "БИК",
            "ИНН Банка получателя",
            "ИНН",
            "КПП Банка получателя",
            "КПП",
            "К/С Банка получателя",
            "Корр. счет",
        }
        # Recipient names and other personal/profile values are not required.
        rows = tuple(Requisite(k, v) for k, v in values if k in recognized)
        for name in {r.name for r in rows}:
            if sum(r.name == name for r in rows) != 1:
                raise SourceError("requisites", "ambiguous_requisite_labels")
        return rows

    def _product(self, route: str, label: str) -> Product:
        parts = route.strip("/").split("/")
        if (
            len(parts) != 3
            or parts[:1] != ["details"]
            or parts[1] not in ("MasterAccount", "SavingsAccount", "TermDeposit")
        ):
            raise SourceError("discovery", "unrecognized_product_route")
        kind, reference = parts[1:]
        self._goto(route)
        self.page.get_by_role("button", name=re.compile(r"^Реквизиты счета")).wait_for()
        current = missing("savings_header_balance_semantics_unconfirmed")
        currency = missing("currency_unread", True)
        if kind == "MasterAccount":
            try:
                container = self.page.locator('[aria-label^="Баланс"]')
                container.wait_for(state="attached")
                if container.count() != 1:
                    raise ValueError("balance_container_not_unique")
                raw = container.inner_text().replace("\n", "")
                match = re.fullmatch(
                    r"\s*([+\-−]?[\d\s\u00a0\u202f]+[.,]\d+)\s*(₽|RUB)\s*", raw
                )
                if not match:
                    raise ValueError("balance_format_unrecognized")
                current = known(
                    BalanceReading(
                        Money(parse_amount(match[1]), "RUB"),
                        datetime.now(UTC),
                        "VTB account details: Баланс",
                    )
                )
                currency = known("RUB")
            except (ValueError, BrowserError):
                current = missing("current_balance_read_failed", True)
        requisites = missing("requisites_read_failed", True)
        opened = missing("opening_date_read_failed", True)
        suffix = "informationAndRequisites" if kind == "MasterAccount" else "requisites"
        try:
            rows = self._requisites(route + "/" + suffix)
            requisites = known(rows)
            dates = [r.value for r in rows if r.name == "Дата открытия"]
            if len(dates) == 1:
                opened = known(parse_date(dates[0]))
            currencies = [r.value for r in rows if r.name == "Валюта счета"]
            if currencies:
                currency = (
                    known("RUB")
                    if currencies == ["Российский рубль"] or currencies == ["RUB"]
                    else missing("unsupported_currency", True)
                )
        except SessionExpired:
            raise
        except (SourceError, BrowserError, ValueError):
            self._check_session()
        masked = re.search(r"[*•·]+\s*(\d{4})", label)
        product_id = local_id("product", reference)
        self.routes[product_id] = route
        return Product(
            product_id,
            known(reference),
            known(label),
            known(kind),
            known("***" + masked[1]) if masked else missing("mask_not_in_source"),
            currency,
            current,
            missing("no_separately_labelled_available_balance_in_observed_details"),
            requisites,
            opened,
        )

    def discover(self) -> DiscoveryResult:
        products: list[Product] = []
        cards: list[Card] = []
        errors = False
        failures: list[DiscoveryFailure] = []
        try:
            inventory = self._inventory()
            status, evidence = self._completion()
            families = [
                (family, self.page.locator(f'[data-test-id="{family}"]').count())
                for family in self.inventory_families
            ]
            for family, count in families:
                for index in range(count):
                    stage = "inventory_read"
                    try:
                        # The first control is already on the verified inventory.
                        current_inventory = (
                            inventory
                            if not products and not cards and not failures
                            else self._inventory()
                        )
                        controls = self.page.locator(f'[data-test-id="{family}"]')
                        if current_inventory != inventory or controls.count() != count:
                            raise SourceError(
                                "discovery", "inventory_changed_during_read"
                            )
                        control = controls.nth(index)
                        label = control.inner_text()
                        accessible_label = control.get_attribute("aria-label") or ""
                        stage = "product_navigation"
                        control.click()
                        self.page.wait_for_url(re.compile(r".*/details/[^/]+/[^/]+$"))
                        self._check_session()
                        route = urlparse(self.page.url).path
                        stage = "product_details"
                        if family != "debet_card_main_page":
                            products.append(self._product(route, label))
                        else:
                            if not route.startswith("/details/DebitCard/"):
                                raise SourceError(
                                    "card_discovery", "unrecognized_card_route"
                                )
                            reference = route.split("/")[-1]
                            card_id = local_id("card", reference)
                            self.card_routes[card_id] = (
                                "/details/MasterAccountCard/" + reference
                            )
                            suffix = card_suffix(label, accessible_label)
                            cards.append(
                                Card(
                                    card_id,
                                    known(reference),
                                    known("****" + suffix)
                                    if suffix
                                    else missing("card_mask_unread", True),
                                    missing("card_link_unconfirmed", True),
                                    missing("card_document_not_read", True),
                                )
                            )
                    except SessionExpired:
                        raise
                    except (SourceError, BrowserError, ValueError) as exc:
                        errors = True
                        failures.append(
                            DiscoveryFailure(
                                family,
                                index,
                                exc.stage if isinstance(exc, SourceError) else stage,
                                exc.reason
                                if isinstance(exc, SourceError)
                                else "page_element_timeout"
                                if isinstance(exc, BrowserTimeout)
                                else "browser_action_failed"
                                if isinstance(exc, BrowserError)
                                else "invalid_product_metadata",
                                **browser_diagnostic(exc),
                            )
                        )
            for index, card in enumerate(cards):
                try:
                    rows = self._requisites(
                        self.card_routes[card.card_id] + "/informationAndRequisites"
                    )
                    numbers = {
                        r.value.replace(" ", "")
                        for r in rows
                        if r.name == "Счет получателя в банке получателя"
                    }
                    matches = [p for p in products if account_number(p) in numbers]
                    link = (
                        known(
                            CardAccountLink(
                                matches[0].product_id,
                                "fresh_account_and_card_requisites_full_number_match",
                            )
                        )
                        if len(matches) == 1
                        else missing("card_account_match_missing_or_ambiguous", True)
                    )
                    cards[index] = replace(card, account_link=link)
                except SessionExpired:
                    raise
                except (SourceError, BrowserError, ValueError) as exc:
                    self._check_session()
                    errors = True
                    failures.append(
                        DiscoveryFailure(
                            "debet_card_main_page",
                            index,
                            exc.stage
                            if isinstance(exc, SourceError)
                            else "card_requisites",
                            exc.reason
                            if isinstance(exc, SourceError)
                            else "page_element_timeout"
                            if isinstance(exc, BrowserTimeout)
                            else "browser_action_failed"
                            if isinstance(exc, BrowserError)
                            else "invalid_card_metadata",
                            **browser_diagnostic(exc),
                        )
                    )
            return DiscoveryResult(
                tuple(products),
                tuple(cards),
                "failed" if errors else status,
                "metadata_discovery_incomplete" if errors else evidence,
                tuple(failures),
            )
        except (SourceError, BrowserError) as exc:
            reason = (
                exc.reason if isinstance(exc, SourceError) else "inventory_read_failed"
            )
            failures.append(
                DiscoveryFailure(
                    "inventory",
                    0,
                    exc.stage if isinstance(exc, SourceError) else "discovery",
                    reason,
                    **browser_diagnostic(exc),
                )
            )
            return DiscoveryResult(
                tuple(products), tuple(cards), "failed", reason, tuple(failures)
            )

    def acquire_account(
        self,
        product: Product,
        period: DateRange,
        destination: Path,
        *,
        previously_ordered: bool = False,
    ) -> StatementDocument:
        route = self.routes.get(product.product_id)
        if not route:
            raise SourceError("acquisition", "product_route_unconfirmed")
        savings = "/SavingsAccount/" in route
        path = route + ("/statement" if savings else "/statement/start")
        return self._acquire(
            path,
            "Период выписки" if savings else "Период",
            "Скачать" if savings else "Заказать",
            period,
            destination,
            product.product_id,
            None,
            previously_ordered,
        )

    def acquire_card(
        self, card: Card, product: Product, period: DateRange, destination: Path
    ) -> StatementDocument:
        route = self.card_routes.get(card.card_id)
        if (
            not route
            or not card.account_link.value
            or card.account_link.value.product_id != product.product_id
        ):
            raise SourceError("card_acquisition", "card_link_or_route_unconfirmed")
        return self._acquire(
            route + "/statement/start",
            "Период",
            "Заказать",
            period,
            destination,
            product.product_id,
            card.card_id,
        )

    def _acquire(
        self,
        route: str,
        label: str,
        action: str,
        period: DateRange,
        destination: Path,
        product_id: str,
        card_id: str | None,
        previously_ordered: bool = False,
    ) -> StatementDocument:
        downloads: list[Download] = []
        new_pages: list[Page] = []
        login_responses: list[Response] = []
        pdf_responses: list[Response] = []
        blob_capture = None
        blob_read_diagnostics: dict[str, str] = {}

        def downloaded(download: Download):
            downloads.append(download)

        def attach(page: Page):
            new_pages.append(page)
            page.on("download", downloaded)

        def response(response: Response):
            if urlparse(response.url).path.startswith("/login"):
                login_responses.append(response)
            elif (
                response.ok
                and response.request.resource_type in ("fetch", "xhr")
                and "application/pdf" in response.headers.get("content-type", "")
            ):
                pdf_responses.append(response)

        submitted = previously_ordered
        master = not card_id and "/MasterAccount/" in route
        context = self.page.context
        self.page.on("download", downloaded)
        context.on("page", attach)
        context.on("response", response)
        step = "navigation"
        try:
            self._goto(route)
            history_opened = False
            if previously_ordered and master:
                self.page.get_by_role("radio", name="Мои выписки", exact=True).click()
                history_opened = True
            else:
                if master:
                    new_order = self.page.get_by_role(
                        "radio", name="Заказать новую", exact=True
                    )
                    if new_order.count():
                        new_order.click()
                step = "period_input"
                field = self.page.get_by_label(label, exact=True)
                field.fill(period_input(period))
                if field.input_value() != period_input(period):
                    raise SourceError("period_input", "period_readback_mismatch")
                if "/SavingsAccount/" in route:
                    # Also cover a page already loaded before adapter creation.
                    self.page.evaluate(self._blob_capture_script)
                    blob_capture = self.page.evaluate_handle(
                        """key => {
                            const capture = window[key];
                            capture.blobs.clear();
                            capture.calls = 0;
                            capture.active = true;
                            URL.createObjectURL = capture.wrapped;
                            return capture;
                        }""",
                        self._blob_capture_key,
                    )
                step = "submit"
                self.page.get_by_role("button", name=action, exact=True).click()
                submitted = master
            step = "document_wait"
            deadline = time.monotonic() + self.wait_seconds
            ready_clicked = False
            next_refresh = 0.0
            while time.monotonic() < deadline:
                if login_responses:
                    raise SessionExpired()
                self._check_session()
                if downloads:
                    downloads[0].save_as(destination)
                    if destination.stat().st_size:
                        break
                    downloads.pop(0)
                if pdf_responses:
                    try:
                        payload = pdf_responses.pop(0).body()
                        if payload:
                            destination.write_bytes(payload)
                            break
                    except BrowserError:
                        pass
                blob = next(
                    (p.url for p in new_pages if p.url.startswith("blob:")), None
                )
                if blob:
                    step = "blob_read"
                    captured = blob_capture is not None and blob_capture.evaluate(
                        "(capture, url) => capture.blobs.has(url)", blob
                    )
                    blob_read_diagnostics["blob_captured"] = (
                        "true" if captured else "false"
                    )
                    if blob_capture is not None:
                        blob_read_diagnostics["blob_capture_calls"] = str(
                            blob_capture.evaluate("capture => capture.calls")
                        )
                    blob_read_diagnostics["blob_frame_count"] = str(
                        len(self.page.frames)
                    )
                    same_origin = self.page.evaluate(
                        "url => new URL(url.slice(5)).origin === location.origin", blob
                    )
                    blob_read_diagnostics["blob_origin_matches_page"] = (
                        "true" if same_origin else "false"
                    )
                    encoded = self.page.evaluate(
                        """async ({url, capture}) => {
                        let blob = capture?.blobs.get(url);
                        if (!blob) {
                            const response = await fetch(url);
                            if (!response.ok) throw Error('blob_read_failed');
                            blob = await response.blob();
                        }
                        return await new Promise((resolve,reject) => {
                            const reader = new FileReader(); reader.onload=()=>resolve(reader.result.split(',')[1]);
                            reader.onerror=reject; reader.readAsDataURL(blob);
                        });
                    }""",
                        {"url": blob, "capture": blob_capture},
                    )
                    destination.write_bytes(base64.b64decode(encoded))
                    break
                # Both account and card orders can lead to the saved-statements UI,
                # including when a document for this period is already ready.
                if master or card_id:
                    history = self.page.get_by_role(
                        "button", name="К выпискам", exact=True
                    )
                    ordered = self.page.get_by_role(
                        "radio", name="Мои выписки", exact=True
                    )
                    if not history_opened:
                        if history.count() and history.is_visible():
                            history.click()
                            history_opened = True
                        elif (
                            ordered.count()
                            and ordered.is_visible()
                            and self.page.get_by_text(
                                "Готовим выписку", exact=True
                            ).count()
                        ):
                            ordered.click()
                            history_opened = True
                    if history_opened and not ready_clicked:
                        selected = self.page.locator('#ORDERED[aria-checked="true"]')
                        if selected.count():
                            exact = re.compile(
                                r"с\s+"
                                + re.escape(f"{period.start:%d.%m.%Y}")
                                + r"\s+по\s+"
                                + re.escape(f"{period.end:%d.%m.%Y}")
                            )
                            ready = (
                                self.page.get_by_role("button")
                                .filter(has_text=exact)
                                .filter(has_text="Готова с")
                            )
                            if ready.count() > 1:
                                raise SourceError(
                                    "generation", "ambiguous_ready_documents"
                                )
                            if ready.count() == 1:
                                ready.click()
                                ready_clicked = True
                            elif time.monotonic() >= next_refresh:
                                refresh = self.page.get_by_role(
                                    "button", name="Обновить", exact=True
                                )
                                if refresh.count() != 1:
                                    raise SourceError(
                                        "generation", "history_controls_unrecognized"
                                    )
                                refresh.click()
                                next_refresh = time.monotonic() + 0.5
                self.page.wait_for_timeout(100)
            else:
                raise SourceError(
                    "generation" if history_opened else "acquisition",
                    "generation_pending"
                    if history_opened
                    else "pdf_not_received_within_budget",
                )
            if not destination.read_bytes().startswith(b"%PDF-"):
                raise SourceError("acquisition", "received_content_is_not_pdf")
            return StatementDocument(
                uuid4().hex,
                "card_statement" if card_id else "account_statement",
                product_id,
                card_id,
                period,
                datetime.now(UTC),
                destination,
            )
        except SourceError as exc:
            exc.submitted = submitted
            exc.diagnostics.update(
                {
                    "acquisition_step": step,
                    **blob_read_diagnostics,
                    **browser_diagnostic(exc),
                }
            )
            raise
        except KeyboardInterrupt as exc:
            raise SourceError(
                "interruption", "interrupted", submitted=submitted
            ) from exc
        except BrowserError as exc:
            if urlparse(self.page.url).path.startswith("/login"):
                expired = SessionExpired()
                expired.submitted = submitted
                raise expired from exc
            raise SourceError(
                "acquisition",
                "browser_action_failed",
                submitted=submitted,
                diagnostics={
                    "acquisition_step": step,
                    **blob_read_diagnostics,
                    **browser_diagnostic(exc),
                },
            ) from exc
        finally:
            try:
                self.page.evaluate(
                    """key => {
                            const capture = window[key];
                            if (!capture) return;
                            capture.active = false;
                            if (URL.createObjectURL === capture.wrapped)
                                URL.createObjectURL = capture.original;
                            capture.blobs.clear();
                        }""",
                    self._blob_capture_key,
                )
            except BrowserError:
                # Navigation/closure already released the page's objects.
                pass
            if blob_capture is not None:
                try:
                    blob_capture.dispose()
                except BrowserError:
                    pass
            self.page.remove_listener("download", downloaded)
            context.remove_listener("page", attach)
            context.remove_listener("response", response)
            for popup in new_pages:
                if not popup.is_closed():
                    popup.close()


class SyntheticAdapter(VtbAdapter):
    inventory_families = VtbAdapter.inventory_families + ("synthetic-term-deposit",)

    def _completion(self) -> tuple[Literal["complete", "uncertain"], str]:
        marker = self.page.locator('[data-synthetic-inventory="complete"]')
        if marker.count() == 1:
            counts = [
                self.page.locator(f'[data-test-id="{family}"]').count()
                for family in self.inventory_families
            ]
            if str(sum(counts[:2]) + counts[3]) == marker.get_attribute(
                "data-product-count"
            ) and str(counts[2]) == marker.get_attribute("data-card-count"):
                return "complete", "explicit_synthetic_inventory_end"
            return "uncertain", "synthetic_inventory_count_mismatch"
        return "uncertain", "synthetic_inventory_end_missing"
