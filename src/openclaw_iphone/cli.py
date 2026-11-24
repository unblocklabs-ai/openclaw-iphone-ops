from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import math
import os
import plistlib
from pathlib import Path
import shutil
import subprocess
import sys
import time

from . import __version__, cache
from .config import (CONFIG_ENV_VAR, IPhoneConfig, config_path, default_config_path, expand_user_path, load_config,
                     update_config_file)
from .connection import Connection
from .control_lock import control_lock
from .d1_mlx import (DEFAULT_MLX_HOST, DEFAULT_MLX_MODEL, DEFAULT_MLX_PORT, create_d1_server, install_mlx_runtime,
                     is_mlx_installed, mlx_model_from_config, mlx_port_from_config, mlx_python, mlx_venv_path,
                     probe_d1_health)
from .devicectl import Device, DeviceCtl
from .evidence import artifact_path, write_private
from .errors import OpenClawIPhoneError, SessionOutputUnavailable, WDAUnavailable, diagnostic
from .goal import CHECK, decisions_from_config, goal_navigation, goal_provider, is_loopback_url, read_key_file
from .media import fetch_video
from .navigator import Unavailable
from .protocol import json_line_emitter, read_requests, serve
from .session import Session
from .wda import (DEFAULT_WDA_PORT, WDAClient, WDARunConfig, build_wda, default_derived_data_path, find_xctestrun,
                  resolve_wda_path, run_wda)

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "handler"):
        parser.print_help()
        return 2

    try:
        validate_numeric_args(args)
        # Read-only observers and the long-lived runner do not hold this lock.
        mutating = (
            args.command == "watchdog"
            or args.command == "apps" and args.apps_command == "terminate"
            or args.command == "wda" and args.wda_command in {"unlock", "lock", "pasteboard"}
            or args.command == "media" and args.media_command == "fetch" and getattr(args, "from_pasteboard", False)
        )
        with control_lock() if mutating else nullcontext():
            return args.handler(args)
    except OpenClawIPhoneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

def validate_numeric_args(args: argparse.Namespace) -> None:
    for name, value in vars(args).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name.replace('_', '-')} must be finite and non-negative.")
        if ("timeout" in name or "deadline" in name or name in {"interval", "frequency"}) and value <= 0:
            raise ValueError(f"{name.replace('_', '-')} must be positive.")

