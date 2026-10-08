from dataclasses import dataclass
from typing import Callable, Literal


@dataclass(frozen=True)
class FieldValue[T]:
    value: T | None
    status: Literal["known", "absent", "failed"]
    reason: str | None

    def __post_init__(self) -> None:

        if self.status not in ("known", "absent", "failed"):
            raise ValueError("The status must be 'known', 'absent' or 'failed'")

        if self.status == "known":
            if self.value is None:
                raise ValueError("The value must be present when the status is 'known'")
            if self.reason is not None:
                raise ValueError("The reason must be None when the status is 'known'")

        if self.status in ("absent", "failed"):
            if self.value is not None:
                raise ValueError(
                    "The value must be None when the status is 'absent' or 'failed'"
                )
            if self.reason is None:
                raise ValueError(
                    "The reason must be present when the status is 'absent' or 'failed'"
                )
            if self.reason.strip() == "":
                raise ValueError(
                    "The reason must be non-empty string when the status is 'absent' or 'failed'"
                )


def field_to_dict[T](
    field: FieldValue[T], serialize: Callable[[T], object]
) -> dict[str, object]:
    return {
        "value": serialize(field.value) if field.value is not None else None,
        "status": field.status,
        "reason": field.reason,
    }
