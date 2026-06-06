from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .devicectl import DeviceCtl
from .evidence import artifact_path
from .errors import OpenClawIPhoneError
from .recipes.instagram import smoke as instagram_smoke
from .ui import UIController
from .wda import WDAClient, WDARunConfig, find_iproxy, resolve_wda_path, run_iproxy, run_wda


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "handler"):
        parser.print_help()
        return 2

    try:
        return args.handler(args)
    except OpenClawIPhoneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openclaw-iphone",
        description="Reusable primitives for controlling a USB-connected iPhone.",
    )
    parser.add_argument("--developer-dir", help="Override DEVELOPER_DIR for Xcode/devicectl.")
    parser.add_argument("--evidence-dir", help="Directory for JSON evidence artifacts.")
    parser.add_argument("--timeout", type=int, default=30, help="External command timeout in seconds.")

    subcommands = parser.add_subparsers(dest="command")

    devices = subcommands.add_parser("devices", help="Device discovery commands.")
    device_subcommands = devices.add_subparsers(dest="devices_command")
    devices_list = device_subcommands.add_parser("list", help="List connected devices.")
    devices_list.set_defaults(handler=handle_devices_list)

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
    apps_launch.add_argument(
        "--skip-lock-check",
        action="store_true",
        help="Launch without first checking lock state.",
    )
    apps_launch.add_argument("query")
    apps_launch.set_defaults(handler=handle_apps_launch)

    apps_terminate = apps_subcommands.add_parser("terminate", help="Terminate an installed app by name or bundle id.")
    add_device_arg(apps_terminate)
    apps_terminate.add_argument("query")
    apps_terminate.set_defaults(handler=handle_apps_terminate)

    instagram = subcommands.add_parser("instagram", help="Optional Instagram recipe commands.")
    instagram_subcommands = instagram.add_subparsers(dest="instagram_command")
    instagram_smoke_parser = instagram_subcommands.add_parser("smoke", help="Verify and launch Instagram.")
    add_device_arg(instagram_smoke_parser)
    instagram_smoke_parser.set_defaults(handler=handle_instagram_smoke)

    wda = subcommands.add_parser("wda", help="WebDriverAgent commands.")
    wda_subcommands = wda.add_subparsers(dest="wda_command")
    wda_status = wda_subcommands.add_parser("status", help="Check whether WebDriverAgent is reachable.")
    add_wda_url_arg(wda_status)
    wda_status.add_argument("--output", help="Optional path for raw status JSON.")
    wda_status.set_defaults(handler=handle_wda_status)

    wda_run = wda_subcommands.add_parser(
        "run",
        help="Build and run WebDriverAgentRunner as a long-lived xcodebuild test process.",
    )
    add_device_arg(wda_run)
    wda_run.add_argument(
        "--wda-path",
        help="Path to WebDriverAgent checkout/project. Defaults to OPENCLAW_IPHONE_WDA_PATH.",
    )
    wda_run.add_argument("--scheme", default="WebDriverAgentRunner")
    wda_run.add_argument("--configuration", default="Debug")
    wda_run.add_argument("--destination-timeout", type=int, default=30)
    wda_run.add_argument("--development-team", help="Apple Developer Team ID to pass to xcodebuild.")
    wda_run.add_argument(
        "--runner-bundle-id",
        help="Bundle identifier override for WebDriverAgentRunner, usually unique to your team.",
    )
    wda_run.add_argument(
        "--allow-provisioning-updates",
        action="store_true",
        help="Pass -allowProvisioningUpdates to xcodebuild for automatic signing.",
    )
    wda_run.set_defaults(handler=handle_wda_run)

    wda_tunnel = wda_subcommands.add_parser(
        "tunnel",
        help="Forward localhost to the device WDA port as a long-lived iproxy process.",
    )
    add_device_arg(wda_tunnel)
    wda_tunnel.add_argument("--local-port", type=int, default=8100)
    wda_tunnel.add_argument("--device-port", type=int, default=8100)
    wda_tunnel.set_defaults(handler=handle_wda_tunnel)

    ui = subcommands.add_parser("ui", help="UI capture commands backed by WebDriverAgent.")
    ui_subcommands = ui.add_subparsers(dest="ui_command")
    ui_screenshot = ui_subcommands.add_parser("screenshot", help="Capture a screenshot through WebDriverAgent.")
    add_wda_url_arg(ui_screenshot)
    ui_screenshot.add_argument("--output", help="Optional path for the PNG screenshot.")
    ui_screenshot.set_defaults(handler=handle_ui_screenshot)

    ui_source = ui_subcommands.add_parser("source", help="Capture the accessibility source through WebDriverAgent.")
    add_wda_url_arg(ui_source)
    ui_source.add_argument("--output", help="Optional path for the source XML/text.")
    ui_source.set_defaults(handler=handle_ui_source)

    return parser


