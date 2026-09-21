"""Local accessibility snapshots. No source text is implicitly cloud-safe."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import uuid
from xml.etree import ElementTree as ET

from .errors import OpenClawIPhoneError


class ObservationRejected(OpenClawIPhoneError):
    """Observation is incomplete, ambiguous, or no longer valid for an action."""


EDITABLE = frozenset({"XCUIElementTypeTextField", "XCUIElementTypeTextView", "XCUIElementTypeSearchField"})
SCROLLABLE = frozenset({"XCUIElementTypeScrollView", "XCUIElementTypeTable", "XCUIElementTypeCollectionView"})
TAPPABLE = EDITABLE | {"XCUIElementTypeButton", "XCUIElementTypeCell", "XCUIElementTypeLink"}


def boolean(value: str | None) -> bool | None:
    return {"true": True, "false": False, "1": True, "0": False}.get(value)


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in value.split("'")) + ")"


@dataclass(frozen=True)
class Selector:
    role: str
    name: str | None = None
    label: str | None = None
    ancestor_label: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"XCUIElementType[A-Za-z]+", self.role):
            raise ValueError("Selector requires an exact XCUIElementType role.")
        if any(value is not None and (not isinstance(value, str) or not value or len(value) > 256)
               for value in (self.name, self.label, self.ancestor_label)):
            raise ValueError("Selector labels must be short non-empty strings.")


@dataclass(frozen=True)
class Element:
    id: str
    role: str
    name: str | None = field(repr=False)
    label: str | None = field(repr=False)
    value: str | None = field(repr=False)
    visible: bool | None
    enabled: bool | None
    focused: bool | None
    bounds: tuple[float, float, float, float] | None
    ancestors: tuple[tuple[str, str | None, str | None], ...] = field(repr=False)
    path: str
    xpath: str = field(repr=False)

    @property
    def actionable(self) -> bool:
        return self.visible is True and self.enabled is True and self.bounds is not None

    def matches(self, selector: Selector) -> bool:
        return (self.role == selector.role
                and (selector.name is None or self.name == selector.name)
                and (selector.label is None or self.label == selector.label)
                and (selector.ancestor_label is None or any(label == selector.ancestor_label for _, _, label in self.ancestors)))

    def fingerprint(self) -> tuple[object, ...]:
        # Includes position in hierarchy AND geometry: identical labels moving
        # to another list item must not silently remap an old action.
        return (self.role, self.name, self.label, self.value, self.visible,
                self.enabled, self.bounds, self.ancestors, self.path)


@dataclass(frozen=True)
class Observation:
    id: str
    generation: int
    device_udid: str = field(repr=False)
    app: str
    captured_at: str
    started: float
    finished: float
    elements: tuple[Element, ...] = field(repr=False)
    secure: bool
    signature: str
    process_id: int | None = None

    def matches(self, selector: Selector) -> tuple[Element, ...]:
        return tuple(e for e in self.elements if e.visible is True and e.matches(selector))

    def unique(self, selector: Selector) -> Element | None:
        matches = self.matches(selector)
        return matches[0] if len(matches) == 1 else None


def parse_observation(source: str, *, generation: int, device_udid: str,
                      app: str, captured_at: str, started: float, finished: float,
                      process_id: int | None = None) -> Observation:
    if len(source.encode("utf-8")) > 2_000_000 or "<!DOCTYPE" in source or "<!ENTITY" in source:
        raise ObservationRejected("Accessibility source exceeds safe parsing limits.")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        raise ObservationRejected("Invalid accessibility XML.") from exc
    snapshot_id = uuid.uuid4().hex
    elements: list[Element] = []
    secure = False

    def walk(node: ET.Element, path: str, ancestors: tuple, depth: int) -> None:
        nonlocal secure
        if depth > 60 or len(elements) >= 2000:
            raise ObservationRejected("Accessibility tree exceeds limits; nothing was truncated into an actionable snapshot.")
        role = node.tag
        if not re.fullmatch(r"XCUIElementType[A-Za-z]+", role):
            raise ObservationRejected("Unrecognized accessibility node type.")
        attrs = node.attrib
        is_secure = role == "XCUIElementTypeSecureTextField"
        secure |= is_secure  # Even a hidden secure field prevents cloud inference.
        name, label = attrs.get("name"), attrs.get("label")
        value = None if is_secure else attrs.get("value")
        try:
            bounds = tuple(float(attrs[key]) for key in ("x", "y", "width", "height"))
            if not all(math.isfinite(v) for v in bounds) or min(bounds[2:]) <= 0:
                bounds = None
        except (KeyError, ValueError):
            bounds = None
        # XPath is local only, built from observed hierarchy plus exact source
        # attributes. Escaping prevents labels becoming executable predicates.
        checks = [f"@{key}={xpath_literal(attrs[key])}" for key in
                  ("name", "label", "value", "visible", "enabled", "x", "y", "width", "height")
                  if key in attrs and not (is_secure and key == "value")]
        xpath = path + ("[" + " and ".join(checks) + "]" if checks else "")
        elements.append(Element(f"{snapshot_id}:{len(elements)}", role, name, label, value,
                                boolean(attrs.get("visible")), boolean(attrs.get("enabled")),
                                boolean(attrs.get("focused")), bounds, ancestors, path, xpath))
        siblings: dict[str, int] = {}
        for child in node:
            siblings[child.tag] = siblings.get(child.tag, 0) + 1
            walk(child, f"{path}/{child.tag}[{siblings[child.tag]}]",
                 ancestors + ((role, name, label),), depth + 1)

    if root.tag == "AppiumAUT":
        for index, child in enumerate(root, 1):
            if len(root) != 1:
                raise ObservationRejected("Multiple accessibility applications are ambiguous.")
            walk(child, f"//{child.tag}[{index}]", (), 0)
    else:
        walk(root, f"//{root.tag}[1]", (), 0)
    if not elements or elements[0].role != "XCUIElementTypeApplication":
        raise ObservationRejected("Accessibility application root is missing.")
    signature = hashlib.sha256(json.dumps([e.fingerprint() for e in elements], ensure_ascii=False).encode()).hexdigest()
    return Observation(snapshot_id, generation, device_udid, app, captured_at,
                       started, finished, tuple(elements), secure, signature, process_id)
