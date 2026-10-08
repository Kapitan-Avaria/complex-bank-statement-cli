import datetime
from dataclasses import dataclass
from typing import Literal, Optional


@dataclass(frozen=True)
class DateRange:
    start: datetime.date
    end: datetime.date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("Start date must be on or before end date")

    def intersection(self, other: "DateRange") -> Optional["DateRange"]:
        start = max(self.start, other.start)
        end = min(self.end, other.end)

        if start > end:
            return None

        return DateRange(start, end)


@dataclass(frozen=True)
class ExtractionPlan:
    requested: DateRange
    extraction_period: DateRange | None
    status: Literal["ready", "not_applicable", "unknown_lifetime"]


def extraction_period_for_active_product(
    requested: DateRange, opened_on: datetime.date
) -> DateRange | None:
    start = max(requested.start, opened_on)
    end = requested.end

    if start > end:
        return None
    return DateRange(start, end)


def plan_extraction_for_active_product(
    requested: DateRange, opened_on: datetime.date | None
) -> ExtractionPlan:
    if opened_on is None:
        return ExtractionPlan(requested, None, "unknown_lifetime")

    extraction_period = extraction_period_for_active_product(requested, opened_on)
    if extraction_period is None:
        return ExtractionPlan(requested, None, "not_applicable")

    return ExtractionPlan(requested, extraction_period, "ready")
