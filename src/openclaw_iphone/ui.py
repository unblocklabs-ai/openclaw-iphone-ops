from __future__ import annotations

from pathlib import Path

from .evidence import artifact_path
from .wda import WDAClient


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


def output_path(output: str | None, prefix: str, suffix: str, evidence_base: str | None) -> Path:
    path = Path(output) if output else artifact_path(prefix, suffix, base=evidence_base)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
