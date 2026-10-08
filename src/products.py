import datetime
from dataclasses import dataclass

from fields import FieldValue
from money import Money
from periods import DateRange


@dataclass(frozen=True)
class BalanceReading:
    value: Money
    read_at: datetime.datetime
    source: str

    def __post_init__(self) -> None:
        if self.read_at.utcoffset() is None:
            raise ValueError("read_at must be timezone-aware")
        if self.source.strip() == "":
            raise ValueError("source must be non-empty string")
        if not self.value.amount.is_finite():
            raise ValueError("value.amount must be finite decimal")


@dataclass(frozen=True)
class Requisite:
    name: str
    value: str


@dataclass(frozen=True)
class Product:
    product_id: str  # Application's local identifier

    source_id: FieldValue[str]  # Identifier supplied by the bank
    name: FieldValue[str]
    product_type: FieldValue[str]
    masked_number: FieldValue[str]
    currency: FieldValue[str]

    current_balance: FieldValue[BalanceReading]
    available_balance: FieldValue[BalanceReading]

    requisites: FieldValue[tuple[Requisite, ...]]

    opened_on: FieldValue[datetime.date]


@dataclass(frozen=True)
class CardAccountLink:
    product_id: str  # The linked account's local Product.product_id
    source: str  # Where the relationship was confirmed

    def __post_init__(self) -> None:
        if self.product_id.strip() == "":
            raise ValueError("product_id must be non-empty string")
        if self.source.strip() == "":
            raise ValueError("source must be non-empty string")


@dataclass(frozen=True)
class CardAvailableBalance:
    value: Money
    document_id: str
    document_period: DateRange
    acquired_at: datetime.datetime
    as_of: FieldValue[datetime.datetime]

    def __post_init__(self) -> None:
        if not self.value.amount.is_finite():
            raise ValueError("value.amount must be finite decimal")
        if self.document_id.strip() == "":
            raise ValueError("document_id must be non-empty string")
        if self.acquired_at.utcoffset() is None:
            raise ValueError("acquired_at must be timezone-aware")
        if self.as_of.value is not None and self.as_of.value.utcoffset() is None:
            raise ValueError("if as_of.value is not None, it must be timezone-aware")


@dataclass(frozen=True)
class Card:
    card_id: str
    source_id: FieldValue[str]
    masked_number: FieldValue[str]
    account_link: FieldValue[CardAccountLink]
    available_balance: FieldValue[CardAvailableBalance]
