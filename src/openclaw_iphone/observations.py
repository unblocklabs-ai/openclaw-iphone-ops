"""Local accessibility snapshots. No source text is implicitly cloud-safe."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
import json
import math
import re
import uuid
from typing import Callable
from xml.etree import ElementTree as ET

from .errors import OpenClawIPhoneError


class ObservationRejected(OpenClawIPhoneError):
    """Observation is incomplete, ambiguous, or no longer valid for an action."""




LAYOUT = frozenset({"XCUIElementTypeOther", "XCUIElementTypeWebView"})
CHECKABLE = frozenset({"XCUIElementTypeSwitch", "XCUIElementTypeCheckBox", "XCUIElementTypeRadioButton", "XCUIElementTypeToggleButton"})


def boolean(value: str | None) -> bool | None:
    return {"true": True, "false": False, "1": True, "0": False}.get(value)


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in value.split("'")) + ")"


def predicate_literal(value: str | int | float) -> str:
    # NSPredicate quoted strings, never caller-provided predicate expressions.
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class Selector:
    role: str
    name: str | None = None
    label: str | None = None
    ancestor_label: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"XCUIElementType[A-Za-z]+", self.role):
            raise ValueError("Selector requires an exact XCUIElementType role.")
        if any(value is not None and (not isinstance(value, str) or not value)
               for value in (self.name, self.label, self.ancestor_label)):
            raise ValueError("Selector labels must be non-empty strings.")

    def xpath(self) -> str:
        """Live predicate lookup, not a durable action reference.

        No @visible: in XPath it makes WDA compute visibility for every node. Callers
        check visibility on the matches instead.
        """
        checks = ["@enabled='true'"]
        checks += [f"@{key}={xpath_literal(value)}" for key, value in
                   (("name", self.name), ("label", self.label)) if value is not None]
        if self.ancestor_label is not None:
            checks.append(f"ancestor::*[@label={xpath_literal(self.ancestor_label)}]")
        return f"//{self.role}[{' and '.join(checks)}]"

    def locator(self) -> tuple[str, str]:
        if self.ancestor_label is not None:
            return "xpath", self.xpath()
        # AND short-circuits: visibility (one AX query per node) is asked only of matches.
        checks = [f"type == {predicate_literal(self.role)}", "enabled == 1"]
        checks += [f"{key} == {predicate_literal(value)}" for key, value in
                   (("name", self.name), ("label", self.label)) if value is not None]
        return "predicate string", " AND ".join(checks + ["visible == 1"])


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
    selected: bool | None = None

    def matches(self, selector: Selector) -> bool:
        return (self.role == selector.role
                and (selector.name is None or self.name == selector.name)
                and (selector.label is None or self.label == selector.label)
                and (selector.ancestor_label is None or any(label == selector.ancestor_label for _, _, label in self.ancestors)))

    def locator(self) -> tuple[str, str]:
        named_ancestor = any(role != "XCUIElementTypeApplication" and (name or label)
                             for role, name, label in self.ancestors)
        if named_ancestor:
            def step(role: str, checks: list[tuple[str, str | int | float | None]]) -> str:
                predicates = [f"{key} == {predicate_literal(value)}" for key, value in checks if value is not None]
                return role + ("[`" + " AND ".join(predicates).replace("`", "``") + "`]" if predicates else "")

            # Direct-child roles and named ancestors preserve row/form context.
            # Geometry/value changes do not change a named target's identity.
            parts = [step(role, [("name", name), ("label", label)])
                     for role, name, label in self.ancestors[1:]]
            parts.append(step(self.role, [("name", self.name), ("label", self.label),
                                          ("enabled", 1), ("visible", 1)]))
            return "class chain", "/".join(parts)
        checks = [f"type == {predicate_literal(self.role)}", "enabled == 1"]
        checks += [f"{key} == {predicate_literal(value)}" for key, value in
                   (("name", self.name), ("label", self.label)) if value]
        return "predicate string", " AND ".join(checks + ["visible == 1"])


@dataclass(frozen=True)
class Observation:
    id: str
    generation: int
    device_udid: str = field(repr=False)
    app: str
    captured_at: str
    started: float
    finished: float
    # None means app identity only, not an empty or non-secure screen.
    elements: tuple[Element, ...] | None = field(repr=False)
    secure: bool | None
    process_id: int | None = None

    def matches(self, selector: Selector) -> tuple[Element, ...]:
        if self.elements is None:
            raise ObservationRejected("App-only observation has no accessibility evidence; observe the full screen first.")
        return tuple(e for e in self.elements if e.visible is True and e.matches(selector))

    def locator(self, element: Element) -> tuple[str, str]:
        locator = element.locator()
        if (element.name or element.label) and sum(e.locator() == locator for e in self.elements or ()) == 1:
            return locator
        # Ambiguous/unnamed IDs denote a position in this captured hierarchy.
        # Construct the positional query only when needed, retaining named
        # ancestors and exact empty identity. Never guess from coordinates.
        parts = element.path.removeprefix("//").split("/")
        identities = list(element.ancestors) + [(element.role, element.name, element.label)]
        for index, (_, name, label) in enumerate(identities):
            checks = []
            for key, value in (("name", name), ("label", label)):
                if index == 0 or identities[index][0] == "XCUIElementTypeSecureTextField":
                    continue
                checks.append(f"@{key}={xpath_literal(value)}" if value else f"(not(@{key}) or @{key}='')")
            if index == len(parts) - 1:
                checks.append("@enabled='true'")
            if checks:
                parts[index] += "[" + " and ".join(checks) + "]"
        return "xpath", "//" + "/".join(parts)

    @cached_property
    def projection(self) -> tuple[tuple[Element, str | None], ...]:
        elements = self.elements or ()
        parents = {e.path.rsplit("/", 1)[0] for e in elements}
        projected: dict[str, Element] = {}
        screen = []
        for element in elements:
            if element.visible is not True or element.role in {"XCUIElementTypeApplication", "XCUIElementTypeWindow"}:
                continue
            if element.role in LAYOUT and not (element.name or element.label) and (element.path in parents or not element.bounds):
                continue
            parent_path = element.path.rsplit("/", 1)[0]
            while parent_path and parent_path not in projected:
                parent_path = parent_path.rsplit("/", 1)[0]
            parent = projected.get(parent_path)
            if element.role == "XCUIElementTypeStaticText" and element.label and parent and parent.label == element.label:
                projected[element.path] = parent
                continue
            projected[element.path] = element
            screen.append((element, parent.id if parent else None))
        return tuple(screen)

    @cached_property
    def counts(self) -> dict[str, int]:
        elements = self.elements or ()
        visible = [e for e in elements if e.visible is True]
        return {"source_nodes": len(elements), "visible": len(visible),
                "unnamed_visible": sum(not (e.name or e.label) for e in visible),
                "unknown_visibility": sum(e.visible is None for e in elements),
                "screen_elements": len(self.projection)}

    def compact(self, *, include_labels: bool = True, limit: int = 80, offset: int = 0,
                redact: Callable[[str], str] = str, ids: set[str] | None = None) -> dict[str, object]:
        """Caller-facing local projection, not a cloud-sanitization API.

        Text values never leave this projection. Labels may disclose private content: even a
        non-secure screen can contain private messages or credentials.
        Display truncation never changes the full source/targets.
        """
        projection = self.projection
        if ids is not None:
            ids = set(ids)
            parent_by_id = {element.id: parent for element, parent in projection}
            for element_id in list(ids):
                parent = parent_by_id.get(element_id)
                while parent:
                    ids.add(parent)
                    parent = parent_by_id.get(parent)
            projection = tuple((e, p) for e, p in projection if e.id in ids)
        rows, size = [], 0
        for element, parent in projection[offset:offset + limit]:
            label = redact(element.label or "")[:256]
            row = {"id": element.id, "role": element.role, "enabled": element.enabled,
                   "bounds": element.bounds}
            if parent:
                row["parent"] = parent
            if element.focused is not None:
                row["focused"] = element.focused
            if element.selected:
                row["selected"] = True
            if element.role in CHECKABLE and (checked := boolean(element.value)) is not None:
                row["checked"] = checked
            if include_labels and element.role != "XCUIElementTypeSecureTextField":
                if label:
                    row["label"] = label
                if element.name and element.name != element.label:
                    row["name"] = redact(element.name)[:256]
            # Leave room for the envelope, image metadata and action receipts.
            row_size = len(json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode()) + 1
            if size + row_size > 48_000:
                break
            rows.append(row)
            size += row_size
        end = offset + len(rows)
        return {"snapshot_id": self.id, "captured_at": self.captured_at, "app": self.app,
                "process_id": self.process_id, "generation": self.generation,
                "capture_seconds": self.finished - self.started, "secure": self.secure,
                "accessibility_observed": self.elements is not None,
                "counts": self.counts,
                "elements": rows, "omitted_elements": max(0, len(projection) - end),
                "offset": offset, "next_offset": end if len(projection) > end else None,
                "labels_included": include_labels,
                "values_included": False}


def element_bounds(attrs: dict[str, str]) -> tuple[float, float, float, float] | None:
    try:
        bounds = tuple(float(attrs[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, ValueError):
        return None
    return bounds if all(math.isfinite(v) for v in bounds) and min(bounds[2:]) > 0 else None


def parse_observation(source: str, *, generation: int, device_udid: str,
                      captured_at: str, started: float, finished: float) -> Observation:
    """The application root's bundleId/processId identify the app.

    A source read without `visible` (see WDAClient.source(compact=True)) gets visibility from
    geometry: on screen means the bounds overlap the application's frame. Content covered by a
    sheet or a stacked screen then counts as visible; the screenshot shows what is on top.
    """
    if len(source.encode("utf-8")) > 2_000_000 or "<!DOCTYPE" in source or "<!ENTITY" in source:
        raise ObservationRejected("Accessibility source exceeds safe parsing limits.")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        raise ObservationRejected("Invalid accessibility XML.") from exc
    application = root[0] if root.tag == "AppiumAUT" and len(root) == 1 else root
    screen = app = process_id = None
    if application.tag == "XCUIElementTypeApplication":
        app = application.get("bundleId")
        pid = application.get("processId")
        if pid is not None and (len(pid) > 20 or not pid.isascii() or not pid.isdecimal() or int(pid) <= 0):
            raise ObservationRejected("Accessibility source has an invalid process identity.")
        if pid is not None:
            process_id = int(pid)
        screen = element_bounds(application.attrib)
    if not app:
        raise ObservationRejected("Foreground app identity is unavailable.")
    snapshot_id = uuid.uuid4().hex[:12]
    elements: list[Element] = []
    secure = False

    def walk(node: ET.Element, path: str, ancestors: tuple, depth: int) -> None:
        nonlocal secure
        if depth > 60:
            raise ObservationRejected("Accessibility tree exceeds limits; nothing was truncated into an actionable snapshot.")
        role = node.tag
        if not re.fullmatch(r"XCUIElementType[A-Za-z]+", role):
            raise ObservationRejected("Unrecognized accessibility node type.")
        attrs = node.attrib
        is_secure = role == "XCUIElementTypeSecureTextField"
        secure |= is_secure  # Even a hidden secure field prevents cloud inference.
        name, label = (None, None) if is_secure else (attrs.get("name"), attrs.get("label"))
        value = None if is_secure else attrs.get("value")
        bounds = element_bounds(attrs)
        visible = boolean(attrs.get("visible"))
        if "visible" not in attrs and screen is not None:
            visible = bounds is not None and (bounds[0] < screen[0] + screen[2] and screen[0] < bounds[0] + bounds[2]
                                              and bounds[1] < screen[1] + screen[3] and screen[1] < bounds[1] + bounds[3])
        selected = "Selected" in (attrs.get("traits") or "").split(", ")
        elements.append(Element(f"{snapshot_id}:{len(elements)}", role, name, label, value,
                                visible, boolean(attrs.get("enabled")),
                                boolean(attrs.get("focused")), bounds, ancestors, path, selected))
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
    return Observation(snapshot_id, generation, device_udid, app, captured_at,
                       started, finished, tuple(elements), secure, process_id)