def selected_device(args: argparse.Namespace, client: DeviceCtl, *, config: IPhoneConfig | None = None) -> Device:
    if not hasattr(args, "_selected_device"):
        args._selected_device = client.select_device(device_selector_from_args(args, config=config))
    return args._selected_device

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openclaw-iphone",
        description="Reusable primitives for controlling a USB-connected iPhone.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--developer-dir", help="Override DEVELOPER_DIR for Xcode/devicectl.")
    parser.add_argument("--evidence-dir", help="Directory for JSON evidence artifacts.")
    parser.add_argument("--timeout", type=int, default=30, help="External command timeout in seconds.")
    parser.add_argument("--read-timeout", type=float, help="Optional shorter read-only WDA timeout; otherwise use the request/operation deadline.")

    subcommands = parser.add_subparsers(dest="command")

    devices = subcommands.add_parser("devices", help="Device discovery commands.")
    device_subcommands = devices.add_subparsers(dest="devices_command")
    devices_list = device_subcommands.add_parser("list", help="List connected devices.")
    devices_list.set_defaults(handler=handle_devices_list)

    doctor = subcommands.add_parser("doctor", help="Run a read-only iPhone control health check.")
    add_device_arg(doctor)
    add_wda_url_arg(doctor)
    doctor.add_argument("--check-ui", action="store_true", help="Also probe accessibility capture; /status alone does not prove read health.")
    doctor.set_defaults(handler=handle_doctor)

    apps = subcommands.add_parser("apps", help="Installed app and process commands.")
    apps_subcommands = apps.add_subparsers(dest="apps_command")

    apps_list = apps_subcommands.add_parser("list", help="List installed apps.")
    add_device_arg(apps_list)
    apps_list.add_argument("--no-all", action="store_true", help="Do not pass --include-all-apps.")
    apps_list.set_defaults(handler=handle_apps_list)

    apps_find = apps_subcommands.add_parser("find", help="Resolve an installed app by name or bundle id.")
    add_device_arg(apps_find)
    apps_find.add_argument("query")
    apps_find.set_defaults(handler=handle_apps_find)

    apps_launch = apps_subcommands.add_parser("launch", help="Launch an installed app by name or bundle id.")
    add_device_arg(apps_launch)
    apps_launch.add_argument("query")
    apps_launch.set_defaults(handler=handle_apps_launch)

    apps_terminate = apps_subcommands.add_parser("terminate", help="Terminate an installed app by name or bundle id.")
    add_device_arg(apps_terminate)
    apps_terminate.add_argument("query")
    apps_terminate.set_defaults(handler=handle_apps_terminate)

    wda = subcommands.add_parser("wda", help="WebDriverAgent commands.")
    wda_subcommands = wda.add_subparsers(dest="wda_command")
    wda_status = wda_subcommands.add_parser("status", help="Check whether WebDriverAgent is reachable.")
    add_device_arg(wda_status)
    add_wda_url_arg(wda_status)
    wda_status.add_argument("--output", help="Optional path for raw status JSON.")
    wda_status.set_defaults(handler=handle_wda_status)

    wda_url = wda_subcommands.add_parser("url", help="Resolve the CoreDevice WebDriverAgent URL.")
    add_device_arg(wda_url)
    wda_url.add_argument("--port", type=int, default=DEFAULT_WDA_PORT)
    wda_url.set_defaults(handler=handle_wda_url)

    wda_locked = wda_subcommands.add_parser("locked", help="Check WDA-reported screen lock state.")
    add_device_arg(wda_locked)
    add_wda_url_arg(wda_locked)
    wda_locked.set_defaults(handler=handle_wda_locked)

    wda_unlock = wda_subcommands.add_parser("unlock", help="Best-effort WDA unlock attempt.")
    add_wda_url_arg(wda_unlock)
    add_device_arg(wda_unlock)
    wda_unlock.add_argument(
        "--verify",
        action="store_true",
        help="Verify passcode lock state with devicectl after the WDA unlock attempt.",
    )
    wda_unlock.set_defaults(handler=handle_wda_unlock)

    wda_lock = wda_subcommands.add_parser("lock", help="Lock the phone through WDA. Mostly for diagnostics.")
    add_device_arg(wda_lock)
    add_wda_url_arg(wda_lock)
    wda_lock.set_defaults(handler=handle_wda_lock)

    wda_pasteboard = wda_subcommands.add_parser(
        "pasteboard",
        help="Read the iPhone clipboard via WDA (briefly foregrounds the WDA runner on iOS 16+ when needed).",
    )
    add_device_arg(wda_pasteboard)
    add_wda_url_arg(wda_pasteboard)
    wda_pasteboard.add_argument("--content-type", choices=("plaintext", "url"), default="plaintext")
    wda_pasteboard.add_argument("--runner-bundle-id", help="Override OPENCLAW_IPHONE_RUNNER_BUNDLE_ID for foreground swap.")
    wda_pasteboard.add_argument("--return-app", help="Bundle ID to reactivate after reading pasteboard.")
    wda_pasteboard.add_argument("--no-foreground-runner", action="store_true",
                                help="Read pasteboard without briefly foregrounding the WDA runner.")
    wda_pasteboard.set_defaults(handler=handle_wda_pasteboard)

    wda_run = wda_subcommands.add_parser(
        "run",
        help="Build and run WebDriverAgentRunner as a long-lived xcodebuild test process.",
    )
    add_device_arg(wda_run)
    wda_run.add_argument(
        "--wda-path",
        help="Debug override for WebDriverAgent checkout/project. Defaults to host config or OPENCLAW_IPHONE_WDA_PATH.",
    )
    wda_run.add_argument("--scheme", default="WebDriverAgentRunner")
    wda_run.add_argument("--configuration", default="Debug")
    wda_run.add_argument("--destination-timeout", type=int)
    wda_run.add_argument("--development-team", help="Debug override for the Apple Developer Team ID passed to xcodebuild.")
    wda_run.add_argument(
        "--runner-bundle-id",
        help="Debug override for the WebDriverAgentRunner bundle identifier.",
    )
    wda_run.add_argument(
        "--allow-provisioning-updates",
        action="store_true",
        help="Pass -allowProvisioningUpdates to xcodebuild for automatic signing.",
    )
    wda_run.add_argument(
        "--xctestrun",
        help="Path to a prebuilt .xctestrun bundle to launch with `xcodebuild test-without-building` (~2-3s restart).",
    )
    wda_run.add_argument(
        "--without-building",
        action="store_true",
        help="Reuse the latest prebuilt .xctestrun from `openclaw-iphone wda build` (falls back to build if absent).",
    )
    wda_run.set_defaults(handler=handle_wda_run)

    wda_build = wda_subcommands.add_parser(
        "build",
        help="Build WebDriverAgentRunner once (`build-for-testing`) so `wda run --without-building` restarts in ~2-3s.",
    )
    add_device_arg(wda_build)
    wda_build.add_argument("--wda-path", help="WebDriverAgent checkout/project path.")
    wda_build.add_argument("--scheme", default="WebDriverAgentRunner")
    wda_build.add_argument("--configuration", default="Debug")
    wda_build.add_argument("--destination-timeout", type=int)
    wda_build.add_argument("--development-team", help="Apple Developer Team ID passed to xcodebuild.")
    wda_build.add_argument("--runner-bundle-id", help="WebDriverAgentRunner bundle identifier.")
    wda_build.add_argument("--derived-data-path", help="DerivedData path (default: ~/.openclaw/iphone/DerivedData).")
    wda_build.add_argument("--allow-provisioning-updates", action="store_true")
    wda_build.add_argument("--allow-provisioning-device-registration", action="store_true")
    wda_build.add_argument("--save-config", action="store_true", help="Save the produced OPENCLAW_IPHONE_WDA_XCTESTRUN in config.env.")
    wda_build.set_defaults(handler=handle_wda_build)

    watchdog = subcommands.add_parser("watchdog", help="Lock-state recovery checks for unattended iPhone control.")
    watchdog_subcommands = watchdog.add_subparsers(dest="watchdog_command")
    watchdog_once = watchdog_subcommands.add_parser("once", help="Run one conservative lock-state recovery pass.")
    add_device_arg(watchdog_once)
    add_wda_url_arg(watchdog_once)
    watchdog_once.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip CoreDevice passcode verification after a WDA unlock attempt.",
    )
    watchdog_once.add_argument(
        "--restart-after",
        type=int,
        help="Optional consecutive WDA failure threshold before kickstarting com.openclaw.iphone-wda-run (0 disables).",
    )
    watchdog_once.set_defaults(handler=handle_watchdog_once)

    d1 = subcommands.add_parser("d1", help="Local Apple Silicon MLX runtime for Liquid AI d1-omni-600M.")
    d1_subcommands = d1.add_subparsers(dest="d1_command")
    d1_install = d1_subcommands.add_parser("install", help="Install mlx-vlm into an isolated host venv for d1-omni-600M.")
    d1_install.add_argument("--model", default=None, help=f"Hugging Face MLX model id (default: {DEFAULT_MLX_MODEL}).")
    d1_install.add_argument("--venv", help="Isolated venv path (default: ~/.openclaw/iphone/mlx-venv).")
    d1_install.add_argument("--port", type=int, help=f"Loopback server port (default: {DEFAULT_MLX_PORT}).")
    d1_install.add_argument("--prefetch", action="store_true", help="Pre-download the MLX model weights into Hugging Face cache.")
    d1_install_mode = d1_install.add_mutually_exclusive_group()
    d1_install_mode.add_argument("--configure-navigator", action="store_true",
                                 help="Configure goal navigation to use this local MLX d1 server.")
    d1_install_mode.add_argument("--configure-guard", action="store_true",
                                 help="Configure this local MLX d1 server as the local privacy guard ahead of a cloud provider.")
    d1_install.set_defaults(handler=handle_d1_install)

    d1_serve = d1_subcommands.add_parser("serve", help="Run the loopback System One HTTP server backed by mlx-vlm.")
    d1_serve.add_argument("--host", default=DEFAULT_MLX_HOST, help=f"Bind address (default: {DEFAULT_MLX_HOST}).")
    d1_serve.add_argument("--port", type=int, help=f"Listen port (default: {DEFAULT_MLX_PORT}).")
    d1_serve.add_argument("--model", help=f"MLX model id (default: {DEFAULT_MLX_MODEL}).")
    d1_serve.add_argument("--venv", help="Isolated MLX venv path.")
    d1_serve.add_argument("--warmup", action="store_true", help="Load the model and compile MLX kernels before serving.")
    d1_serve.add_argument("--allow-non-loopback", action="store_true", help="Explicitly allow binding outside 127.0.0.1/localhost.")
    d1_serve.set_defaults(handler=handle_d1_serve)

    d1_status = d1_subcommands.add_parser("status", help="Check local MLX d1-omni-600M installation and live server health.")
    d1_status.add_argument("--venv", help="Isolated MLX venv path.")
    d1_status.add_argument("--port", type=int, help=f"Server port to probe (default: {DEFAULT_MLX_PORT}).")
    d1_status.set_defaults(handler=handle_d1_status)

    goal = subcommands.add_parser("goal", help="Optional goal navigation through OpenAI, TypeSafe, or Liquid AI (LFM-d1).")
    goal_subcommands = goal.add_subparsers(dest="goal_command")
    goal_setup = goal_subcommands.add_parser("setup", help="Turn goal navigation on (or off) and configure its provider/key.")
    goal_setup.add_argument("--provider", choices=("openai", "typesafe", "liquid"),
                            help="Defaults to the configured provider, initially openai.")
    goal_setup.add_argument("--model", help="Optional model override (e.g., d1.2, d1.2-ultra, d1-omni-2B, gpt-6-luna).")
    goal_setup.add_argument("--base-url", help="Optional provider API base URL (e.g., local loopback URL for d1-omni).")
    goal_setup.add_argument("--local-guard-url", help="Optional local loopback d1-omni URL to screen for passwords/OTPs before cloud calls.")
    goal_setup.add_argument("--local-guard-model", help="Optional model name for the local privacy guard (default: d1-omni-600M).")
    key = goal_setup.add_mutually_exclusive_group()
    key.add_argument("--key-stdin", action="store_true", help="Read the API key from stdin and store it in the config file (owner-only).")
    key.add_argument("--key-file", help="Use the key in this file (a bare key or PROVIDER_API_KEY= line); the key is not copied.")
    key.add_argument("--disable", action="store_true", help="Turn goal navigation off; key settings are kept.")
    goal_setup.set_defaults(handler=handle_goal_setup)
    goal_check = goal_subcommands.add_parser("check", help="Report the setting and make one tiny Decisions call (no screen data).")
    goal_check.set_defaults(handler=handle_goal_check)

    media = subcommands.add_parser("media", help="Download Instagram/TikTok videos as local .mp4 files via host yt-dlp.")
    media_subcommands = media.add_subparsers(dest="media_command")
    media_fetch = media_subcommands.add_parser(
        "fetch",
        help="Download an Instagram/TikTok video to a local .mp4 file for video analysis (e.g. video-understand).",
    )
    media_fetch.add_argument("source_url", nargs="?", help="Share URL (or pass --from-pasteboard to read from iPhone clipboard).")
    media_fetch.add_argument("--from-pasteboard", action="store_true",
                             help="Read the share URL from the iPhone pasteboard via WDA first (outside an active session).")
    add_device_arg(media_fetch)
    add_wda_url_arg(media_fetch)
    media_fetch.add_argument("--runner-bundle-id", help="Override OPENCLAW_IPHONE_RUNNER_BUNDLE_ID for pasteboard foreground swap.")
    media_fetch.add_argument("--return-app", help="Bundle ID to reactivate after pasteboard read.")
    media_fetch.add_argument("--no-foreground-runner", action="store_true",
                             help="Read pasteboard without briefly foregrounding the WDA runner.")
    media_out = media_fetch.add_mutually_exclusive_group()
    media_out.add_argument("--output", help="Exact output .mp4 file path.")
    media_out.add_argument("--output-dir", help="Directory to write the downloaded .mp4 into (default: private 0700 temp dir).")
    media_fetch.add_argument("--cookies", help="Path to an owner-only (chmod 600) Netscape cookies file.")
    media_fetch.add_argument("--cookies-from-browser", help="Browser profile for yt-dlp --cookies-from-browser fallback (e.g. chrome).")
    media_fetch.add_argument("--allow-any-host", action="store_true", help="Allow yt-dlp downloads from hosts other than Instagram/TikTok.")
    media_fetch.add_argument("--download-timeout", type=int, default=120, help="Download timeout in seconds (default: 120).")
    media_fetch.add_argument("--json", action="store_true", help="Emit structured JSON instead of key-value lines.")
    media_fetch.set_defaults(handler=handle_media_fetch)

    session = subcommands.add_parser("session", help="Direct controls over persistent stdin/stdout JSON lines.")
    add_device_arg(session)
    session.add_argument("--operation-timeout", type=float, default=30, help="Per-operation deadline; idle deliberation is unlimited.")
    session.add_argument("--allow-images", action="store_true", help="Allow potentially private screenshot evidence; no implicit masking.")
    session.set_defaults(handler=handle_session)

    return parser


