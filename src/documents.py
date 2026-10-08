import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from periods import DateRange


@dataclass(frozen=True)
class StatementDocument:
    document_id: str
    kind: Literal["account_statement", "card_statement"]
    product_id: str
    card_id: str | None
    period: DateRange
    acquired_at: datetime.datetime
    local_path: Path

    def __post_init__(self) -> None:
        if self.kind not in ("account_statement", "card_statement"):
            # Future: This approach breaks Open-Closed Principle, like in some other places too.
            # What if we'll want to add a new document kind?
            # But this is a prototype and it works for now
            raise ValueError("kind must be 'account_statement' or 'card_statement'")
        if self.document_id.strip() == "":
            raise ValueError("document_id must be non-empty string")
        if self.product_id.strip() == "":
            raise ValueError("product_id must be non-empty string")

        if self.kind == "card_statement":
            if self.card_id is None:
                raise ValueError(
                    "card_id must be present when document kind is 'card_statement'"
                )
            if self.card_id.strip() == "":
                raise ValueError("card_id must be non-empty string")
        if self.kind == "account_statement" and self.card_id is not None:
            raise ValueError(
                "card_id must be None when document kind is 'account_statement'"
            )

        if self.acquired_at.utcoffset() is None:
            raise ValueError("acquired_at must be timezone-aware")
