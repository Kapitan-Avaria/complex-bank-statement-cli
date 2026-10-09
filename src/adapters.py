"""Source boundary: discovery evidence and verified document acquisition."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from documents import StatementDocument
from periods import DateRange
from products import Card, Product


class SourceError(Exception):
    """A safe, structured source failure; messages contain no personal data."""

    def __init__(
        self,
        stage: str,
        reason: str,
        submitted: bool = False,
        *,
        diagnostics: dict[str, str | None] | None = None,
    ) -> None:
        self.stage = stage
        self.reason = reason
        self.submitted = submitted
        self.diagnostics = diagnostics or {}
        super().__init__(f"{stage}: {reason}")


class SessionExpired(SourceError):
    def __init__(self) -> None:
        super().__init__("session", "session_expired")


@dataclass(frozen=True)
class DiscoveryFailure:
    family: str
    index: int
    stage: str
    reason: str
    browser_operation: str | None = None
    browser_error: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    products: tuple[Product, ...]
    cards: tuple[Card, ...]
    status: Literal["complete", "uncertain", "failed"]
    evidence: str
    failures: tuple[DiscoveryFailure, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in ("complete", "uncertain", "failed"):
            raise ValueError("Invalid discovery status")
        if not self.evidence.strip():
            raise ValueError("Discovery requires evidence or a failure reason")
        if len({p.product_id for p in self.products}) != len(self.products):
            raise ValueError("Duplicate product identity")
        if len({c.card_id for c in self.cards}) != len(self.cards):
            raise ValueError("Duplicate card identity")


class SourceAdapter(Protocol):
    def discover(self) -> DiscoveryResult: ...

    def acquire_account(
        self,
        product: Product,
        period: DateRange,
        destination: Path,
        *,
        previously_ordered: bool = False,
    ) -> StatementDocument: ...

    def acquire_card(
        self, card: Card, product: Product, period: DateRange, destination: Path
    ) -> StatementDocument: ...
