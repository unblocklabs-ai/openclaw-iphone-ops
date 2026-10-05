"""Desired values for native controls; no task planning or input replay."""
from __future__ import annotations

import calendar
from datetime import date
import re
from typing import Callable

from .errors import OpenClawIPhoneError
from .observations import CHECKABLE, boolean
from .wda import NativeElement, WDAClient

Send = Callable[[Callable[[], object]], None]
TEXT_ROLES = frozenset({"XCUIElementTypeTextField", "XCUIElementTypeSecureTextField",
                        "XCUIElementTypeTextView", "XCUIElementTypeSearchField"})
WHEEL = "XCUIElementTypePickerWheel"


class InputUnavailable(OpenClawIPhoneError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InputReadbackUnavailable(OpenClawIPhoneError):
    def __init__(self, error: BaseException) -> None:
        super().__init__("Input readback unavailable.")
        self.error = error


def readback(wda: WDAClient, ref: str) -> str:
    try:
        return wda.element_value(ref, allow_null_empty=False)
    except (OpenClawIPhoneError, OSError) as exc:
        raise InputReadbackUnavailable(exc) from exc


def validate_text(text: object, *, multiline: bool = False, empty: bool = False) -> str:
    if (not isinstance(text, str) or not empty and not text
            or any((ord(c) < 32 and not (multiline and c == "\n"))
                   or 0xE000 <= ord(c) <= 0xF8FF or ord(c) == 127 for c in text)):
        raise ValueError("Invalid text; use press for control keys.")
    return text


def iso_date(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Use a Gregorian YYYY-MM-DD date.")
    return date.fromisoformat(value)


def write_text(wda: WDAClient, ref: str | None, text: str, send: Send, *,
               replace: bool, strategy: str, frequency: int | None = None) -> None:
    if replace:
        send(lambda: wda.element_action(ref, "clear") if ref else wda.clear_text())
    if not text:
        return
    if strategy == "sequential":
        if ref:
            send(lambda: wda.element_action(ref, "click"))
        send(lambda: wda.type_text(text, frequency=frequency))
    elif ref:
        # WDA prepares keyboard focus itself; don't prepend another tap.
        send(lambda: wda.element_action(ref, "value", text=text))
    else:
        send(lambda: wda.type_text_bulk(text, frequency=frequency))


def wheel_text(value: str) -> str:
    # Some native wheels append an English accessibility position announcement.
    return re.sub(r",\s*[0-9]+ of [0-9]+$", "", value).strip()


def number_value(value: str) -> int | None:
    match = re.fullmatch(r"\D*(\d+)\D*", value)
    return int(match[1]) if match else None


def format_number(value: int, current: str) -> str:
    match = re.fullmatch(r"(\D*)(\d+)(\D*)", current)
    if match is None:
        raise InputUnavailable("date_component_format_unsupported")
    digits = str(value).zfill(len(match[2])) if int(match[2][0]) == 0 else str(value)
    zero = ord(match[2][0]) - int(match[2][0])
    return match[1] + "".join(chr(zero + int(digit)) for digit in digits) + match[3]


def pick_value(wda: WDAClient, ref: str, value: str, send: Send, *,
               order: str | None = None, max_steps: int = 10, offset: float = 0.15,
               verify: bool = True,
               current: str | None = None) -> str:
    if current is not None and (current == value or wheel_text(current) == value):
        return "match"
    if order is None:
        send(lambda: wda.element_action(ref, "value", text=value))
        if not verify:
            return "unknown"
        current = readback(wda, ref)
        if current == value or wheel_text(current) == value:
            return "match"
        return "mismatch"
    current = current if current is not None else wda.element_value(ref, allow_null_empty=False)
    seen = {current}
    steps = 0
    while steps < max_steps:
        send(lambda: wda.picker_step(ref, order, offset=offset))
        steps += 1
        current = readback(wda, ref)
        if current == value or wheel_text(current) == value:
            return "match"
        if current in seen:
            break
        seen.add(current)
    return "mismatch"


def set_checked(wda: WDAClient, ref: str, value: bool, send: Send, *, current: str | None, verify: bool) -> str:
    current = boolean(current)
    if current is None:
        return "unknown"
    if current == value:
        return "match"
    send(lambda: wda.element_action(ref, "click"))
    if not verify:
        return "unknown"
    current = boolean(readback(wda, ref))
    return "unknown" if current is None else "match" if current == value else "mismatch"


def month_number(value: str, month_values: list[str] | None) -> int | None:
    value = wheel_text(value)
    names = [month_values] if month_values else [list(calendar.month_name)[1:], list(calendar.month_abbr)[1:]]
    for choices in names:
        for index, name in enumerate(choices, 1):
            if value.casefold() == name.casefold():
                return index
    number = number_value(value)
    return number if number is not None and 1 <= number <= 12 else None


WHEELS = "**/XCUIElementTypePickerWheel[`visible == 1 AND enabled == 1`]"


def date_components(wda: WDAClient, ref: str | None, components: dict | None,
                    resolve: Callable[[str | dict], NativeElement], month_values: list[str] | None) -> dict[str, NativeElement]:
    if components is not None:
        wheels = {part: resolve(target) for part, target in components.items()}
        if len({wheel.ref for wheel in wheels.values()}) != 3 or any(wheel.role != WHEEL for wheel in wheels.values()):
            raise InputUnavailable("date_components_must_be_distinct_native_wheels")
        if any(not isinstance(wheel.value, str) for wheel in wheels.values()):
            raise InputUnavailable("date_component_value_unavailable")
        return wheels
    wheels = wda.find_elements(WHEELS, using="class chain", element_id=ref)
    if len(wheels) != 3:
        raise InputUnavailable("date_component_mapping_required")
    if any(not isinstance(wheel.value, str) for wheel in wheels):
        raise InputUnavailable("date_component_value_unavailable")
    refs = {}
    for wheel in wheels:
        value = wheel_text(wheel.value)
        number = number_value(value)
        # Native birthday wheels use ---- for an omitted year.
        if value == "----" or number is not None and number > 31:
            part = "year"
        elif number is None and month_number(value, month_values) is not None:
            part = "month"
        else:
            continue
        if part in refs:
            raise InputUnavailable("date_component_mapping_required")
        refs[part] = wheel
    remaining = [wheel for wheel in wheels if wheel not in refs.values()]
    if set(refs) == {"year", "month"} and len(remaining) == 1:
        number = number_value(wheel_text(remaining[0].value))
        if number is not None and 1 <= number <= 31:
            refs["day"] = remaining[0]
    # Numeric dates can be ambiguous. Use explicit native labels, not positions.
    if set(refs) != {"year", "month", "day"}:
        refs = {}
        for wheel in wheels:
            label = (wheel.label or "").casefold()
            if label not in {"year", "month", "day"} or label in refs:
                raise InputUnavailable("date_component_mapping_required")
            refs[label] = wheel
    return refs


def set_date(wda: WDAClient, group: str | None, wheels: dict[str, NativeElement], desired: date, send: Send,
             month_values: list[str] | None, secrets: set[str], *, verify: bool) -> str:
    current = {part: wheel_text(wheel.value) for part, wheel in wheels.items()}
    month = current["month"]
    if month_values:
        month = month_values[desired.month - 1]
    elif number_value(month) is not None:
        month = format_number(desired.month, month)
    elif month.casefold() in {name.casefold() for name in calendar.month_name[1:]}:
        month = calendar.month_name[desired.month]
    elif month.casefold() in {name.casefold() for name in calendar.month_abbr[1:]}:
        month = calendar.month_abbr[desired.month]
    else:
        raise InputUnavailable("date_month_values_required")
    values = {"year": str(desired.year) if current["year"] == "----" else format_number(desired.year, current["year"]), "month": month,
              "day": format_number(desired.day, current["day"])}
    secrets.update(values.values())
    if current == values:
        return "match"
    for part in ("year", "month", "day"):
        send(lambda: wda.element_action(wheels[part].ref, "value", text=values[part]))
    if not verify:
        return "unknown"
    # One native snapshot verifies the complete date, including clamping.
    try:
        final_wheels = wda.find_elements(WHEELS, using="class chain", element_id=group)
    except (OpenClawIPhoneError, OSError) as exc:
        raise InputReadbackUnavailable(exc) from exc
    by_ref = {wheel.ref: wheel.value for wheel in final_wheels}
    if any(not isinstance(by_ref.get(wheel.ref), str) for wheel in wheels.values()):
        return "unknown"
    final = {part: wheel_text(by_ref[wheel.ref]) for part, wheel in wheels.items()}
    final_month = month_number(final["month"], month_values)
    final_year, final_day = number_value(final["year"]), number_value(final["day"])
    if final_month is None or final_year is None or final_day is None:
        return "unknown"
    try:
        observed = date(final_year, final_month, final_day)
    except ValueError:
        return "unknown"
    return "match" if observed == desired else "mismatch"


def input_kind(role: str | None, value: object, kind: str | None) -> str:
    if kind == "date" or role == "XCUIElementTypeDatePicker":
        return "date"
    if type(value) is bool and role in CHECKABLE:
        return "checked"
    if isinstance(value, str) and role == WHEEL:
        return "picker"
    if isinstance(value, str) and role in TEXT_ROLES:
        return "text"
    raise InputUnavailable("unsupported_input_control")
