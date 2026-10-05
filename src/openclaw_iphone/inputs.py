"""Desired values for native controls; no task planning or input replay."""
from __future__ import annotations

import calendar
from datetime import date
import re
from typing import Callable

from .errors import OpenClawIPhoneError, WDAUnsupportedCommand
from .observations import CHECKABLE, boolean
from .wda import WDAClient

Send = Callable[[Callable[[], object]], None]
TEXT_ROLES = frozenset({"XCUIElementTypeTextField", "XCUIElementTypeSecureTextField",
                        "XCUIElementTypeTextView", "XCUIElementTypeSearchField"})
WHEEL = "XCUIElementTypePickerWheel"


class InputUnavailable(OpenClawIPhoneError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InputReadbackUnavailable(OpenClawIPhoneError):
    def __init__(self, error: BaseException, *, complete: bool = True) -> None:
        super().__init__("Input readback unavailable.")
        self.error, self.complete = error, complete


def readback(wda: WDAClient, ref: str) -> str:
    try:
        return wda.element_value(ref, allow_null_empty=False)
    except (OpenClawIPhoneError, OSError) as exc:
        raise InputReadbackUnavailable(exc) from exc


def validate_text(text: object, *, multiline: bool = False, empty: bool = False) -> str:
    if (not isinstance(text, str) or len(text) > 4096 or not empty and not text
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
    with wda.input_transaction():
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
               adjust: bool = False, order: str | None = None,
               max_steps: int | None = None, offset: float = 0.15,
               current: str | None = None, direction: Callable[[str], str] | None = None) -> str:
    current = current if current is not None else wda.element_value(ref, allow_null_empty=False)
    if current == value or wheel_text(current) == value:
        return "match"
    if order is None:
        try:
            send(lambda: wda.element_action(ref, "value", text=value))
        except WDAUnsupportedCommand:
            if not adjust:
                raise
            # An explicit unsupported response proves this route sent no input.
        current = readback(wda, ref)
        if current == value or wheel_text(current) == value:
            return "match"
        if not adjust:
            return "mismatch"
    seen = {current}
    steps = 0
    while max_steps is None or steps < max_steps:
        adjustment = order or (direction(current) if direction else "next")
        send(lambda: wda.picker_step(ref, adjustment, offset=offset))
        steps += 1
        current = readback(wda, ref)
        if current == value or wheel_text(current) == value:
            return "match"
        if current in seen:
            break
        seen.add(current)
    return "mismatch"


def set_checked(wda: WDAClient, ref: str, value: bool, send: Send) -> str:
    current = boolean(wda.element_value(ref))
    if current is None:
        return "unknown"
    if current == value:
        return "match"
    send(lambda: wda.element_action(ref, "click"))
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


def date_components(wda: WDAClient, ref: str | None, components: dict | None,
                    resolve: Callable[[str | dict], str], month_values: list[str] | None) -> tuple[dict[str, str], dict[str, str]]:
    if components is not None:
        refs = {part: resolve(target) for part, target in components.items()}
        if len(set(refs.values())) != 3 or any(wda.element_type(wheel) != WHEEL for wheel in refs.values()):
            raise InputUnavailable("date_components_must_be_distinct_native_wheels")
        return refs, {part: wheel_text(wda.element_value(wheel, allow_null_empty=False)) for part, wheel in refs.items()}
    wheels = wda.find_elements(".//XCUIElementTypePickerWheel[@visible='true' and @enabled='true']", element_id=ref)
    if len(wheels) != 3:
        raise InputUnavailable("date_component_mapping_required")
    values = {wheel: wheel_text(wda.element_value(wheel, allow_null_empty=False)) for wheel in wheels}
    refs = {}
    for wheel, value in values.items():
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
        number = number_value(values[remaining[0]])
        if number is not None and 1 <= number <= 31:
            refs["day"] = remaining[0]
    # Numeric dates can be ambiguous. Use explicit native labels, not positions.
    if set(refs) != {"year", "month", "day"}:
        refs = {}
        for wheel in wheels:
            label = (wda.element_label(wheel) or "").casefold()
            if label not in {"year", "month", "day"} or label in refs:
                raise InputUnavailable("date_component_mapping_required")
            refs[label] = wheel
    return refs, {part: values[wheel] for part, wheel in refs.items()}


def set_date(wda: WDAClient, refs: dict[str, str], current: dict[str, str], desired: date, send: Send,
             month_values: list[str] | None, secrets: set[str]) -> str:
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
    def direction(part: str, value: str) -> str:
        current_number = month_number(value, month_values) if part == "month" else number_value(value)
        if current_number is None:
            raise InputUnavailable("date_component_format_unsupported")
        desired_number = getattr(desired, part)
        if part == "month":
            return "next" if (desired_number - current_number) % 12 <= (current_number - desired_number) % 12 else "previous"
        return "previous" if current_number > desired_number else "next"
    with wda.input_transaction():
        for part in ("year", "month", "day"):
            try:
                pick_value(wda, refs[part], values[part], send, adjust=True,
                           current=current["year"] if part == "year" else None,
                           direction=lambda value: direction(part, value))
            except InputReadbackUnavailable as exc:
                exc.complete = part == "day"
                raise
        # Later wheels can clamp earlier components; check the complete date.
        final = {part: wheel_text(readback(wda, ref)) for part, ref in refs.items()}
    final_month = month_number(final["month"], month_values)
    final_year, final_day = number_value(final["year"]), number_value(final["day"])
    if final_month is None or final_year is None or final_day is None:
        return "unknown"
    try:
        observed = date(final_year, final_month, final_day)
    except ValueError:
        return "unknown"
    return "match" if observed == desired else "mismatch"


def input_kind(role: str, value: object, kind: str | None) -> str:
    if kind == "date" or role == "XCUIElementTypeDatePicker":
        return "date"
    if type(value) is bool and role in CHECKABLE:
        return "checked"
    if isinstance(value, str) and role == WHEEL:
        return "picker"
    if isinstance(value, str) and role in TEXT_ROLES:
        return "text"
    raise InputUnavailable("unsupported_input_control")