def add_device_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--device",
        help="Device UDID/identifier/name. Defaults to OPENCLAW_IPHONE_DEVICE or the only connected device.",
    )

def add_wda_url_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        help="Debug override for WebDriverAgent base URL. Defaults to config/env or the USB CoreDevice tunnel URL.",
    )

def client_from_args(args: argparse.Namespace) -> DeviceCtl:
    if not hasattr(args, "_devicectl"):
        args._devicectl = DeviceCtl(developer_dir=args.developer_dir,
                                   evidence_base=args.evidence_dir, timeout=args.timeout)
    return args._devicectl

def wda_client_from_args(args: argparse.Namespace) -> WDAClient:
    return WDAClient(url=resolve_wda_url_from_args(args), timeout=args.timeout, read_timeout=getattr(args, "read_timeout", None))

def resolve_wda_url_from_args(args: argparse.Namespace) -> str:
    explicit = getattr(args, "url", None)
    config = load_config()
    config_url = config.wda_url
    if (explicit or config_url) and not device_selector_from_args(args, config=config) and not hasattr(args, "_selected_device"):
        return explicit or config_url

    client = client_from_args(args)
    device = selected_device(args, client, config=config)
    url, _ = client.coredevice_wda_url(device.identifier, port=DEFAULT_WDA_PORT)
    if (explicit or config_url) and (explicit or config_url).rstrip("/") != url:
        raise ValueError("WDA URL override does not match the selected device's CoreDevice endpoint; remove the override.")
    return url