def add_device_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device", help="Device UDID/identifier/name. Defaults to the only connected device.")


def add_wda_url_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", help="WebDriverAgent base URL. Defaults to OPENCLAW_IPHONE_WDA_URL or localhost.")


def client_from_args(args: argparse.Namespace) -> DeviceCtl:
    return DeviceCtl(
        developer_dir=args.developer_dir,
        evidence_base=args.evidence_dir,
        timeout=args.timeout,
    )


def wda_client_from_args(args: argparse.Namespace) -> WDAClient:
    return WDAClient(url=args.url, timeout=args.timeout)


def handle_devices_list(args: argparse.Namespace) -> int:
    devices, artifact = client_from_args(args).list_devices()
    for device in devices:
        print(f"{device.name}\t{device.identifier}\t{device.state}\t{device.model}")
    print(f"evidence: {artifact}")
    return 0


def handle_apps_list(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    device = client.select_device(args.device)
    apps, artifact = client.list_apps(device.identifier, include_all=not args.no_all)
    for app in apps:
        print(f"{app.name}\t{app.bundle_identifier}\t{app.version}\t{app.bundle_version}")
    print(f"evidence: {artifact}")
    return 0


def handle_apps_find(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    device = client.select_device(args.device)
    app = client.find_app(device.identifier, args.query)
    print(f"{app.name}\t{app.bundle_identifier}\t{app.version}\t{app.bundle_version}")
    return 0


def handle_apps_launch(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    device = client.select_device(args.device)
    app = client.find_app(device.identifier, args.query)
    if not args.skip_lock_check:
        client.require_unlocked(device.identifier)
    client.launch_app(device.identifier, app.bundle_identifier)
    print(f"launched: {app.name} ({app.bundle_identifier}) on {device.name}")
    return 0


def handle_apps_terminate(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    device = client.select_device(args.device)
    app = client.find_app(device.identifier, args.query)
    client.terminate_app(device.identifier, app.bundle_identifier)
    print(f"terminated: {app.name} ({app.bundle_identifier}) on {device.name}")
    return 0


def handle_instagram_smoke(args: argparse.Namespace) -> int:
    result = instagram_smoke(client_from_args(args), device_selector=args.device)
    print(f"device: {result.device.name} ({result.device.identifier})")
    print(f"instagram: {result.app_name} ({result.bundle_identifier})")
    print(f"lock-state evidence: {result.lock_state_artifact}")
    print(f"apps evidence: {result.apps_artifact}")
    print("result: launched")
    return 0


def handle_wda_status(args: argparse.Namespace) -> int:
    status = wda_client_from_args(args).status()
    artifact = Path(args.output) if args.output else artifact_path("wda-status", base=args.evidence_dir)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(status.payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    ready = "unknown" if status.ready is None else str(status.ready).lower()
    print(f"url: {status.url}")
    print("reachable: true")
    print(f"ready: {ready}")
    print(f"evidence: {artifact}")
    return 0


def handle_wda_run(args: argparse.Namespace) -> int:
    wda_path = resolve_wda_path(args.wda_path)
    client = client_from_args(args)
    device = client.select_device(args.device)
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
            destination_timeout=args.destination_timeout,
            development_team=args.development_team,
            runner_bundle_id=args.runner_bundle_id,
            allow_provisioning_updates=args.allow_provisioning_updates,
        )
    )


def handle_wda_tunnel(args: argparse.Namespace) -> int:
    find_iproxy()
    device = client_from_args(args).select_device(args.device)
    tunnel_device_id = device.xcode_identifier
    print(f"device: {device.name} ({device.identifier})")
    print(f"forwarding: 127.0.0.1:{args.local_port} -> device:{args.device_port}")
    print("note: keep this process alive; if it exits, localhost WDA access breaks.")
    return run_iproxy(
        tunnel_device_id,
        local_port=args.local_port,
        device_port=args.device_port,
    )


def handle_ui_screenshot(args: argparse.Namespace) -> int:
    path = UIController(
        wda_client_from_args(args),
        evidence_base=args.evidence_dir,
    ).capture_screenshot(args.output)
    print(f"screenshot: {path}")
    return 0


def handle_ui_source(args: argparse.Namespace) -> int:
    path = UIController(
        wda_client_from_args(args),
        evidence_base=args.evidence_dir,
    ).capture_source(args.output)
    print(f"source: {path}")
    return 0
