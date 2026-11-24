"""Local Apple Silicon MLX runtime and System One HTTP server for Liquid AI d1-omni-600M.

Keeps core `openclaw-iphone-ops` zero-dependency by installing `mlx-vlm` into an isolated host venv
(`~/.openclaw/iphone/mlx-venv` by default) and serving `/v1/systemone` + `/health` on 127.0.0.1.
"""
from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import traceback
from typing import Any, Callable
import urllib.error
import urllib.request

from .config import IPhoneConfig
from .goal import is_loopback_url

DEFAULT_MLX_MODEL = "LiquidAI/d1-omni-600M-MLX-8bit"
DEFAULT_MLX_HOST = "127.0.0.1"
DEFAULT_MLX_PORT = 8781
DEFAULT_MLX_VENV_RELATIVE = Path(".openclaw/iphone/mlx-venv")
MLX_VLM_SPEC = "mlx-vlm>=0.7.8"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_IMAGE_ASSET_BYTES = 12 * 1024 * 1024


def default_mlx_venv(env: dict[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    home = source.get("HOME")
    base = Path(home) if home else Path.home()
    return base / DEFAULT_MLX_VENV_RELATIVE


def mlx_venv_path(config: IPhoneConfig | None = None, explicit: str | None = None) -> Path:
    raw = explicit or (config.get("OPENCLAW_IPHONE_MLX_VENV") if config else None)
    return Path(raw).expanduser() if raw else default_mlx_venv()


def mlx_model_from_config(config: IPhoneConfig | None = None, explicit: str | None = None) -> str:
    raw = explicit or (config.get("OPENCLAW_IPHONE_MLX_MODEL") if config else None)
    return (raw or DEFAULT_MLX_MODEL).strip() or DEFAULT_MLX_MODEL


def mlx_port_from_config(config: IPhoneConfig | None = None, explicit: int | None = None) -> int:
    if explicit is not None:
        port = explicit
    else:
        raw = config.get("OPENCLAW_IPHONE_MLX_PORT") if config else None
        port = int(raw) if raw else DEFAULT_MLX_PORT
    if not 1 <= port <= 65535:
        raise ValueError("MLX server port must be 1-65535.")
    return port


def mlx_python(venv_path: Path) -> Path:
    return venv_path / "bin" / "python"


def is_mlx_installed(venv_path: Path) -> bool:
    python = mlx_python(venv_path)
    if not python.is_file():
        return False
    try:
        probe = subprocess.run(
            [str(python), "-c", "import mlx_vlm; assert hasattr(mlx_vlm, 'load') and hasattr(mlx_vlm, 'predict')"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def build_install_commands(venv_path: Path, *, python_executable: str = sys.executable,
                           uv_path: str | None = None) -> list[list[str]]:
    """Build commands to create an isolated venv and install mlx-vlm + pillow."""
    python = str(mlx_python(venv_path))
    if uv_path:
        return [
            [uv_path, "venv", "--python", python_executable, str(venv_path)],
            [uv_path, "pip", "install", "--python", python, "-U", MLX_VLM_SPEC, "pillow"],
        ]
    return [
        [python_executable, "-m", "venv", str(venv_path)],
        [python, "-m", "pip", "install", "-U", "pip", MLX_VLM_SPEC, "pillow"],
    ]


def install_mlx_runtime(venv_path: Path, *, model: str = DEFAULT_MLX_MODEL,
                        prefetch: bool = False, python_executable: str = sys.executable,
                        runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Install mlx-vlm into `venv_path` and optionally prefetch MLX model weights."""
    venv_path = venv_path.expanduser().resolve()
    venv_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    uv_path = shutil.which("uv")
    commands = build_install_commands(venv_path, python_executable=python_executable, uv_path=uv_path)
    for command in commands:
        runner(command, check=True)
    if prefetch:
        runner(
            [str(mlx_python(venv_path)), "-c",
             "import sys; from mlx_vlm import load; load(sys.argv[1])", model],
            check=True,
        )
    return {
        "venv": str(venv_path),
        "python": str(mlx_python(venv_path)),
        "model": model,
        "installer": "uv" if uv_path else "pip",
        "prefetched": prefetch,
    }


def _extract_state_and_images(payload: dict[str, Any], temp_dir: Path) -> tuple[str | None, list[str]]:
    """Convert System One `state` + `assets` into `(state_text, image_paths)` for mlx_vlm.predict()."""
    raw_state = payload.get("state")
    assets = payload.get("assets") or {}
    if not isinstance(assets, dict):
        raise ValueError("assets must be an object.")

    image_paths: list[str] = []
    seen_refs: set[str] = set()

    def materialize_asset(ref: str) -> None:
        if ref in seen_refs:
            return
        asset = assets.get(ref)
        if not isinstance(asset, dict) or asset.get("type") != "image":
            raise ValueError(f"Unknown or non-image asset reference: {ref}")
        b64 = asset.get("data_base64") or asset.get("base64")
        if not isinstance(b64, str) or not b64:
            raise ValueError("Image asset missing data_base64.")
        try:
            raw_bytes = base64.b64decode(b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid base64 image asset.") from exc
        if len(raw_bytes) > MAX_IMAGE_ASSET_BYTES:
            raise ValueError("Image asset exceeds size limit.")
        fmt = str(asset.get("format") or "jpg").strip().lower()
        ext = ".png" if fmt == "png" else ".jpg"
        out = temp_dir / f"asset-{len(image_paths)}{ext}"
        out.write_bytes(raw_bytes)
        image_paths.append(str(out))
        seen_refs.add(ref)

    text_parts: list[str] = []
    if isinstance(raw_state, str):
        text_parts.append(raw_state)
    elif isinstance(raw_state, list):
        for item in raw_state:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    text_parts.append(item["text"])
                for key in ("screenshot", "image"):
                    ref = item.get(key)
                    if isinstance(ref, str) and ref in assets:
                        materialize_asset(ref)
    elif isinstance(raw_state, dict):
        for value in raw_state.values():
            if isinstance(value, str) and value in assets:
                materialize_asset(value)
        text_parts.append(json.dumps(raw_state, ensure_ascii=False))
    elif raw_state is not None:
        text_parts.append(str(raw_state))

    for ref, asset in assets.items():
        if ref not in seen_refs and isinstance(asset, dict) and asset.get("type") == "image":
            materialize_asset(ref)

    state_text = "\n".join(part for part in text_parts if part).strip() or None
    return state_text, image_paths


class D1MLXEngine:
    """Thread-safe lazy loader and System One evaluator backed by `mlx_vlm`."""

    def __init__(self, model_id: str = DEFAULT_MLX_MODEL, *,
                 predictor: Callable[[str | None, dict[str, Any], list[str]], dict[str, Any]] | None = None) -> None:
        self.model_id = model_id
        self._predictor = predictor
        self._model: Any = None
        self._processor: Any = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._predictor is not None or (self._model is not None and self._processor is not None)

    def _ensure_loaded(self) -> None:
        if self._predictor is not None or self._model is not None:
            return
        try:
            from mlx_vlm import load, predict  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "mlx-vlm is not installed in this Python interpreter. "
                "Run `openclaw-iphone d1 install` first."
            ) from exc
        model, processor = load(self.model_id)
        self._model, self._processor = model, processor

        def run_predict(state: str | None, questions: dict[str, Any], images: list[str]) -> dict[str, Any]:
            return predict(self._model, self._processor, state, questions, images=images or None)

        self._predictor = run_predict

    def warmup(self) -> None:
        """Load the model and run one tiny synthetic multimodal pass to compile MLX kernels."""
        # Valid 1x1 JPEG (160 bytes) — compiles both SigLIP2 vision encoder and LFM encoder kernels.
        tiny_jpg_b64 = (
            "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQ"
            "DQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/E"
            "ABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
        )
        self.predict_systemone({
            "state": [{"screenshot": "warmup"}, {"text": "Warmup check."}],
            "assets": {"warmup": {"type": "image", "format": "jpg", "data_base64": tiny_jpg_b64}},
            "questions": {"ok": {"type": "noul", "instructions": "Is this a warmup check?"}},
        })

    def predict_systemone(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        raw_questions = payload.get("questions")
        if not isinstance(raw_questions, dict) or not raw_questions:
            raise ValueError("Request must include a non-empty `questions` object.")
        questions = _normalize_questions(raw_questions)
        with tempfile.TemporaryDirectory(prefix="openclaw-d1-mlx-") as scratch:
            state_text, image_paths = _extract_state_and_images(payload, Path(scratch))
            with self._lock:
                self._ensure_loaded()
                assert self._predictor is not None
                raw = self._predictor(state_text, questions, image_paths)
        answers = raw.get("answers") if isinstance(raw, dict) and "answers" in raw else raw
        if not isinstance(answers, dict):
            raise RuntimeError("Unexpected mlx_vlm.predict() result shape.")
        normalized = {
            name: _normalize_answer(spec, answers[name])
            for name, spec in questions.items()
            if name in answers
        }
        return {"model": self.model_id, "answers": normalized}


def _normalize_questions(questions: dict[str, Any]) -> dict[str, Any]:
    """Normalize question specs for `mlx_vlm` (which requires `instructions`)."""
    out: dict[str, Any] = {}
    for name, spec in questions.items():
        if isinstance(spec, dict) and "instructions" not in spec and "prompt" in spec:
            copied = dict(spec)
            copied["instructions"] = copied["prompt"]
            out[name] = copied
        else:
            out[name] = spec
    return out


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalize_answer(question: Any, raw: Any) -> Any:
    """Translate `mlx_vlm.predict()` answers (or raw values) into the typed System One contract.

    Real `mlx_vlm` returns:
    - noul:   {"type": "bool", "value": True, "probability": 0.88}
    - choice: {"type": "choice", "value": "<label>", "probabilities": {...}, "metadata": {...}}
    - score:  {"type": "score", "value": 2.4, "probabilities": {...}, "metadata": {...}}
    """
    qtype = question.get("type") if isinstance(question, dict) else None
    if qtype == "noul":
        if isinstance(raw, dict) and raw.get("type") == "noul" and _is_number(raw.get("noul")):
            return raw
        if _is_number(raw):
            return {"type": "noul", "noul": float(raw)}
        if isinstance(raw, dict):
            val = raw.get("noul", raw.get("probability"))
            if _is_number(val):
                return {"type": "noul", "noul": float(val)}
    elif qtype == "choice":
        if isinstance(raw, dict):
            probs = raw.get("probabilities") if isinstance(raw.get("probabilities"), dict) else (
                None if "type" in raw else raw
            )
            if isinstance(probs, dict) and probs and all(_is_number(v) for v in probs.values()):
                clean = {str(k): float(v) for k, v in probs.items()}
                candidate = raw.get("choice", raw.get("value"))
                top = candidate if isinstance(candidate, str) and candidate in clean else max(clean, key=clean.__getitem__)
                return {"type": "choice", "choice": top, "probabilities": clean}
    elif qtype == "score":
        if isinstance(raw, dict) and raw.get("type") == "score" and _is_number(raw.get("score")):
            return raw
        if _is_number(raw):
            return {"type": "score", "score": float(raw)}
        if isinstance(raw, dict):
            val = raw.get("score", raw.get("value"))
            if _is_number(val):
                return {"type": "score", "score": float(val)}
    return raw


class D1MLXServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], engine: D1MLXEngine) -> None:
        self.engine = engine
        super().__init__(address, _D1MLXHandler)


class _D1MLXIPv6Server(D1MLXServer):
    address_family = socket.AF_INET6


class _D1MLXHandler(BaseHTTPRequestHandler):
    timeout = 30

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.rstrip("/") in {"/health", "/v1/health"}:
            engine = self.server.engine  # type: ignore[attr-defined]
            self._send_json(200, {
                "status": "ok",
                "model": engine.model_id,
                "backend": "mlx-vlm",
                "loaded": engine.loaded,
            })
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") not in {"/v1/systemone", "/systemone"}:
            self._send_json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid_content_length"})
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send_json(413 if length > MAX_REQUEST_BYTES else 400, {"error": "invalid_body_size"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except ValueError:
            self._send_json(400, {"error": "invalid_json"})
            return
        try:
            reply = self.server.engine.predict_systemone(payload)  # type: ignore[attr-defined]
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:
            print(f"d1-mlx inference error ({type(exc).__name__}): {exc}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            self._send_json(500, {"error": "inference_failed"})
            return
        self._send_json(200, reply)


def _format_url_host(host: str) -> str:
    clean = host.strip("[]")
    return f"[{clean}]" if ":" in clean else clean


def create_d1_server(host: str = DEFAULT_MLX_HOST, port: int = DEFAULT_MLX_PORT, *,
                     model_id: str = DEFAULT_MLX_MODEL, allow_non_loopback: bool = False,
                     engine: D1MLXEngine | None = None) -> D1MLXServer:
    clean_host = host.strip("[]")
    url_host = _format_url_host(clean_host)
    if not allow_non_loopback and not is_loopback_url(f"http://{url_host}:{port}"):
        raise ValueError("Refusing to bind d1 MLX server to a non-loopback address without --allow-non-loopback.")
    is_ipv6 = False
    try:
        is_ipv6 = ipaddress.ip_address(clean_host).version == 6
    except ValueError:
        pass
    server_cls = _D1MLXIPv6Server if is_ipv6 else D1MLXServer
    return server_cls((clean_host, port), engine or D1MLXEngine(model_id))


def probe_d1_health(base_url: str, *, timeout: float = 2.0) -> dict[str, Any] | None:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    url = f"{root}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.load(response)
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None
