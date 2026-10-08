"""Atomic local exports and conservative document checkpoints; no login state."""

import hashlib
import json
import os
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from documents import StatementDocument
from periods import DateRange

SCHEMA_VERSION = 1


def json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: json_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non_finite_money")
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(json_value(value), stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Checkpoints:
    def __init__(self, directory: Path, requested: DateRange, resume: bool) -> None:
        self.directory = directory.resolve()
        self.path = directory / "progress.json"
        self.requested = json_value(requested)
        self.entries: dict[str, dict] = {}
        self.pending_orders: dict[str, dict] = {}
        if resume and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if (
                    isinstance(data, dict)
                    and data["schema_version"] == SCHEMA_VERSION
                    and data["requested_period"] == self.requested
                    and isinstance(data.get("documents"), dict)
                ):
                    self.entries = data["documents"]
                    if isinstance(data.get("pending_orders"), dict):
                        self.pending_orders = data["pending_orders"]
            except (ValueError, KeyError, TypeError):
                pass

    def document(
        self, key: str, binding: dict, period: DateRange
    ) -> StatementDocument | None:
        item = self.entries.get(key)
        if not isinstance(item, dict) or item.get("binding") != binding:
            return None
        try:
            d = item["document"]
            if not isinstance(d, dict):
                return None
            path = Path(d["local_path"]).resolve()
            if not path.is_relative_to(self.directory) or not path.is_file():
                return None
            if (
                item["sha256"] != sha256(path)
                or d["period"] != json_value(period)
                or d["kind"] != binding["kind"]
            ):
                return None
            return StatementDocument(
                d["document_id"],
                d["kind"],
                d["product_id"],
                d["card_id"],
                period,
                datetime.fromisoformat(d["acquired_at"]),
                path,
            )
        except (ValueError, KeyError, TypeError, OSError):
            return None

    def save(
        self,
        key: str,
        binding: dict,
        document: StatementDocument,
        result_path: Path | None = None,
    ) -> None:
        self.entries[key] = {
            "binding": binding,
            "document": json_value(document),
            "sha256": sha256(document.local_path),
            "result_path": json_value(result_path),
        }
        self.pending_orders.pop(key, None)
        self._persist()

    def order_pending(self, key: str, binding: dict, period: DateRange) -> bool:
        return self.pending_orders.get(key) == {
            "binding": binding,
            "period": json_value(period),
        }

    def save_pending(self, key: str, binding: dict, period: DateRange) -> None:
        self.pending_orders[key] = {"binding": binding, "period": json_value(period)}
        self._persist()

    def _persist(self) -> None:
        atomic_json(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "requested_period": self.requested,
                "documents": self.entries,
                "pending_orders": self.pending_orders,
            },
        )
