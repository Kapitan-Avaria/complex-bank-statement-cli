"""Python CLI. Manual live login/consent, ephemeral Playwright context."""

import argparse
import os
import subprocess
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import Error as BrowserError
from playwright.sync_api import sync_playwright

from browser_source import SyntheticAdapter, VtbAdapter
from periods import DateRange
from pipeline import run_statement
from synthetic import synthetic_cabinet


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="Комплексная выписка: JSON и отчёт о полноте"
    )
    cli.add_argument("--source", choices=("synthetic", "vtb"), default="synthetic")
    cli.add_argument(
        "--from",
        dest="start",
        required=True,
        type=date.fromisoformat,
        help="YYYY-MM-DD, включительно",
    )
    cli.add_argument(
        "--to",
        dest="end",
        required=True,
        type=date.fromisoformat,
        help="YYYY-MM-DD, включительно",
    )
    cli.add_argument("--output", type=Path)
    cli.add_argument(
        "--browser-executable",
        type=Path,
        help="Путь к установленному Chromium-совместимому браузеру (например Яндекс)",
    )
    cli.add_argument(
        "--ignore-https-errors",
        action="store_true",
        help="Небезопасно: игнорировать ошибки TLS-сертификатов на свой страх и риск",
    )
    cli.add_argument("--resume", action="store_true")
    cli.add_argument(
        "--automated",
        action="store_true",
        help="Только синтетический автоматический вход/согласие и headless",
    )
    cli.add_argument(
        "--consent",
        choices=("ask", "yes", "no"),
        default="ask",
        help="yes разрешено только для синтетического --automated",
    )
    cli.add_argument(
        "--scenario",
        choices=(
            "complete",
            "download-failure",
            "card-failure",
            "malformed",
            "expiry",
            "pending",
            "uncertain",
            "identity-mismatch",
        ),
        default="complete",
    )
    cli.add_argument(
        "--wait-seconds",
        type=float,
        default=60,
        help="Бюджет ожидания одного документа, 0 < значение <= 300",
    )
    return cli


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        requested = DateRange(args.start, args.end)
    except ValueError:
        cli.error("Начало периода должно быть не позже конца")
    if not 0 < args.wait_seconds <= 300:
        cli.error("--wait-seconds должен быть от 0 (не включая) до 300")
    if args.source == "vtb" and (
        args.automated or args.consent == "yes" or args.scenario != "complete"
    ):
        cli.error(
            "ВТБ требует ручной вход и явное согласие; сценарии относятся к синтетическому источнику"
        )
    if args.consent == "yes" and not args.automated:
        cli.error("--consent yes требует синтетический --automated")
    if args.resume and args.output is None:
        cli.error("--resume требует исходный --output")
    if args.consent == "no":
        print("Чтение не разрешено.")
        return 3
    output = (args.output or Path("runs") / uuid4().hex).resolve()
    root = Path(__file__).resolve().parents[1]
    if output.is_relative_to(root):
        ignored = (
            subprocess.run(
                ["git", "check-ignore", "-q", str(output / "statement.json")],
                cwd=root,
                check=False,
            ).returncode
            == 0
        )
        if not ignored:
            cli.error(
                "Папка результата внутри репозитория должна быть исключена из Git (например runs/)"
            )
    if output.exists() and any(output.iterdir()) and not args.resume:
        cli.error("Папка результата непустая: выберите новую или используйте --resume")
    os.umask(0o077)
    server = (
        synthetic_cabinet(args.scenario)
        if args.source == "synthetic"
        else nullcontext("https://online.vtb.ru")
    )
    if args.ignore_https_errors:
        print(
            "ВНИМАНИЕ: --ignore-https-errors отключает проверку TLS-сертификатов "
            "для всех запросов в браузерном контексте этого запуска. "
            "Это небезопасно: возможны подмена сайта и перехват банковских данных. "
            "Используйте на свой страх и риск."
        )
    try:
        with server as base_url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=args.automated,
                executable_path=(
                    args.browser_executable.expanduser().resolve()
                    if args.browser_executable
                    else None
                ),
            )
            try:
                context = browser.new_context(
                    accept_downloads=True, ignore_https_errors=args.ignore_https_errors
                )
                try:
                    page = context.new_page()
                    page.goto(base_url + "/login", wait_until="domcontentloaded")
                    if args.automated:
                        page.get_by_role("button", name="Войти", exact=True).click()
                    else:
                        input(
                            "Войдите вручную, закройте рекламные окна и нажмите Enter здесь. "
                        )
                    consent = args.consent
                    if consent == "ask" and not args.automated:
                        consent = (
                            "yes"
                            if input(
                                "Разрешить чтение продуктов и заказ выписок за выбранный период? Введите да: "
                            )
                            .strip()
                            .lower()
                            == "да"
                            else "no"
                        )
                    elif args.automated and consent == "ask":
                        consent = "yes"  # Explicit --automated authorizes only invented fixture data.
                    if consent != "yes":
                        print("Чтение не разрешено.")
                        return 3
                    adapter = (
                        SyntheticAdapter(page, base_url, args.wait_seconds)
                        if args.source == "synthetic"
                        else VtbAdapter(page, base_url, args.wait_seconds)
                    )
                    result = run_statement(
                        adapter, requested, output, resume=args.resume
                    )
                    status = result["report"]["status"]
                    print(
                        f"Результат: {status}. JSON и отчёт сохранены в выбранной папке результата."
                    )
                    return 0 if status == "full" else 2
                finally:
                    context.close()
            finally:
                browser.close()
    except BrowserError as exc:
        if "net::ERR_CERT_AUTHORITY_INVALID" in str(exc):
            print(
                "Браузер не доверяет TLS-сертификату сайта "
                "(ERR_CERT_AUTHORITY_INVALID). "
                "Для ВТБ установите сертификаты по инструкции https://www.vtb.ru/crt/ "
                "или выберите Яндекс Браузер через --browser-executable. "
                "Проверка HTTPS остаётся включённой."
            )
        else:
            print("Не удалось завершить запуск браузера или локального хранилища.")
        return 2
    except (OSError, RuntimeError):
        print("Не удалось завершить запуск браузера или локального хранилища.")
        return 2
    except (KeyboardInterrupt, EOFError):
        print("Запуск прерван.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
