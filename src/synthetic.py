"""Local synthetic bank: browser login, requisites, ordering, PDF delivery, failures."""

import os
import threading
from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from parsing import parse_date
from periods import DateRange

ACCOUNTS = {
    "master-demo": (
        "MasterAccount",
        "Дебетовый счет",
        "40817810000000000001",
        date(2025, 1, 1),
    ),
    "savings-demo": (
        "SavingsAccount",
        "Накопительный счет",
        "40817810000000000002",
        date(2026, 10, 7),
    ),
    "deposit-demo": (
        "TermDeposit",
        "Срочный вклад (синтетический)",
        "42301810000000000003",
        date(2026, 10, 5),
    ),
}
CARD_ID = "card-demo"


def synthetic_font() -> str:
    candidates = [
        os.environ.get("SYNTHETIC_FONT", ""),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            if "SyntheticFont" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("SyntheticFont", candidate))
            return "SyntheticFont"
    raise RuntimeError("Set SYNTHETIC_FONT to a Cyrillic-capable TrueType font")


def make_pdf(identifier: str, period: DateRange, scenario: str = "complete") -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(840, 600))
    font = synthetic_font()
    span = f"{period.start:%d.%m.%Y} - {period.end:%d.%m.%Y}"

    def header():
        c.setFont(font, 10)
        if identifier == CARD_ID:
            lines = [
                "Номер карты 4321",
                f"Период выписки {span}",
                "Доступный остаток 75.50 RUB",
                "Операции по карте",
                "Карточная история не входит в сводные операции",
            ]
        else:
            _, _, number, _ = ACCOUNTS[identifier]
            closing = (
                "80.00"
                if identifier == "master-demo"
                else "105.00"
                if identifier == "deposit-demo"
                else "100.00"
            )
            lines = [
                f"Номер счёта {number} (RUB)",
                f"Период выписки {span}",
                "Баланс на начало периода 100.00 RUB",
                f"Баланс на конец периода {closing} RUB",
                "Операции по счету",
            ]
        for i, line in enumerate(lines):
            c.drawString(20, 570 - i * 22, line)

    def table(rows):
        xs = [20, 118, 218, 328, 420, 510, 710, 820]
        rows = [
            [
                "Дата операции",
                "Дата обработки",
                "Сумма операции",
                "Приход",
                "Расход",
                "Описание операции",
                "Получатель",
            ]
        ] + rows
        top = 435
        c.setFont(font, 9)
        for r, row in enumerate(rows):
            for j, cell in enumerate(row):
                for k, line in enumerate(cell.splitlines()):
                    c.drawString(xs[j] + 3, top - r * 55 - 16 - k * 13, line)
        for x in xs:
            c.line(x, top, x, top - len(rows) * 55)
        for r in range(len(rows) + 1):
            c.line(xs[0], top - r * 55, xs[-1], top - r * 55)

    header()
    if identifier == "savings-demo":
        c.drawString(20, 430, "За заданный период операций по счету не проводилось")
        c.showPage()
        c.showPage()
    elif identifier == "master-demo":
        # Two legitimately distinct identical rows; one description continues on another page.
        row = [
            f"{period.start:%d.%m.%Y}",
            f"{period.start:%d.%m.%Y}",
            "10.00 RUB",
            "",
            "10.00 RUB",
            "Оплата\nМногострочное описание",
            "Магазин",
        ]
        table([row])
        c.showPage()
        header()
        continuation = ["", "", "", "", "", "Продолжение на другой странице", ""]
        second = [
            f"{period.start:%d.%m.%Y}",
            f"{period.start:%d.%m.%Y}",
            "10.00 RUB",
            "",
            "bad" if scenario == "malformed" else "10.00 RUB",
            "Оплата",
            "Магазин",
        ]
        table([continuation, second])
    elif identifier == "deposit-demo":
        table(
            [
                [
                    f"{period.start:%d.%m.%Y}",
                    f"{period.start:%d.%m.%Y}",
                    "5.00 RUB",
                    "5.00 RUB",
                    "",
                    "Проценты",
                    "Банк",
                ]
            ]
        )
    c.save()
    return buffer.getvalue()


def document_page(body: str) -> str:
    return (
        '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Синтетический банк</title><body><main>'
        + body
        + "</main></body></html>"
    )