def device_selector_from_args(args: argparse.Namespace, *, config: IPhoneConfig | None = None) -> str | None:
    explicit = getattr(args, "device", None)
    if explicit:
        return explicit
    if config is None:
        config = load_config()
    return config.device

def handle_devices_list(args: argparse.Namespace) -> int:
    devices, artifact = client_from_args(args).list_devices(keep=True)
    for device in devices:
        print(f"{device.name}\t{device.identifier}\t{device.state}\t{device.model}")
    print(f"evidence: {artifact}")
    return 0

def handle_doctor(args: argparse.Namespace) -> int:
    config = load_config()
    for key, value in runtime_provenance(config).items():
        print(f"{key}: {value}")
    try:
        client = client_from_args(args)
        device = selected_device(args, client, config=config)
    except (OpenClawIPhoneError, ValueError) as exc:
        print("result: device-selection-failed")
        print(f"blocker: {exc}")
        return 1
    print(f"device: {device.name} ({device.identifier})")

    try:
        lock_data, lock_artifact = client.lock_state(device.identifier, keep=True)
    except (OpenClawIPhoneError, ValueError) as exc:
        print("passcode-required: unknown")
        print("result: lock-state-failed")
        print(f"blocker: {exc}")
        _print_usb_tunnel_hint(device)
        return 1
    passcode_required = passcode_required_from_lock_state(lock_data)
    print(f"passcode-required: {bool_value(passcode_required)}")
    print(f"lock-state evidence: {lock_artifact}")

    try:
        url = resolve_wda_url_from_args(args)
    except (OpenClawIPhoneError, ValueError) as exc:
        print("wda-url: unknown")
        print("result: wda-url-resolution-failed")
        print(f"blocker: {exc}")
        _print_usb_tunnel_hint(device)
        return 1
    print(f"wda-url: {url}")

    wda = WDAClient(url=url, timeout=args.timeout, read_timeout=getattr(args, "read_timeout", None))
    try:
        status = wda.status()
    except WDAUnavailable as exc:
        print("wda-reachable: false")
        print("wda-ready: false")
        print("result: attention-required")
        print(f"blocker: {exc}")
        return 1
    print("wda-reachable: true")
    print(f"wda-ready: {bool_value(status.ready)}")

    try:
        locked = wda.locked()
    except WDAUnavailable as exc:
        print("result: lock-check-failed")
        print(f"blocker: {exc}")
        return 1
    print(f"wda-locked: {bool_value(locked)}")

    healthy = passcode_required is False and status.ready is True and locked is False
    if getattr(args, "check_ui", False):
        try:
            wda.source()
        except WDAUnavailable:
            print("screen-read: unavailable")
            print("result: screen-read-failed")
            return 1
        print("screen-read: ok")
    print(f"result: {'ok' if healthy else 'attention-required'}")
    return 0 if healthy else 1

def _print_usb_tunnel_hint(device: Device) -> None:
    """When CoreDevice times out, check if usbmuxd still sees the phone so doctor can flag a wedged tunnel."""
    if not device.udid or not shutil.which("idevice_id"):
        return
    try:
        probe = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return
    udids = {line.strip().lower() for line in probe.stdout.splitlines() if line.strip()}
    if device.udid.lower() in udids:
        print("usb-present: true")
        print("hint: CoreDevice tunnel appears wedged while USB is connected; wake/unlock or replug the iPhone.")


def handle_goal_setup(args: argparse.Namespace) -> int:
    explicit = os.environ.get(CONFIG_ENV_VAR)  # else the file the CLI reads now, else the default location
    path = expand_user_path(explicit, os.environ) if explicit else config_path(os.environ) or default_config_path(os.environ)
    updates: dict[str, str | None] = {"OPENCLAW_IPHONE_GOAL_ENABLED": "0" if args.disable else "1"}
    provider = args.provider or goal_provider(load_config())
    if args.provider:
        updates["OPENCLAW_IPHONE_GOAL_PROVIDER"] = provider
    if args.model is not None:
        updates["OPENCLAW_IPHONE_GOAL_MODEL"] = args.model.strip() or None
    if args.base_url is not None:
        base_url_key = f"OPENCLAW_IPHONE_{provider.upper()}_BASE_URL"
        if base_url_key not in {"OPENCLAW_IPHONE_OPENAI_BASE_URL", "OPENCLAW_IPHONE_LIQUID_BASE_URL"}:
            raise ValueError(f"--base-url is supported for openai and liquid providers, not {provider}.")
        updates[base_url_key] = args.base_url.strip() or None
    if args.local_guard_url is not None:
        guard_url = args.local_guard_url.strip()
        if guard_url and not is_loopback_url(guard_url):
            raise ValueError("--local-guard-url must point to a local loopback host (127.0.0.1/localhost/::1).")
        updates["OPENCLAW_IPHONE_LOCAL_GUARD_URL"] = guard_url or None
    if args.local_guard_model is not None:
        updates["OPENCLAW_IPHONE_LOCAL_GUARD_MODEL"] = args.local_guard_model.strip() or None
    key_name = f"OPENCLAW_IPHONE_{provider.upper()}_API_KEY"
    if args.key_stdin:
        key = sys.stdin.read().strip()
        if not key or any(c.isspace() for c in key):
            raise ValueError("Expected exactly one API key on stdin.")
        updates |= {key_name: key, key_name + "_FILE": None}
    elif args.key_file:
        key_file = Path(args.key_file).expanduser().resolve()
        if read_key_file(key_file, provider=provider) is None:
            raise ValueError(f"No API key found in {key_file}.")
        updates |= {key_name + "_FILE": str(key_file), key_name: None}
    update_config_file(path, updates)
    print(f"config: {path}")
    print(f"goal-navigation: {goal_navigation(load_config())}")
    print("Restart any running session to apply it; `openclaw-iphone goal check` tests the key.")
    return 0


