"""Private presentation result; preparation, networking and refresh live in Observer."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PickerModel:
    payload: dict[str, object]
    refresh_needed: bool
