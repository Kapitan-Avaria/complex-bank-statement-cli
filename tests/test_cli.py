import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cli(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "src/main.py"), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
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