@contextmanager
def synthetic_cabinet(scenario: str = "complete"):
    """Serve only loopback; all names, identifiers and financial values are invented."""
    sessions: set[str] = set()
    orders: dict[tuple[str, str], DateRange] = {}
    refreshes: dict[tuple[str, str], int] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # No account/period/request logging.

        def send(
            self,
            body,
            content_type="text/html; charset=utf-8",
            status=200,
            headers=None,
        ):
            raw = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def session(self):
            cookies = dict(
                item.strip().split("=", 1)
                for item in self.headers.get("Cookie", "").split(";")
                if "=" in item
            )
            return cookies.get("synthetic-session", "")

        def authenticated(self):
            if self.session() in sessions:
                return True
            self.send("", status=303, headers={"Location": "/login"})
            return False

        def history(self, identifier):
            key = ("demo-user", identifier)
            refreshes[key] = refreshes.get(key, 0) + 1
            period = orders.get(key)
            body = '<h1>Выписка по дебетовому счету</h1><button role="radio" id="ORDERED" aria-checked="true">Мои выписки</button>'
            if period and refreshes[key] > 1 and scenario != "pending":
                label = f"Выписка с {period.start:%d.%m.%Y} по {period.end:%d.%m.%Y} Готова с 08.10.2026"
                body += f"<button onclick=\"window.open('/viewer/{identifier}')\">{label}</button>"
            else:
                body += "<p>Готовим выписку</p>"
            body += f"<button onclick=\"fetch('/history/{identifier}').then(r=>r.text()).then(t=>document.querySelector('main').innerHTML=t)\">Обновить</button>"
            return body

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/login":
                self.send(
                    document_page(
                        '<h1>Синтетический вход</h1><form method="post" action="/login"><button>Войти</button></form>'
                    )
                )
                return
            if not self.authenticated():
                return
            if path == "/home":
                self.send(
                    document_page(
                        '<h1>Синтетический банк</h1><button data-test-id="allmyproducts_favorite-buttons" onclick="location.href=\'/home/all-products\'">Все мои продукты и счета</button>'
                    )
                )
                return
            if path == "/home/all-products":
                body = "<h1>Все мои продукты и счета</h1><h2>Карты и счета</h2>"
                for ident, (kind, name, number, opened) in ACCOUNTS.items():
                    test = {
                        "MasterAccount": "MASTER_ACCOUNT",
                        "SavingsAccount": "saving_account_savings",
                        "TermDeposit": "synthetic-term-deposit",
                    }[kind]
                    body += f'<button data-test-id="{test}" data-id="{ident}" onclick="location.href=\'/details/{kind}/{ident}\'">{name} ***{number[-4:]}</button>'
                body += f'<button data-test-id="debet_card_main_page" data-id="{CARD_ID}" onclick="location.href=\'/details/DebitCard/{CARD_ID}\'">Карта ****4321</button><h2>Вклады и счета</h2><h2>Кредиты</h2>'
                if scenario != "uncertain":
                    body += '<p data-synthetic-inventory="complete" data-product-count="3" data-card-count="1">Все 3 продукта и 1 карта загружены</p>'
                self.send(document_page(body))
                return
            if path.startswith("/history/"):
                self.send(self.history(path.split("/")[-1]))
                return
            if path.startswith("/viewer/"):
                ident = path.split("/")[-1]
                self.send(
                    document_page(
                        f'<script>fetch("/ready/{ident}").then(r=>r.blob()).then(b=>location.href=window.URL.createObjectURL(b))</script>'
                    )
                )
                return
            if path.startswith("/ready/"):
                ident = path.split("/")[-1]
                period = orders.get(("demo-user", ident))
                if period is None:
                    self.send("not ready", status=404)
                    return
                self.send(make_pdf(ident, period, scenario), "application/pdf")
                return
            if path.startswith("/details/"):
                parts = path.split("/")
                kind, ident = parts[2:4]
                suffix = "/".join(parts[4:])
                is_card = ident == CARD_ID
                account_ident = "master-demo" if is_card else ident
                if account_ident not in ACCOUNTS:
                    self.send("Unknown product", status=404)
                    return
                akind, name, number, opened = ACCOUNTS[account_ident]
                if scenario == "identity-mismatch" and account_ident == "master-demo":
                    number = "40817810000000000009"
                if suffix in ("informationAndRequisites", "requisites"):
                    label = (
                        "Номер счета"
                        if akind != "MasterAccount"
                        else "Счет получателя в банке получателя"
                    )
                    body = "<h1>Реквизиты счета</h1>"
                    for label, value in [
                        (label, number),
                        ("Дата открытия", f"{opened:%d.%m.%Y}"),
                        ("Валюта счета", "Российский рубль"),
                    ]:
                        body += f'<div tabindex="0"><span>{label}</span><span>{value}</span></div>'
                    self.send(document_page(body))
                    return
                if suffix in ("statement", "statement/start"):
                    period_label = (
                        "Период выписки" if kind == "SavingsAccount" else "Период"
                    )
                    if kind == "SavingsAccount":
                        form = f"<label>{period_label}<input aria-label=\"{period_label}\" name=\"period\" data-test-id=\"statement-period_input\"></label><button data-test-id=\"export-statement_button\" onclick=\"const v=document.querySelector('input').value; fetch('/generate/{ident}',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:'period='+encodeURIComponent(v)}}).then(r=>{{if(!r.ok)throw Error();return r.blob()}}).then(b=>window.open(window.URL.createObjectURL(b))).catch(e=>{{console.error(e.toString());document.body.insertAdjacentHTML('beforeend','<p>Ошибка загрузки</p>')}})\">Скачать</button>"
                    else:
                        form = f'<form method="post" action="/generate/{ident}"><label>{period_label}<input aria-label="{period_label}" name="period"></label><button>Заказать</button></form>'
                    if kind == "MasterAccount":
                        form = (
                            f'<button role="radio" id="NEW" aria-checked="true">Заказать новую</button><button role="radio" id="ORDERED" aria-checked="false" onclick="fetch(\'/history/{ident}\').then(r=>r.text()).then(t=>document.querySelector(\'main\').innerHTML=t)">Мои выписки</button>'
                            + form
                        )
                    if kind == "MasterAccount" and scenario == "order-disabled":
                        form = form.replace(
                            "<button>Заказать</button>",
                            "<button disabled>Заказать</button>",
                        )
                    self.send(document_page(form))
                    return
                reqsuffix = (
                    "informationAndRequisites"
                    if akind == "MasterAccount"
                    else "requisites"
                )
                reqroute = f"/details/{'MasterAccountCard' if is_card else kind}/{ident}/{reqsuffix}"
                stmt = f"/details/{'MasterAccountCard' if is_card else kind}/{ident}/{'statement' if kind == 'SavingsAccount' else 'statement/start'}"
                balance = (
                    ""
                    if kind != "MasterAccount"
                    else '<div aria-label="Баланс 100 рублей"><h2 aria-hidden="true">100<span>,00 ₽</span></h2></div>'
                )
                body = f'<h1>{"Карта" if is_card else name}</h1>{balance}<button id="triggeringElement-requisites" onclick="location.href=\'{reqroute}\'">Реквизиты счета</button><button id="triggeringElement-statement" onclick="location.href=\'{stmt}\'">{"Выписка по карте" if is_card else "Получить выписку" if kind == "SavingsAccount" else "Выписка по счету"}</button>'
                self.send(document_page(body))
                return
            self.send("Not found", status=404)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/login":
                session = uuid4().hex
                sessions.add(session)
                self.send(
                    "",
                    status=303,
                    headers={
                        "Location": "/home",
                        "Set-Cookie": f"synthetic-session={session}; HttpOnly; SameSite=Strict; Path=/",
                    },
                )
                return
            if not self.authenticated():
                return
            if path.startswith("/generate/"):
                ident = path.split("/")[-1]
                if scenario == "expiry" and ident == "savings-demo":
                    sessions.discard(self.session())
                    self.send("", status=303, headers={"Location": "/login"})
                    return
                if (scenario == "download-failure" and ident == "savings-demo") or (
                    scenario == "card-failure" and ident == CARD_ID
                ):
                    self.send(document_page("<p>Ошибка загрузки</p>"), status=503)
                    return
                body = self.rfile.read(
                    int(self.headers.get("Content-Length", "0"))
                ).decode()
                raw = parse_qs(body).get("period", [""])[0]
                try:
                    start, end = raw.split(" – ")
                    period = DateRange(parse_date(start), parse_date(end))
                except ValueError:
                    self.send("Invalid period", status=400)
                    return
                if scenario == "pending" and ("demo-user", ident) in orders:
                    self.send("duplicate_order", status=409)
                    return
                orders[("demo-user", ident)] = period
                if ident == "master-demo":
                    self.send(
                        document_page(
                            "<h1>Готовим выписку</h1><button onclick=\"fetch('/history/master-demo').then(r=>r.text()).then(t=>document.querySelector('main').innerHTML=t)\">К выпискам</button>"
                        )
                    )
                    return
                self.send(
                    make_pdf(ident, period, scenario),
                    "application/pdf",
                    headers={}
                    if ident == "savings-demo"
                    else {
                        "Content-Disposition": 'attachment; filename="statement.pdf"'
                    },
                )
                return
            self.send("Not found", status=404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
