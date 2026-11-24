"""Owner-only host caches for fast session startup, app resolution, and watchdog state."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterator

WDA_URL_TTL_SECONDS = 6 * 3600
APPS_TTL_SECONDS = 10 * 60
LAST_APP_TTL_SECONDS = 30 * 60


def runtime_cache_path() -> Path:
    return Path.home() / ".openclaw/iphone/runtime-cache.json"


def watchdog_state_path() -> Path:
    return Path.home() / ".openclaw/iphone/watchdog-state.json"


@contextmanager
def _locked_cache(path: Path) -> Iterator[None]:
    """Serialize read-modify-write cache updates across concurrent CLI/session processes."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = path.parent / f".{path.name}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError:
        pass


def get_cached_wda_endpoint(selector: str | None) -> dict[str, Any] | None:
    """Return cached device metadata + WDA URL if fresh and unambiguous for `selector`."""
    data = _read_json(runtime_cache_path())
    endpoints = data.get("wda_endpoints")
    if not isinstance(endpoints, dict):
        return None
    now = time.time()
    fresh = [
        entry for entry in endpoints.values()
        if isinstance(entry, dict)
        and isinstance(entry.get("updated_at"), (int, float))
        and not isinstance(entry.get("updated_at"), bool)
        and now - entry["updated_at"] < WDA_URL_TTL_SECONDS
        and isinstance(entry.get("url"), str) and entry["url"]
        and isinstance(entry.get("udid"), str) and entry["udid"]
        and isinstance(entry.get("identifier"), str) and entry["identifier"]
    ]
    if not fresh:
        return None
    if not selector:
        return fresh[0] if len(fresh) == 1 else None
    needle = selector.casefold()
    matches = [
        entry for entry in fresh
        if needle in {
            str(entry.get("udid", "")).casefold(),
            str(entry.get("identifier", "")).casefold(),
            str(entry.get("name", "")).casefold(),
        }
    ]
    return matches[0] if len(matches) == 1 else None


def set_cached_wda_endpoint(*, udid: str, identifier: str, name: str, model: str, url: str) -> None:
    if not udid or not identifier or not url:
        return
    path = runtime_cache_path()
    with _locked_cache(path):
        data = _read_json(path)
        endpoints = data.get("wda_endpoints")
        if not isinstance(endpoints, dict):
            endpoints = {}
        endpoints[udid] = {
            "udid": udid,
            "identifier": identifier,
            "name": name,
            "model": model,
            "url": url,
            "updated_at": time.time(),
        }
        data["wda_endpoints"] = endpoints
        _write_json(path, data)


def clear_cached_wda_endpoint(udid: str | None) -> None:
    if not udid:
        return
    path = runtime_cache_path()
    with _locked_cache(path):
        data = _read_json(path)
        endpoints = data.get("wda_endpoints")
        if isinstance(endpoints, dict) and udid in endpoints:
            endpoints.pop(udid, None)
            _write_json(path, data)


def get_cached_apps(device_id: str) -> list[dict[str, str]] | None:
    data = _read_json(runtime_cache_path())
    apps_by_device = data.get("apps")
    if not isinstance(apps_by_device, dict):
        return None
    entry = apps_by_device.get(device_id)
    if not isinstance(entry, dict):
        return None
    updated = entry.get("updated_at")
    items = entry.get("items")
    if (
        not isinstance(updated, (int, float))
        or isinstance(updated, bool)
        or time.time() - updated >= APPS_TTL_SECONDS
    ):
        return None
    if not isinstance(items, list):
        return None
    cleaned: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        if not isinstance(item.get("name"), str) or not isinstance(item.get("bundle_identifier"), str):
            return None
        cleaned.append({
            "name": item["name"],
            "bundle_identifier": item["bundle_identifier"],
            "version": item["version"] if isinstance(item.get("version"), str) else "",
            "bundle_version": item["bundle_version"] if isinstance(item.get("bundle_version"), str) else "",
        })
    return cleaned


def set_cached_apps(device_id: str, items: list[dict[str, str]]) -> None:
    if not device_id:
        return
    path = runtime_cache_path()
    with _locked_cache(path):
        data = _read_json(path)
        apps_by_device = data.get("apps")
        if not isinstance(apps_by_device, dict):
            apps_by_device = {}
        apps_by_device[device_id] = {"updated_at": time.time(), "items": items}
        data["apps"] = apps_by_device
        _write_json(path, data)


def get_cached_last_app(udid: str | None) -> str | None:
    if not udid:
        return None
    data = _read_json(runtime_cache_path())
    last_apps = data.get("last_apps")
    if not isinstance(last_apps, dict):
        return None
    entry = last_apps.get(udid)
    if not isinstance(entry, dict):
        return None
    updated = entry.get("updated_at")
    bundle_id = entry.get("bundle_id")
    if (
        not isinstance(updated, (int, float))
        or isinstance(updated, bool)
        or time.time() - updated >= LAST_APP_TTL_SECONDS
        or not isinstance(bundle_id, str)
        or not bundle_id
    ):
        return None
    return bundle_id


def set_cached_last_app(udid: str | None, bundle_id: str | None) -> None:
    if not udid or not bundle_id:
        return
    path = runtime_cache_path()
    with _locked_cache(path):
        data = _read_json(path)
        last_apps = data.get("last_apps")
        if not isinstance(last_apps, dict):
            last_apps = {}
        last_apps[udid] = {"bundle_id": bundle_id, "updated_at": time.time()}
        data["last_apps"] = last_apps
        _write_json(path, data)


def update_watchdog_failures(device_id: str, *, failed: bool) -> int:
    """Increment or reset consecutive watchdog failure count for `device_id`."""
    path = watchdog_state_path()
    with _locked_cache(path):
        data = _read_json(path)
        entry = data.get(device_id) if isinstance(data.get(device_id), dict) else {}
        try:
            previous = int(entry.get("consecutive_failures", 0))
        except (TypeError, ValueError):
            previous = 0
        count = max(0, previous) + 1 if failed else 0
        data[device_id] = {"consecutive_failures": count, "updated_at": time.time()}
        _write_json(path, data)
        return count