def handle_goal_check(args: argparse.Namespace) -> int:
    config = load_config()
    print(f"goal-navigation: {goal_navigation(config)}")
    decisions = decisions_from_config(config)
    if decisions is None:
        return 1
    started = time.monotonic()
    try:
        decisions.ask(CHECK)
    except Unavailable as exc:
        print(f"goal-check: failed ({exc.category})")
        return 1
    print(f"goal-check: ok ({time.monotonic() - started:.2f} s; no screen data sent)")
    return 0


def runtime_provenance(config: IPhoneConfig) -> dict[str, str]:
    """Report source/config/runner paths without claiming process provenance.

    A doctor invocation may use an editable checkout while launchd runs an
    installed copy (or vice versa).  This intentionally reports configured
    paths and plist metadata only; it never reads a running process command
    line or prints plist contents that could contain unrelated settings.
    """
    source = Path(__file__).resolve()
    configured_repo = config.get("OPENCLAW_IPHONE_REPO_DIR")
    repo_path = Path(configured_repo).expanduser().resolve() if configured_repo else None
    configured_wda = config.get("OPENCLAW_IPHONE_WDA_PATH")
    wda_path = Path(configured_wda).expanduser().resolve() if configured_wda else None
    plist_path = Path.home() / "Library/LaunchAgents/com.openclaw.iphone-wda-run.plist"

    def relation(path: Path | None, root: Path | None) -> str:
        if path is None:
            return "not-configured"
        if root is None:
            return "configured"
        try:
            path.relative_to(root)
        except ValueError:
            return "different"
        return "match"

    result = {
        "runtime-version": __version__,
        "runtime-python": sys.executable,
        "runtime-source": str(source),
        "configured-repo": str(repo_path) if repo_path else "absent",
        "source-repo": relation(source, repo_path) if repo_path else "not-configured",
        "configured-wda-path": str(wda_path) if wda_path else "absent",
        "wda-path": "absent" if wda_path is None else "present" if wda_path.exists() else "missing",
        "launchd-plist": "absent",
        "launchd-wrapper": "absent",
        "launchd-working-directory": "absent",
        "goal-navigation": goal_navigation(config),
    }
    if not plist_path.is_file():
        return result
    result["launchd-plist"] = str(plist_path.resolve())
    try:
        with plist_path.open("rb") as stream:
            plist = plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException):
        result["launchd-plist"] = f"{plist_path.resolve()} (unreadable)"
        return result
    arguments = plist.get("ProgramArguments") if isinstance(plist, dict) else None
    if isinstance(arguments, list):
        wrappers = [item for item in arguments if isinstance(item, str) and item.endswith(".sh")]
        if wrappers:
            wrapper = Path(wrappers[0]).expanduser().resolve()
            result["launchd-wrapper"] = f"{wrapper} ({relation(wrapper, repo_path)})"
    working_directory = plist.get("WorkingDirectory") if isinstance(plist, dict) else None
    if isinstance(working_directory, str) and working_directory:
        result["launchd-working-directory"] = f"{Path(working_directory).expanduser().resolve()} ({relation(Path(working_directory).expanduser().resolve(), repo_path)})"
    return result

