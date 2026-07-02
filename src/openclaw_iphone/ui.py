from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
import html
import json
from pathlib import Path
import time
from typing import Any
from xml.etree import ElementTree as ET

from .evidence import artifact_path
from .errors import WDAUnavailable
from .wda import WDAClient


@dataclass(frozen=True)
class UIElement:
    index: int
    type: str
    name: str | None
    label: str | None
    value: str | None
    rect: dict[str, int | None]
    visible: bool | None
    enabled: bool | None

    @property
    def text(self) -> str:
        return " ".join(value for value in (self.label, self.name, self.value) if value)

    @property
    def center(self) -> tuple[float, float] | None:
        x = self.rect.get("x")
        y = self.rect.get("y")
        width = self.rect.get("width")
        height = self.rect.get("height")
        if None in (x, y, width, height):
            return None
        return (float(x + width / 2), float(y + height / 2))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["text"] = self.text
        center = self.center
        data["center"] = None if center is None else {"x": center[0], "y": center[1]}
        return data


class UIController:
    def __init__(self, client: WDAClient, *, evidence_base: str | None = None) -> None:
        self.client = client
        self.evidence_base = evidence_base

    def capture_source(self, output: str | None = None) -> Path:
        path = output_path(output, "wda-source", ".xml", self.evidence_base)
        path.write_text(self.client.source(), encoding="utf-8")
        return path

    def capture_screenshot(self, output: str | None = None) -> Path:
        path = output_path(output, "wda-screenshot", ".png", self.evidence_base)
        path.write_bytes(self.client.screenshot())
        return path

    def tap(self, x: float, y: float) -> None:
        self.client.tap(x, y)

    def type_text(self, text: str, *, frequency: int | None = None) -> None:
        self.client.type_text(text, frequency=frequency)

    def clear_field(self, query: str | None = None, *, exact: bool = False) -> UIElement | None:
        element = None
        if query:
            element = self.tap_text(query, exact=exact)
            time.sleep(0.2)
        clear_button = self.find_text("Clear", exact=True)
        if clear_button is not None and clear_button.center is not None:
            self.tap(clear_button.center[0], clear_button.center[1])
            return clear_button
        self.client.clear_text()
        return element

    def press_button(self, name: str, *, duration: float | None = None) -> None:
        self.client.press_button(name, duration=duration)

    def back(self) -> None:
        try:
            self.client.back()
            return
        except WDAUnavailable as exc:
            wda_error = exc

        for element in self.elements():
            text = element.text.lower()
            rect = element.rect
            is_backish = any(token in text for token in ("back", "close", "cancel", "dismiss"))
            is_top_left_button = element.type == "XCUIElementTypeButton" and (rect.get("x") or 0) < 90 and (rect.get("y") or 0) < 120
            if (is_backish or is_top_left_button) and element.center is not None:
                self.tap(element.center[0], element.center[1])
                return

        raise WDAUnavailable(f"No WDA back route or visible back/close control was available. Last WDA error: {wda_error}")

    def drag(self, from_x: float, from_y: float, to_x: float, to_y: float, *, duration: float = 0.1) -> None:
        self.client.drag(from_x, from_y, to_x, to_y, duration=duration)

    def elements(self, *, visible_only: bool = True) -> list[UIElement]:
        return parse_elements(self.client.source(), visible_only=visible_only)

    def save_elements(self, output: str | None = None, *, visible_only: bool = True) -> Path:
        path = output_path(output, "wda-elements", ".json", self.evidence_base)
        payload = [element.to_dict() for element in self.elements(visible_only=visible_only)]
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def find_text(self, query: str, *, exact: bool = False, visible_only: bool = True) -> UIElement | None:
        return find_element(self.elements(visible_only=visible_only), query, exact=exact)

    def tap_text(self, query: str, *, exact: bool = False) -> UIElement:
        element = self.find_text(query, exact=exact)
        if element is None:
            raise WDAUnavailable(f"No visible UI element matched text: {query!r}")
        center = element.center
        if center is None:
            raise WDAUnavailable(f"Matched UI element has no tappable frame: {query!r}")
        self.tap(center[0], center[1])
        return element

    def wait_text(self, query: str, *, timeout: float = 10.0, interval: float = 0.5, exact: bool = False) -> UIElement:
        deadline = time.monotonic() + timeout
        while True:
            element = self.find_text(query, exact=exact)
            if element is not None:
                return element
            if time.monotonic() >= deadline:
                raise WDAUnavailable(f"Timed out waiting for visible text: {query!r}")
            time.sleep(interval)

    def scroll_until_text(
        self,
        query: str,
        *,
        max_scrolls: int = 8,
        exact: bool = False,
        start_x: float = 200,
        start_y: float = 720,
        end_x: float = 200,
        end_y: float = 260,
        duration: float = 0.2,
    ) -> UIElement:
        for attempt in range(max_scrolls + 1):
            element = self.find_text(query, exact=exact)
            if element is not None:
                return element
            if attempt == max_scrolls:
                break
            self.drag(start_x, start_y, end_x, end_y, duration=duration)
            time.sleep(0.4)
        raise WDAUnavailable(f"Could not find visible text after {max_scrolls} scrolls: {query!r}")

    def annotated_screenshot(self, output: str | None = None, *, visible_only: bool = True) -> tuple[Path, Path, Path]:
        screenshot = output_path(output, "wda-annotated", ".png", self.evidence_base)
        screenshot.write_bytes(self.client.screenshot())
        elements = self.elements(visible_only=visible_only)

        json_path = screenshot.with_suffix(".elements.json")
        json_path.write_text(
            json.dumps([element.to_dict() for element in elements], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        html_path = screenshot.with_suffix(".html")
        html_path.write_text(render_annotation_html(screenshot, elements), encoding="utf-8")
        return screenshot, json_path, html_path


def parse_elements(source_text: str, *, visible_only: bool = True) -> list[UIElement]:
    root = ET.fromstring(source_text)
    elements: list[UIElement] = []
    for node in root.iter():
        name = attr(node, "name")
        label = attr(node, "label")
        value = attr(node, "value")
        rect = {
            key: parse_int(attr(node, key))
            for key in ("x", "y", "width", "height")
        }
        visible = parse_bool(attr(node, "visible"))
        enabled = parse_bool(attr(node, "enabled"))
        element = UIElement(
            index=len(elements),
            type=node.tag,
            name=name,
            label=label,
            value=value,
            rect=rect,
            visible=visible,
            enabled=enabled,
        )
        if visible_only and visible is False:
            continue
        if not element.text:
            continue
        elements.append(element)
    return elements


def find_element(elements: list[UIElement], query: str, *, exact: bool = False) -> UIElement | None:
    normalized = normalize(query)
    candidates: list[UIElement] = []
    for element in elements:
        values = [element.label, element.name, element.value, element.text]
        normalized_values = [normalize(value) for value in values if value]
        if exact:
            matched = normalized in normalized_values
        else:
            matched = any(normalized in value for value in normalized_values)
        if matched:
            candidates.append(element)
    return best_tappable_candidate(candidates)


def best_tappable_candidate(candidates: list[UIElement]) -> UIElement | None:
    if not candidates:
        return None
    with_center = [element for element in candidates if element.center is not None]
    pool = with_center or candidates
    tappable = [
        element
        for element in pool
        if element.type in {"XCUIElementTypeButton", "XCUIElementTypeCell", "XCUIElementTypeLink", "XCUIElementTypeTextField"}
    ]
    return (tappable or pool)[0]


def render_annotation_html(screenshot: Path, elements: list[UIElement]) -> str:
    image_data = base64.b64encode(screenshot.read_bytes()).decode("ascii")
    boxes = []
    for element in elements:
        x = element.rect.get("x")
        y = element.rect.get("y")
        width = element.rect.get("width")
        height = element.rect.get("height")
        if None in (x, y, width, height) or not element.text:
            continue
        title = html.escape(f"{element.index}: {element.text}")
        boxes.append(
            f'<div class="box" title="{title}" style="left:{x}px;top:{y}px;width:{width}px;height:{height}px">'
            f'<span>{html.escape(str(element.index))}</span></div>'
        )
    return (
        "<!doctype html><meta charset=\"utf-8\"><title>iPhone UI Annotation</title>"
        "<style>body{margin:0;background:#111;color:#fff;font-family:system-ui,sans-serif}.wrap{position:relative;display:inline-block}"
        "img{display:block}.box{position:absolute;border:2px solid #ffcc00;box-sizing:border-box;pointer-events:auto}"
        ".box span{position:absolute;left:0;top:0;background:#ffcc00;color:#111;font-size:11px;padding:1px 3px}</style>"
        f'<div class="wrap"><img src="data:image/png;base64,{image_data}">'
        + "".join(boxes)
        + "</div>"
    )


def normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def attr(element: ET.Element, name: str) -> str | None:
    value = element.attrib.get(name)
    return value if value else None


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return None


def output_path(output: str | None, prefix: str, suffix: str, evidence_base: str | None) -> Path:
    path = Path(output) if output else artifact_path(prefix, suffix, base=evidence_base)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
