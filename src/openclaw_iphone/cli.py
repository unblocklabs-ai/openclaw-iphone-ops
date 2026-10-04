from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import math
import plistlib
from pathlib import Path
import sys
import time

from . import __version__
from .config import IPhoneConfig, load_config
from .connection import Connection
from .control_lock import control_lock
from .devicectl import Device, DeviceCtl
from .evidence import artifact_path, write_private
from .errors import DeviceLocked, DeviceSelectionError, OpenClawIPhoneError, SessionOutputUnavailable, WDAUnavailable, diagnostic
from .protocol import json_line_emitter, read_requests, serve
from .session import Session
from .wda import DEFAULT_SCREEN_READ_TIMEOUT, DEFAULT_WDA_PORT, WDAClient, WDARunConfig, resolve_wda_path, run_wda

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
            or args.command == "wda" and args.wda_command in {"unlock", "lock"}
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
    parser.add_argument("--read-timeout", type=float, default=DEFAULT_SCREEN_READ_TIMEOUT, help="Read-only WDA request timeout (default 12s), capped by each operation deadline.")

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
    wda_run.set_defaults(handler=handle_wda_run)

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
    watchdog_once.set_defaults(handler=handle_watchdog_once)

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
    return WDAClient(url=resolve_wda_url_from_args(args), timeout=args.timeout, read_timeout=getattr(args, "read_timeout", DEFAULT_SCREEN_READ_TIMEOUT))

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
    devices, artifact = client_from_args(args).list_devices()
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
        lock_data, lock_artifact = client.lock_state(device.identifier)
    except (OpenClawIPhoneError, ValueError) as exc:
        print("passcode-required: unknown")
        print("result: lock-state-failed")
        print(f"blocker: {exc}")
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
        return 1
    print(f"wda-url: {url}")

    wda = WDAClient(url=url, timeout=args.timeout, read_timeout=getattr(args, "read_timeout", DEFAULT_SCREEN_READ_TIMEOUT))
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
    apps, artifact = client.list_apps(device.identifier, include_all=not args.no_all)
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
    device = selected_device(args, client)
    app = client.find_app(device.identifier, args.query)
    if not device.udid:
        raise DeviceSelectionError("Physical device identity is unavailable; launch not sent.")
    with Connection(client, device=device.udid, seconds=args.timeout,
                    read_timeout=args.read_timeout) as connection:
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
    url, artifact = client.coredevice_wda_url(device.identifier, port=args.port)
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
        data, artifact = client.lock_state(device.identifier)
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

def handle_watchdog_once(args: argparse.Namespace) -> int:
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
        return 1

    try:
        status = wda.status()
    except WDAUnavailable as exc:
        print(f"result: wda-unreachable")
        print(f"blocker: {exc}")
        return 1

    print(f"wda-url: {status.url}")
    print(f"wda-ready: {bool_value(status.ready)}")
    if status.ready is not True:
        print(f"result: {'wda-ready-unknown' if status.ready is None else 'wda-not-ready'}")
        return 1

    try:
        locked = wda.locked()
    except WDAUnavailable as exc:
        print("result: lock-check-failed")
        print(f"blocker: {exc}")
        return 1
    print(f"wda-locked: {bool_value(locked)}")
    if locked is False:
        if not args.no_verify:
            try:
                client.require_unlocked(device.identifier)
            except (OpenClawIPhoneError, ValueError) as exc:
                print("result: lock-state-conflict-or-unknown")
                print(f"blocker: {exc}")
                return 1
        print("result: ok")
        return 0
    if locked is None:
        print("result: lock-state-unknown")
        return 1

    if not args.no_verify:
        try:
            client.require_unlocked(device.identifier)
        except (OpenClawIPhoneError, ValueError) as exc:
            print("result: human-unlock-required-or-unknown")
            print(f"blocker: {exc}")
            return 1

    try:
        wda.unlock()
    except WDAUnavailable as exc:
        print("result: unlock-failed")
        print(f"blocker: {exc}")
        return 1
    print("unlock-attempted: true")
    try:
        locked_after = wda.locked()
    except WDAUnavailable as exc:
        print("result: post-unlock-lock-check-failed")
        print(f"blocker: {exc}")
        return 1
    print(f"wda-locked-after-unlock: {bool_value(locked_after)}")

    passcode_required = None
    if not args.no_verify:
        try:
            data, artifact = client.lock_state(device.identifier)
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

def handle_wda_run(args: argparse.Namespace) -> int:
    config = load_config()
    wda_path = resolve_wda_path(args.wda_path or config.get("OPENCLAW_IPHONE_WDA_PATH"))
    destination_timeout = args.destination_timeout
    if destination_timeout is None:
        destination_timeout = int(config.get("OPENCLAW_IPHONE_DESTINATION_TIMEOUT", "30") or "30")
    development_team = args.development_team or config.get("OPENCLAW_IPHONE_DEVELOPMENT_TEAM")
    runner_bundle_id = args.runner_bundle_id or config.get("OPENCLAW_IPHONE_RUNNER_BUNDLE_ID")
    client = client_from_args(args)
    device = selected_device(args, client, config=config)
    client.require_unlocked(device.identifier)
    print(f"device: {device.name} ({device.identifier})")
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
        )
    )

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
            session = Session(connection, allow_images=args.allow_images, evidence_base=args.evidence_dir)
            emit({"status": "ready", "protocol": 2, "device_udid": connection.device.udid,
                  "capabilities": ["native_picker", "private_replace_verification", "foreground_readiness", "request_timing", "candidate_paging"]})
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