def handle_apps_list(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    device = selected_device(args, client)
    apps, artifact = client.list_apps(device.identifier, include_all=not args.no_all, keep=True)
    for app in apps:
        print(f"{app.name}\t{app.bundle_identifier}\t{app.version}\t{app.bundle_version}")
    print(f"evidence: {artifact}")
    return 0

def handle_apps_find(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    device = selected_device(args, client)
    app = client.find_app(device.identifier, args.query)
    print(f"{app.name}\t{app.bundle_identifier}\t{app.version}\t{app.bundle_version}")
    return 0

def handle_apps_launch(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    with Connection(client, device=device_selector_from_args(args), seconds=args.timeout,
                    read_timeout=args.read_timeout) as connection:
        device = connection.device
        app = client.find_app(device.identifier, args.query)
        result = Session(connection).request({"op": "launch", "bundle_id": app.bundle_identifier})
    if result.get("dispatch") != "acknowledged":
        print(json.dumps(result))
        return 1
    print(f"launched: {app.name} ({app.bundle_identifier}) on {device.name}")
    return 0

def handle_apps_terminate(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    device = selected_device(args, client)
    app = client.find_app(device.identifier, args.query)
    wda_client_from_args(args).terminate_app(app.bundle_identifier)
    print(f"terminated: {app.name} ({app.bundle_identifier}) on {device.name}")
    return 0


def handle_wda_status(args: argparse.Namespace) -> int:
    status = wda_client_from_args(args).status()
    artifact = Path(args.output).expanduser().absolute() if args.output else artifact_path("wda-status", base=args.evidence_dir)
    write_private(artifact, json.dumps(status.payload, indent=2, sort_keys=True) + "\n")

    ready = "unknown" if status.ready is None else str(status.ready).lower()
    print(f"url: {status.url}")
    print("reachable: true")
    print(f"ready: {ready}")
    print(f"evidence: {artifact}")
    return 0 if status.ready is True else 1

def handle_wda_url(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    device = selected_device(args, client)
    url, artifact = client.coredevice_wda_url(device.identifier, port=args.port, keep=True)
    print(f"url: {url}")
    print(f"device: {device.name} ({device.identifier})")
    print(f"evidence: {artifact}")
    return 0

def handle_wda_locked(args: argparse.Namespace) -> int:
    locked = wda_client_from_args(args).locked()
    value = "unknown" if locked is None else str(locked).lower()
    print(f"locked: {value}")
    return 0 if locked is not None else 1

def handle_wda_unlock(args: argparse.Namespace) -> int:
    if args.verify:
        client = client_from_args(args)
        device = selected_device(args, client)
    wda = wda_client_from_args(args)
    wda.unlock()
    print("unlock-attempted: true")
    locked = wda.locked()
    if locked is not None:
        print(f"wda-locked: {str(locked).lower()}")
    if args.verify:
        data, artifact = client.lock_state(device.identifier, keep=True)
        passcode_required = passcode_required_from_lock_state(data)
        print(f"passcode-required: {bool_value(passcode_required)}")
        print(f"evidence: {artifact}")
        if passcode_required is True:
            print("result: human-unlock-required")
            return 1
        if passcode_required is not False:
            print("result: lock-state-unknown")
            return 1
    if locked is not False:
        print("result: still-locked" if locked is True else "result: lock-state-unknown")
        return 1
    print("result: ok")
    return 0

def handle_wda_lock(args: argparse.Namespace) -> int:
    wda_client_from_args(args).lock()
    print("locked: true")
    return 0


def handle_wda_pasteboard(args: argparse.Namespace) -> int:
    config = load_config()
    runner_id = args.runner_bundle_id or config.get("OPENCLAW_IPHONE_RUNNER_BUNDLE_ID")
    wda = wda_client_from_args(args)
    try:
        result = wda.read_pasteboard(
            content_type=args.content_type,
            runner_bundle_id=runner_id,
            return_app=args.return_app,
            foreground_runner=not args.no_foreground_runner,
        )
    finally:
        if hasattr(wda, "close"):
            wda.close()
    print(result["text"])
    return 0


def handle_media_fetch(args: argparse.Namespace) -> int:
    config = load_config()
    raw_url = args.source_url
    if args.from_pasteboard:
        if raw_url:
            raise ValueError("Pass either a URL or --from-pasteboard, not both.")
        runner_id = args.runner_bundle_id or config.get("OPENCLAW_IPHONE_RUNNER_BUNDLE_ID")
        wda = wda_client_from_args(args)
        try:
            pb = wda.read_pasteboard(
                runner_bundle_id=runner_id,
                return_app=args.return_app,
                foreground_runner=not args.no_foreground_runner,
            )
        finally:
            if hasattr(wda, "close"):
                wda.close()
        raw_url = pb["text"]
    if not raw_url:
        raise ValueError("Provide a social video URL or pass --from-pasteboard.")
    result = fetch_video(
        raw_url,
        config=config,
        output_path=Path(args.output) if args.output else None,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        cookies_file=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
        allow_any_host=args.allow_any_host,
        timeout=args.download_timeout,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"platform: {result['platform']}")
        print(f"canonical-url: {result['canonical_url']}")
        print(f"path: {result['path']}")
        print(f"size-bytes: {result['size_bytes']}")
        if result["duration_seconds"] is not None:
            print(f"duration-seconds: {result['duration_seconds']:.2f}")
        if result["uploader"]:
            print(f"uploader: {result['uploader']}")
        print(f"auth: {result['auth']}")
    return 0

def _maybe_kickstart_wda(device_id: str, threshold: int) -> None:
    if threshold <= 0:
        return
    failures = cache.update_watchdog_failures(device_id, failed=True)
    print(f"consecutive-failures: {failures}/{threshold}")
    if failures < threshold:
        return
    target = f"gui/{os.getuid()}/com.openclaw.iphone-wda-run"
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        print("recovery: kickstart-failed")
        return
    if result.returncode == 0:
        cache.update_watchdog_failures(device_id, failed=False)
        print("recovery: kickstarted com.openclaw.iphone-wda-run")
    else:
        print("recovery: kickstart-failed")


def handle_watchdog_once(args: argparse.Namespace) -> int:
    config = load_config()
    restart_after = getattr(args, "restart_after", None)
    if restart_after is None:
        raw = (config.get("OPENCLAW_IPHONE_WATCHDOG_RESTART_AFTER") or "0").strip()
        restart_after = int(raw) if raw else 0

    try:
        client = client_from_args(args)
        device = selected_device(args, client)
    except (OpenClawIPhoneError, ValueError) as exc:
        print("result: device-selection-failed")
        print(f"blocker: {exc}")
        return 1
    print(f"device: {device.name} ({device.identifier})")

    try:
        wda = wda_client_from_args(args)
    except (OpenClawIPhoneError, ValueError) as exc:
        print("wda-url: unknown")
        print("result: wda-url-resolution-failed")
        print(f"blocker: {exc}")
        _maybe_kickstart_wda(device.identifier, restart_after)
        return 1

    print(f"wda-url: {wda.url}")
    try:
        locked = wda.locked()
    except WDAUnavailable as exc:
        print("result: lock-check-failed")
        print(f"blocker: {exc}")
        _maybe_kickstart_wda(device.identifier, restart_after)
        return 1
    print(f"wda-locked: {bool_value(locked)}")
    if locked is False:
        if restart_after > 0:
            cache.update_watchdog_failures(device.identifier, failed=False)
        print("result: ok")
        return 0
    if locked is None:
        print("result: lock-state-unknown")
        return 1

    try:
        wda.unlock()
    except WDAUnavailable as exc:
        print("result: unlock-failed")
        print(f"blocker: {exc}")
        _maybe_kickstart_wda(device.identifier, restart_after)
        return 1
    print("unlock-attempted: true")
    try:
        locked_after = wda.locked()
    except WDAUnavailable as exc:
        print("result: post-unlock-lock-check-failed")
        print(f"blocker: {exc}")
        _maybe_kickstart_wda(device.identifier, restart_after)
        return 1
    # WDA answered both unlock and lock checks cleanly, so WDA itself is healthy.
    if restart_after > 0:
        cache.update_watchdog_failures(device.identifier, failed=False)
    print(f"wda-locked-after-unlock: {bool_value(locked_after)}")

    passcode_required = None
    if not args.no_verify:
        try:
            data, artifact = client.lock_state(device.identifier, keep=True)
        except (OpenClawIPhoneError, ValueError) as exc:
            print("passcode-required: unknown")
            print("result: lock-state-failed")
            print(f"blocker: {exc}")
            return 1
        passcode_required = passcode_required_from_lock_state(data)
        print(f"passcode-required: {bool_value(passcode_required)}")
        print(f"lock-state evidence: {artifact}")
        if passcode_required is True:
            print("result: human-unlock-required")
            return 1
        if passcode_required is not False:
            print("result: lock-state-unknown")
            return 1

    if locked_after is False:
        print("result: unlocked")
        return 0
    if locked_after is None:
        print("result: lock-state-unknown")
        return 1
    if locked_after is True and passcode_required is False:
        print("result: lock-state-conflict")
        return 1

    print("result: still-locked")
    return 1

def handle_wda_build(args: argparse.Namespace) -> int:
    config = load_config()
    wda_path = resolve_wda_path(args.wda_path or config.get("OPENCLAW_IPHONE_WDA_PATH"))
    destination_timeout = args.destination_timeout
    if destination_timeout is None:
        destination_timeout = int(config.get("OPENCLAW_IPHONE_DESTINATION_TIMEOUT", "30") or "30")
    development_team = args.development_team or config.get("OPENCLAW_IPHONE_DEVELOPMENT_TEAM")
    runner_bundle_id = args.runner_bundle_id or config.get("OPENCLAW_IPHONE_RUNNER_BUNDLE_ID")
    derived_raw = args.derived_data_path or config.get("OPENCLAW_IPHONE_WDA_DERIVED_DATA")
    derived_data_path = Path(derived_raw).expanduser() if derived_raw else default_derived_data_path()
    client = client_from_args(args)
    device = selected_device(args, client, config=config)
    print(f"device: {device.name} ({device.identifier})")
    print(f"wda path: {wda_path}")
    print(f"derived-data: {derived_data_path}")
    xctestrun = build_wda(
        WDARunConfig(
            device_id=device.xcode_identifier,
            wda_path=wda_path,
            scheme=args.scheme,
            configuration=args.configuration,
            developer_dir=args.developer_dir,
            destination_timeout=destination_timeout,
            development_team=development_team,
            runner_bundle_id=runner_bundle_id,
            allow_provisioning_updates=args.allow_provisioning_updates,
            allow_provisioning_device_registration=args.allow_provisioning_device_registration,
            derived_data_path=derived_data_path,
            build_only=True,
        )
    )
    print(f"xctestrun: {xctestrun or 'not-found'}")
    if args.save_config and xctestrun is not None:
        explicit = os.environ.get(CONFIG_ENV_VAR)
        cfg_path = expand_user_path(explicit, os.environ) if explicit else config_path(os.environ) or default_config_path(os.environ)
        update_config_file(cfg_path, {
            "OPENCLAW_IPHONE_WDA_XCTESTRUN": str(xctestrun),
            "OPENCLAW_IPHONE_WDA_DERIVED_DATA": str(derived_data_path),
        })
        print(f"config: {cfg_path}")
    return 0 if xctestrun is not None else 1


def handle_wda_run(args: argparse.Namespace) -> int:
    config = load_config()
    destination_timeout = args.destination_timeout
    if destination_timeout is None:
        destination_timeout = int(config.get("OPENCLAW_IPHONE_DESTINATION_TIMEOUT", "30") or "30")
    development_team = args.development_team or config.get("OPENCLAW_IPHONE_DEVELOPMENT_TEAM")
    runner_bundle_id = args.runner_bundle_id or config.get("OPENCLAW_IPHONE_RUNNER_BUNDLE_ID")
    xctestrun: Path | None = None
    explicit_arg = getattr(args, "xctestrun", None)
    explicit_xctestrun = explicit_arg or config.get("OPENCLAW_IPHONE_WDA_XCTESTRUN")
    if explicit_xctestrun:
        candidate = Path(explicit_xctestrun).expanduser()
        if candidate.is_file():
            xctestrun = candidate
        elif explicit_arg:
            raise ValueError(f"xctestrun file does not exist: {candidate}")
    if xctestrun is None and getattr(args, "without_building", False):
        derived_raw = config.get("OPENCLAW_IPHONE_WDA_DERIVED_DATA")
        derived = Path(derived_raw).expanduser() if derived_raw else default_derived_data_path()
        xctestrun = find_xctestrun(derived, scheme=args.scheme)
    wda_path = xctestrun.parent if xctestrun is not None else resolve_wda_path(args.wda_path or config.get("OPENCLAW_IPHONE_WDA_PATH"))
    client = client_from_args(args)
    device = selected_device(args, client, config=config)
    print(f"device: {device.name} ({device.identifier})")
    if xctestrun is not None:
        print(f"xctestrun: {xctestrun}")
        print("starting: xcodebuild test-without-building")
    else:
        print(f"wda path: {wda_path}")
        print("starting: xcodebuild test")
    print("note: keep this process alive; if it exits, WDA control breaks.")
    return run_wda(
        WDARunConfig(
            device_id=device.xcode_identifier,
            wda_path=wda_path,
            scheme=args.scheme,
            configuration=args.configuration,
            developer_dir=args.developer_dir,
            destination_timeout=destination_timeout,
            development_team=development_team,
            runner_bundle_id=runner_bundle_id,
            allow_provisioning_updates=args.allow_provisioning_updates,
            xctestrun=xctestrun,
        )
    )


def handle_d1_install(args: argparse.Namespace) -> int:
    config = load_config()
    venv = mlx_venv_path(config, args.venv)
    model = mlx_model_from_config(config, args.model)
    port = mlx_port_from_config(config, args.port)
    info = install_mlx_runtime(venv, model=model, prefetch=args.prefetch)
    explicit = os.environ.get(CONFIG_ENV_VAR)
    cfg_path = expand_user_path(explicit, os.environ) if explicit else config_path(os.environ) or default_config_path(os.environ)
    updates: dict[str, str | None] = {
        "OPENCLAW_IPHONE_MLX_VENV": info["venv"],
        "OPENCLAW_IPHONE_MLX_MODEL": model,
        "OPENCLAW_IPHONE_MLX_PORT": str(port),
    }
    base_url = f"http://{DEFAULT_MLX_HOST}:{port}/v1"
    if args.configure_navigator:
        updates |= {
            "OPENCLAW_IPHONE_GOAL_ENABLED": "1",
            "OPENCLAW_IPHONE_GOAL_PROVIDER": "liquid",
            "OPENCLAW_IPHONE_GOAL_MODEL": model,
            "OPENCLAW_IPHONE_LIQUID_BASE_URL": base_url,
        }
    elif args.configure_guard:
        updates |= {
            "OPENCLAW_IPHONE_LOCAL_GUARD_URL": base_url,
            "OPENCLAW_IPHONE_LOCAL_GUARD_MODEL": model,
        }
    update_config_file(cfg_path, updates)
    print(f"mlx-venv: {info['venv']}")
    print(f"mlx-python: {info['python']}")
    print(f"mlx-installer: {info['installer']}")
    print(f"mlx-model: {model}")
    print(f"mlx-url: {base_url}")
    print(f"config: {cfg_path}")
    print("Next: start with `openclaw-iphone d1 serve` or install snippets/launchd/install-d1-serve-launchagent.sh")
    return 0


def handle_d1_serve(args: argparse.Namespace) -> int:
    config = load_config()
    venv = mlx_venv_path(config, args.venv)
    model = mlx_model_from_config(config, args.model)
    port = mlx_port_from_config(config, args.port)
    # If invoked from system Python without mlx_vlm, seamlessly re-exec using the isolated MLX venv.
    try:
        import mlx_vlm  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        venv_py = mlx_python(venv)
        # Compare un-resolved paths / sys.prefix because venv/bin/python is typically a symlink
        # to the base interpreter (Python detects the venv via argv[0] + pyvenv.cfg).
        in_target_venv = (
            os.path.abspath(sys.executable) == os.path.abspath(venv_py)
            or Path(sys.prefix).resolve() == venv.resolve()
        )
        if os.environ.get("OPENCLAW_IPHONE_D1_REEXEC") != "1" and venv_py.is_file() and not in_target_venv:
            env = os.environ.copy()
            env["OPENCLAW_IPHONE_D1_REEXEC"] = "1"
            src_root = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = f"{src_root}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else src_root
            argv = [
                str(venv_py), "-m", "openclaw_iphone", "d1", "serve",
                f"--host={args.host}", f"--port={port}", f"--model={model}", f"--venv={venv}",
            ]
            if getattr(args, "warmup", False):
                argv.append("--warmup")
            if args.allow_non_loopback:
                argv.append("--allow-non-loopback")
            os.execve(str(venv_py), argv, env)
        raise OpenClawIPhoneError(
            f"mlx-vlm is not installed in {venv}. Run `openclaw-iphone d1 install` first."
        )
    server = create_d1_server(args.host, port, model_id=model, allow_non_loopback=args.allow_non_loopback)
    if getattr(args, "warmup", False):
        print(f"d1-mlx warming up {model}...", flush=True)
        server.engine.warmup()
        print("d1-mlx warmup complete", flush=True)
    url_host = f"[{args.host}]" if ":" in args.host and not args.host.startswith("[") else args.host
    print(f"d1-mlx serving {model} on http://{url_host}:{port}/v1/systemone", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def handle_d1_status(args: argparse.Namespace) -> int:
    config = load_config()
    venv = mlx_venv_path(config, args.venv)
    model = mlx_model_from_config(config)
    port = mlx_port_from_config(config, args.port)
    base_url = f"http://{DEFAULT_MLX_HOST}:{port}/v1"
    installed = is_mlx_installed(venv)
    health = probe_d1_health(base_url)
    print(f"mlx-venv: {venv}")
    print(f"mlx-installed: {str(installed).lower()}")
    print(f"mlx-model: {model}")
    print(f"mlx-url: {base_url}")
    if health:
        print(f"server: reachable (model={health.get('model')}, loaded={str(bool(health.get('loaded'))).lower()})")
        return 0
    print("server: unreachable")
    return 0 if installed else 1

def passcode_required_from_lock_state(data: dict[str, object]) -> bool | None:
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    value = result.get("passcodeRequired")
    return value if isinstance(value, bool) else None

def bool_value(value: bool | None) -> str:
    return "unknown" if value is None else str(value).lower()


def handle_session(args: argparse.Namespace) -> int:
    emit = json_line_emitter(sys.stdout)
    connection = None
    try:
        config = load_config()
        if config.wda_url:
            raise ValueError("Session control requires the selected phone's CoreDevice endpoint; remove the URL override.")
        connection = Connection(client_from_args(args), device=device_selector_from_args(args, config=config),
                                seconds=args.operation_timeout, read_timeout=args.read_timeout)
        with connection:
            session = Session(connection, allow_images=args.allow_images, evidence_base=args.evidence_dir,
                              decisions=decisions_from_config(config),
                              runner_bundle_id=config.get("OPENCLAW_IPHONE_RUNNER_BUNDLE_ID"))
            capabilities = [
                "set_input", "set_submit", "gregorian_wheel_date", "native_picker",
                "private_replace_verification", "foreground_readiness", "request_timing",
                "candidate_paging", "pasteboard", "sequence",
            ]
            if session.decisions:
                capabilities.extend(["goal", "goal_observe"])
                if getattr(session.decisions, "supports_box", False):
                    capabilities.append("goal_vision_box")
            emit({"status": "ready", "protocol": 2, "device_udid": connection.device.udid,
                  "capabilities": capabilities})
            code = serve(session, read_requests(sys.stdin.fileno()), emit)
        emit({"status": "session_end", "cleanup": "warning" if connection.cleanup_failed else "completed",
              "timing": connection.metrics.summary(), "reconnects": connection.reconnects})
        return code
    except SessionOutputUnavailable:
        return 1
    except (ValueError, OSError, OpenClawIPhoneError, KeyboardInterrupt) as exc:
        try:
            emit({"status": "error", "reason": "session_unavailable_or_interrupted",
                  "error": diagnostic(exc),
                  "timing": connection.metrics.summary() if connection else {},
                  "cleanup": "warning" if connection and connection.cleanup_failed else "completed"})
        except SessionOutputUnavailable:
            pass
        return 1
