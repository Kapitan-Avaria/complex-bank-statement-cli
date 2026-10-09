import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest
from playwright.sync_api import Error as BrowserError

import main

ROOT = Path(__file__).resolve().parents[1]


def cli(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "src/main.py"), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def test_invalid_period_and_declined_consent_do_not_extract(tmp_path):
    invalid = cli(
        "--from",
        "2026-10-08",
        "--to",
        "2026-10-01",
        "--output",
        str(tmp_path / "invalid"),
    )
    assert invalid.returncode == 2
    assert not (tmp_path / "invalid").exists()
    declined = cli(
        "--from",
        "2026-10-01",
        "--to",
        "2026-10-08",
        "--source",
        "synthetic",
        "--automated",
        "--consent",
        "no",
        "--output",
        str(tmp_path / "declined"),
    )
    assert declined.returncode == 3
    assert not (tmp_path / "declined").exists()


def test_synthetic_cli_produces_reviewable_export(tmp_path):
    import json

    result = cli(
        "--from",
        "2026-10-01",
        "--to",
        "2026-10-08",
        "--automated",
        "--output",
        str(tmp_path / "run"),
        "--wait-seconds",
        "2",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    statement = json.loads((tmp_path / "run" / "statement.json").read_text())
    assert statement["report"]["status"] == "full"
    assert len(statement["operations"]) == 3
    assert statement["products"][2]["extraction_period"] == {
        "start": "2026-10-05",
        "end": "2026-10-08",
    }


@pytest.mark.parametrize("custom_browser", [False, True])
def test_vtb_untrusted_certificate_is_actionable_and_private(
    tmp_path, monkeypatch, capsys, custom_browser
):
    playwright = Mock()
    browser = playwright.chromium.launch.return_value
    context = browser.new_context.return_value
    context.new_page.return_value.goto.side_effect = BrowserError(
        "Page.goto: net::ERR_CERT_AUTHORITY_INVALID at "
        "https://online.vtb.ru/login?token=PRIVATE_TOKEN\nCall log: PRIVATE_DATA"
    )
    monkeypatch.setattr(main, "sync_playwright", lambda: nullcontext(playwright))
    manual_input = Mock(side_effect=AssertionError("Login must not be requested"))
    monkeypatch.setattr("builtins.input", manual_input)
    argv = [
        "--source",
        "vtb",
        "--from",
        "2026-10-01",
        "--to",
        "2026-10-08",
        "--output",
        str(tmp_path / "run"),
    ]
    executable = tmp_path / "Yandex Browser" / "Yandex"
    if custom_browser:
        argv += ["--browser-executable", str(executable)]
    assert main.main(argv) == 2
    output = capsys.readouterr().out
    assert "ERR_CERT_AUTHORITY_INVALID" in output
    assert "https://www.vtb.ru/crt/" in output
    assert "--browser-executable" in output
    assert "PRIVATE" not in output
    assert not (tmp_path / "run").exists()
    context.close.assert_called_once()
    browser.close.assert_called_once()
    manual_input.assert_not_called()
    if custom_browser:
        assert (
            playwright.chromium.launch.call_args.kwargs["executable_path"] == executable
        )
    assert not browser.new_context.call_args.kwargs.get("ignore_https_errors", False)


@pytest.mark.parametrize("ignore_https_errors", [False, True])
def test_https_errors_require_explicit_flag_and_keep_manual_consent(
    tmp_path, monkeypatch, capsys, ignore_https_errors
):
    playwright = Mock()
    browser = playwright.chromium.launch.return_value
    monkeypatch.setattr(main, "sync_playwright", lambda: nullcontext(playwright))
    inputs = iter(["", "нет"])

    def manual_input(prompt):
        # The warning must be visible before the user enters credentials.
        if "Войдите вручную" in prompt:
            output = capsys.readouterr().out
            assert ("ВНИМАНИЕ" in output) == ignore_https_errors
            if ignore_https_errors:
                assert "на свой страх и риск" in output
        return next(inputs)

    monkeypatch.setattr("builtins.input", manual_input)
    extract = Mock()
    monkeypatch.setattr(main, "run_statement", extract)
    argv = [
        "--source",
        "vtb",
        "--from",
        "2026-10-01",
        "--to",
        "2026-10-08",
        "--output",
        str(tmp_path / "run"),
    ]
    if ignore_https_errors:
        argv.append("--ignore-https-errors")
    assert main.main(argv) == 3
    assert (
        browser.new_context.call_args.kwargs["ignore_https_errors"]
        is ignore_https_errors
    )
    extract.assert_not_called()
    browser.new_context.return_value.close.assert_called_once()
    browser.close.assert_called_once()
