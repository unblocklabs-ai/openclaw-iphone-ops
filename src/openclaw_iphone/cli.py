from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import IPhoneConfig, load_config
from .devicectl import DeviceCtl
from .evidence import artifact_path
from .errors import DeviceLocked, OpenClawIPhoneError, WDAUnavailable
from .instagram_context import capture_instagram_context
from .instagram_ops import DEFAULT_ANALYSIS_PROMPT, analyze_video, benchmark_discovery, benchmark_ranking_quality, discover_creators, triage_shortlist, verify_handles
from .recipes.instagram import smoke as instagram_smoke
from .ui import UIController
from .wda import DEFAULT_WDA_PORT, WDAClient, WDARunConfig, resolve_wda_path, run_wda


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

    doctor = subcommands.add_parser("doctor", help="Run a read-only iPhone control health check.")
    add_device_arg(doctor)
    add_wda_url_arg(doctor)
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

    instagram_context = instagram_subcommands.add_parser(
        "capture-context",
        help="Capture current Instagram screen/source and parse visible creator/content metadata.",
    )
    add_device_arg(instagram_context)
    add_wda_url_arg(instagram_context)
    instagram_context.add_argument("--output-dir", help="Directory for screenshot/source/manifest artifacts.")
    instagram_context.add_argument("--prefix", default="instagram-context", help="Artifact filename prefix.")
    instagram_context.set_defaults(handler=handle_instagram_capture_context)

    instagram_verify = instagram_subcommands.add_parser(
        "verify-handles",
        help="Best-effort bounded flow to open known Instagram handles and capture profile evidence.",
    )
    add_device_arg(instagram_verify)
    add_wda_url_arg(instagram_verify)
    instagram_verify.add_argument("handles", nargs="+", help="Instagram handles, with or without @.")
    instagram_verify.add_argument("--output-dir", help="Directory for per-handle evidence artifacts.")
    instagram_verify.add_argument("--prefix", default="instagram-verify")
    instagram_verify.add_argument("--max-steps-per-handle", type=int, default=12)
    instagram_verify.add_argument("--deadline-seconds", type=float, help="Wall-clock deadline per handle.")
    instagram_verify.add_argument("--no-launch", action="store_true", help="Do not launch Instagram before verifying.")
    instagram_verify.set_defaults(handler=handle_instagram_verify_handles)

    instagram_video = instagram_subcommands.add_parser(
        "analyze-video",
        help="Capture current Instagram context and hand a supplied video URL/file to video-understand.",
    )
    add_device_arg(instagram_video)
    add_wda_url_arg(instagram_video)
    instagram_video.add_argument("--video", required=True, help="Direct video URL or local file for video-understand.")
    instagram_video.add_argument("--prompt", default=DEFAULT_ANALYSIS_PROMPT)
    instagram_video.add_argument("--output-dir", help="Directory for context and analysis artifacts.")
    instagram_video.add_argument("--prefix", default="instagram-video-analysis")
    instagram_video.add_argument("--dry-run", action="store_true", help="Write handoff artifacts without invoking video-understand.")
    instagram_video.add_argument("--timeout", type=int, default=300)
    instagram_video.set_defaults(handler=handle_instagram_analyze_video)

    instagram_discover = instagram_subcommands.add_parser(
        "discover-creators",
        help="Discover pregnancy/motherhood creator candidates from bounded Instagram source screens.",
    )
    add_device_arg(instagram_discover)
    add_wda_url_arg(instagram_discover)
    instagram_discover.add_argument("--query", required=True, help="Creator discovery query, for example 'pregnancy journey'.")
    instagram_discover.add_argument("--max-candidates", type=int, default=10)
    instagram_discover.add_argument("--deadline-seconds", type=float, default=600)
    instagram_discover.add_argument("--output-dir", help="Directory for discovery artifacts.")
    instagram_discover.add_argument("--prefix", default="instagram-discovery")
    instagram_discover.add_argument("--max-source-scrolls", type=int, default=6)
    instagram_discover.add_argument("--max-steps", type=int, default=120)
    instagram_discover.add_argument("--max-steps-per-candidate", type=int, default=10)
    instagram_discover.add_argument("--per-candidate-deadline-seconds", type=float, default=45)
    instagram_discover.add_argument("--verification-mode", choices=("profile", "source-only"), default="profile")
    instagram_discover.add_argument("--source-open-wait-seconds", type=float, default=1.5)
    instagram_discover.add_argument("--no-launch", action="store_true", help="Do not launch Instagram before discovery.")
    instagram_discover.set_defaults(handler=handle_instagram_discover_creators)

    instagram_benchmark = instagram_subcommands.add_parser(
        "benchmark-discovery",
        help="Run the creator discovery benchmark scenarios and write JSON/markdown reports.",
    )
    add_device_arg(instagram_benchmark)
    add_wda_url_arg(instagram_benchmark)
    instagram_benchmark.add_argument("--output-dir", help="Directory for benchmark artifacts.")
    instagram_benchmark.add_argument("--prefix", default="instagram-discovery-benchmark")
    instagram_benchmark.add_argument("--max-candidates-per-scenario", type=int, default=10)
    instagram_benchmark.add_argument("--scenario-deadline-seconds", type=float, default=360)
    instagram_benchmark.add_argument("--max-source-scrolls", type=int, default=6)
    instagram_benchmark.add_argument("--verification-mode", choices=("profile", "source-only"), default="profile")
    instagram_benchmark.add_argument("--source-open-wait-seconds", type=float, default=1.5)
    instagram_benchmark.add_argument("--no-launch", action="store_true", help="Do not launch Instagram before benchmarking.")
    instagram_benchmark.set_defaults(handler=handle_instagram_benchmark_discovery)

    instagram_triage = instagram_subcommands.add_parser(
        "triage-shortlist",
        help="Run fast source-only triage, verify top candidates, and write a shortlist report.",
    )
    add_device_arg(instagram_triage)
    add_wda_url_arg(instagram_triage)
    instagram_triage.add_argument("--output-dir", help="Directory for triage artifacts.")
    instagram_triage.add_argument("--prefix", default="instagram-triage-shortlist")
    instagram_triage.add_argument("--max-candidates-per-scenario", type=int, default=10)
    instagram_triage.add_argument("--source-deadline-seconds", type=float, default=45)
    instagram_triage.add_argument("--max-source-scrolls", type=int, default=1)
    instagram_triage.add_argument("--verify-top", type=int, default=10)
    instagram_triage.add_argument("--verification-deadline-seconds", type=float, default=180)
    instagram_triage.add_argument("--per-candidate-deadline-seconds", type=float, default=30)
    instagram_triage.add_argument("--shortlist-size", type=int, default=5)
    instagram_triage.add_argument("--source-open-wait-seconds", type=float, default=1.5)
    instagram_triage.add_argument("--no-launch", action="store_true", help="Do not launch Instagram before triage.")
    instagram_triage.set_defaults(handler=handle_instagram_triage_shortlist)

    instagram_ranking = instagram_subcommands.add_parser(
        "benchmark-ranking-quality",
        help="Benchmark triage ranking quality across varied themes against a lower-ranked comparison sample.",
    )
    add_device_arg(instagram_ranking)
    add_wda_url_arg(instagram_ranking)
    instagram_ranking.add_argument("--output-dir", help="Directory for ranking benchmark artifacts.")
    instagram_ranking.add_argument("--prefix", default="instagram-ranking-quality")
    instagram_ranking.add_argument("--theme", action="append", help="Theme/query to benchmark. Repeat for multiple themes.")
    instagram_ranking.add_argument("--candidates-per-theme", type=int, default=30)
    instagram_ranking.add_argument("--verify-top", type=int, default=10)
    instagram_ranking.add_argument("--comparison-size", type=int, default=5)
    instagram_ranking.add_argument("--comparison-start-rank", type=int, default=10)
    instagram_ranking.add_argument("--source-deadline-seconds", type=float, default=60)
    instagram_ranking.add_argument("--verification-deadline-seconds", type=float, default=180)
    instagram_ranking.add_argument("--per-candidate-deadline-seconds", type=float, default=30)
    instagram_ranking.add_argument("--max-source-scrolls", type=int, default=1)
    instagram_ranking.add_argument("--source-open-wait-seconds", type=float, default=1.5)
    instagram_ranking.add_argument("--no-launch", action="store_true", help="Do not launch Instagram before benchmarking.")
    instagram_ranking.set_defaults(handler=handle_instagram_benchmark_ranking_quality)

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

    ui = subcommands.add_parser("ui", help="UI capture commands backed by WebDriverAgent.")
    ui_subcommands = ui.add_subparsers(dest="ui_command")
    ui_screenshot = ui_subcommands.add_parser("screenshot", help="Capture a screenshot through WebDriverAgent.")
    add_device_arg(ui_screenshot)
    add_wda_url_arg(ui_screenshot)
    ui_screenshot.add_argument("--output", help="Optional path for the PNG screenshot.")
    ui_screenshot.set_defaults(handler=handle_ui_screenshot)

    ui_source = ui_subcommands.add_parser("source", help="Capture the accessibility source through WebDriverAgent.")
    add_device_arg(ui_source)
    add_wda_url_arg(ui_source)
    ui_source.add_argument("--output", help="Optional path for the source XML/text.")
    ui_source.set_defaults(handler=handle_ui_source)

    ui_elements = ui_subcommands.add_parser("elements", help="Save visible accessibility elements as JSON.")
    add_device_arg(ui_elements)
    add_wda_url_arg(ui_elements)
    ui_elements.add_argument("--output", help="Optional path for elements JSON.")
    ui_elements.add_argument("--all", action="store_true", help="Include elements marked not visible.")
    ui_elements.set_defaults(handler=handle_ui_elements)

    ui_annotated = ui_subcommands.add_parser("annotated-screenshot", help="Capture screenshot plus element map/HTML overlay.")
    add_device_arg(ui_annotated)
    add_wda_url_arg(ui_annotated)
    ui_annotated.add_argument("--output", help="Optional path for the PNG screenshot.")
    ui_annotated.add_argument("--all", action="store_true", help="Include elements marked not visible.")
    ui_annotated.set_defaults(handler=handle_ui_annotated_screenshot)

    ui_tap = ui_subcommands.add_parser("tap", help="Tap absolute screen coordinates through WebDriverAgent.")
    add_device_arg(ui_tap)
    add_wda_url_arg(ui_tap)
    ui_tap.add_argument("--x", type=float, required=True)
    ui_tap.add_argument("--y", type=float, required=True)
    ui_tap.set_defaults(handler=handle_ui_tap)

    ui_tap_text = ui_subcommands.add_parser("tap-text", help="Tap the center of a visible element matching text.")
    add_device_arg(ui_tap_text)
    add_wda_url_arg(ui_tap_text)
    ui_tap_text.add_argument("text")
    ui_tap_text.add_argument("--exact", action="store_true", help="Require exact text/name/label/value match.")
    ui_tap_text.set_defaults(handler=handle_ui_tap_text)

    ui_wait_text = ui_subcommands.add_parser("wait-text", help="Wait until visible text appears.")
    add_device_arg(ui_wait_text)
    add_wda_url_arg(ui_wait_text)
    ui_wait_text.add_argument("text")
    ui_wait_text.add_argument("--timeout", type=float, default=10.0)
    ui_wait_text.add_argument("--interval", type=float, default=0.5)
    ui_wait_text.add_argument("--exact", action="store_true")
    ui_wait_text.set_defaults(handler=handle_ui_wait_text)

    ui_scroll_until_text = ui_subcommands.add_parser("scroll-until-text", help="Scroll up until visible text appears.")
    add_device_arg(ui_scroll_until_text)
    add_wda_url_arg(ui_scroll_until_text)
    ui_scroll_until_text.add_argument("text")
    ui_scroll_until_text.add_argument("--max-scrolls", type=int, default=8)
    ui_scroll_until_text.add_argument("--exact", action="store_true")
    ui_scroll_until_text.add_argument("--from-x", type=float, default=200)
    ui_scroll_until_text.add_argument("--from-y", type=float, default=720)
    ui_scroll_until_text.add_argument("--to-x", type=float, default=200)
    ui_scroll_until_text.add_argument("--to-y", type=float, default=260)
    ui_scroll_until_text.add_argument("--duration", type=float, default=0.2)
    ui_scroll_until_text.set_defaults(handler=handle_ui_scroll_until_text)

    ui_type = ui_subcommands.add_parser("type", help="Type text into the currently focused field.")
    add_device_arg(ui_type)
    add_wda_url_arg(ui_type)
    ui_type.add_argument("text")
    ui_type.add_argument("--frequency", type=int, help="Optional WDA typing frequency override.")
    ui_type.set_defaults(handler=handle_ui_type)

    ui_clear = ui_subcommands.add_parser("clear-field", help="Clear the focused field or a field matched by text.")
    add_device_arg(ui_clear)
    add_wda_url_arg(ui_clear)
    ui_clear.add_argument("text", nargs="?", help="Optional visible text/name/label to tap before clearing.")
    ui_clear.add_argument("--exact", action="store_true")
    ui_clear.set_defaults(handler=handle_ui_clear_field)

    ui_drag = ui_subcommands.add_parser("drag", help="Drag between absolute screen coordinates.")
    add_device_arg(ui_drag)
    add_wda_url_arg(ui_drag)
    ui_drag.add_argument("--from-x", type=float, required=True)
    ui_drag.add_argument("--from-y", type=float, required=True)
    ui_drag.add_argument("--to-x", type=float, required=True)
    ui_drag.add_argument("--to-y", type=float, required=True)
    ui_drag.add_argument("--duration", type=float, default=0.1, help="Press duration before dragging, in seconds.")
    ui_drag.set_defaults(handler=handle_ui_drag)

    ui_press_button = ui_subcommands.add_parser("press-button", help="Press an iPhone hardware button.")
    add_device_arg(ui_press_button)
    add_wda_url_arg(ui_press_button)
    ui_press_button.add_argument("name", help="Button name, for example home, volumeUp, volumeDown, or siri.")
    ui_press_button.add_argument("--duration", type=float, help="Optional press duration in seconds.")
    ui_press_button.set_defaults(handler=handle_ui_press_button)

    ui_back = ui_subcommands.add_parser("back", help="Navigate back through WDA.")
    add_device_arg(ui_back)
    add_wda_url_arg(ui_back)
    ui_back.set_defaults(handler=handle_ui_back)

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
    return DeviceCtl(
        developer_dir=args.developer_dir,
        evidence_base=args.evidence_dir,
        timeout=args.timeout,
    )


def wda_client_from_args(args: argparse.Namespace) -> WDAClient:
    return WDAClient(url=resolve_wda_url_from_args(args), timeout=args.timeout)


def resolve_wda_url_from_args(args: argparse.Namespace) -> str:
    explicit = getattr(args, "url", None)
    if explicit:
        return explicit
    config = load_config()
    config_url = config.wda_url
    if config_url:
        return config_url

    client = client_from_args(args)
    device = client.select_device(device_selector_from_args(args, config=config))
    url, _ = client.coredevice_wda_url(device.identifier, port=DEFAULT_WDA_PORT)
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
    try:
        client = client_from_args(args)
        device = client.select_device(device_selector_from_args(args))
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

    wda = WDAClient(url=url, timeout=args.timeout)
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
    print(f"result: {'ok' if healthy else 'attention-required'}")
    return 0 if healthy else 1


def handle_apps_list(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    device = client.select_device(device_selector_from_args(args))
    apps, artifact = client.list_apps(device.identifier, include_all=not args.no_all)
    for app in apps:
        print(f"{app.name}\t{app.bundle_identifier}\t{app.version}\t{app.bundle_version}")
    print(f"evidence: {artifact}")
    return 0


def handle_apps_find(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    device = client.select_device(device_selector_from_args(args))
    app = client.find_app(device.identifier, args.query)
    print(f"{app.name}\t{app.bundle_identifier}\t{app.version}\t{app.bundle_version}")
    return 0


def handle_apps_launch(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    device = client.select_device(device_selector_from_args(args))
    app = client.find_app(device.identifier, args.query)
    if not args.skip_lock_check:
        ensure_unlocked_or_attempt_wda(args, client, device.identifier)
    client.launch_app(device.identifier, app.bundle_identifier)
    print(f"launched: {app.name} ({app.bundle_identifier}) on {device.name}")
    return 0


def handle_apps_terminate(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    device = client.select_device(device_selector_from_args(args))
    app = client.find_app(device.identifier, args.query)
    client.terminate_app(device.identifier, app.bundle_identifier)
    print(f"terminated: {app.name} ({app.bundle_identifier}) on {device.name}")
    return 0


def handle_instagram_smoke(args: argparse.Namespace) -> int:
    result = instagram_smoke(client_from_args(args), device_selector=device_selector_from_args(args))
    print(f"device: {result.device.name} ({result.device.identifier})")
    print(f"instagram: {result.app_name} ({result.bundle_identifier})")
    print(f"lock-state evidence: {result.lock_state_artifact}")
    print(f"apps evidence: {result.apps_artifact}")
    print("result: launched")
    return 0


def handle_instagram_capture_context(args: argparse.Namespace) -> int:
    capture = capture_instagram_context(
        wda_client_from_args(args),
        output_dir=args.output_dir or args.evidence_dir,
        prefix=args.prefix,
    )
    print(f"screenshot: {capture.screenshot}")
    print(f"source: {capture.source}")
    print(f"manifest: {capture.manifest}")
    current_reel = capture.payload.get("current_reel")
    current_profile = capture.payload.get("current_profile")
    visible_videos = capture.payload.get("visible_videos") or []
    if current_reel:
        print(f"current reel: {current_reel}")
    if current_profile:
        print(f"current profile: {current_profile}")
    if visible_videos:
        print(f"visible videos: {len(visible_videos)}")
        for video in visible_videos[:8]:
            print(f"- {video.get('creator')}\t{video.get('plays')}\t{video.get('rect')}")
    return 0


def handle_instagram_verify_handles(args: argparse.Namespace) -> int:
    if not args.no_launch:
        client = client_from_args(args)
        device = client.select_device(device_selector_from_args(args))
        ensure_unlocked_or_attempt_wda(args, client, device.identifier)
        app = client.find_app(device.identifier, "Instagram")
        client.launch_app(device.identifier, app.bundle_identifier)
    result = verify_handles(
        wda_client_from_args(args),
        args.handles,
        output_dir=args.output_dir or args.evidence_dir,
        prefix=args.prefix,
        max_steps_per_handle=args.max_steps_per_handle,
        deadline_seconds=args.deadline_seconds,
    )
    print(f"manifest: {result.manifest}")
    for item in result.payload.get("handles", []):
        print(f"{item.get('handle')}: {item.get('status')}")
    return 0


def handle_instagram_analyze_video(args: argparse.Namespace) -> int:
    result = analyze_video(
        wda_client_from_args(args),
        args.video,
        prompt=args.prompt,
        output_dir=args.output_dir or args.evidence_dir,
        prefix=args.prefix,
        dry_run=args.dry_run,
        timeout=args.timeout,
    )
    print(f"manifest: {result.manifest}")
    print(f"status: {result.payload.get('status')}")
    if result.payload.get("blocker"):
        print(f"blocker: {result.payload.get('blocker')}")
    return 0 if result.payload.get("status") in {"analyzed", "dry_run"} else 1


def handle_instagram_discover_creators(args: argparse.Namespace) -> int:
    if not args.no_launch:
        launch_instagram_for_foreground_work(args)
    result = discover_creators(
        wda_client_from_args(args),
        args.query,
        output_dir=args.output_dir or args.evidence_dir,
        prefix=args.prefix,
        max_candidates=args.max_candidates,
        deadline_seconds=args.deadline_seconds,
        max_source_scrolls=args.max_source_scrolls,
        max_steps=args.max_steps,
        max_steps_per_candidate=args.max_steps_per_candidate,
        per_candidate_deadline_seconds=args.per_candidate_deadline_seconds,
        verification_mode=args.verification_mode,
        source_open_wait_seconds=args.source_open_wait_seconds,
    )
    print(f"manifest: {result.manifest}")
    print(f"report: {result.report}")
    summary = result.payload.get("summary", {})
    print(f"candidates found: {summary.get('candidates_found', 0)}")
    print(f"handles found: {summary.get('handles_found', 0)}")
    print(f"follower counts found: {summary.get('follower_counts_found', 0)}")
    print(f"likely under 10k: {summary.get('likely_under_10k_followers', 0)}")
    print(f"pregnancy/motherhood evidence: {summary.get('pregnancy_motherhood_evidence', 0)}")
    print(f"recency evidence: {summary.get('recency_evidence', 0)}")
    print(f"ui steps: {result.payload.get('ui_steps')}")
    return 0


def handle_instagram_benchmark_discovery(args: argparse.Namespace) -> int:
    if not args.no_launch:
        launch_instagram_for_foreground_work(args)
    result = benchmark_discovery(
        wda_client_from_args(args),
        output_dir=args.output_dir or args.evidence_dir,
        prefix=args.prefix,
        max_candidates_per_scenario=args.max_candidates_per_scenario,
        scenario_deadline_seconds=args.scenario_deadline_seconds,
        max_source_scrolls=args.max_source_scrolls,
        verification_mode=args.verification_mode,
        source_open_wait_seconds=args.source_open_wait_seconds,
    )
    print(f"manifest: {result.manifest}")
    print(f"report: {result.report}")
    summary = result.payload.get("summary", {})
    print(f"candidates found: {summary.get('candidates_found', 0)}")
    print(f"handles found: {summary.get('handles_found', 0)}")
    print(f"follower counts found: {summary.get('follower_counts_found', 0)}")
    print(f"likely under 10k: {summary.get('likely_under_10k_followers', 0)}")
    print(f"pregnancy/motherhood evidence: {summary.get('pregnancy_motherhood_evidence', 0)}")
    print(f"recency evidence: {summary.get('recency_evidence', 0)}")
    print(f"failed/ambiguous screens: {summary.get('failed_ambiguous_screens', 0)}")
    for target, passed in (result.payload.get("target_results") or {}).items():
        print(f"{target}: {'PASS' if passed else 'FAIL'}")
    return 0


def handle_instagram_triage_shortlist(args: argparse.Namespace) -> int:
    if not args.no_launch:
        launch_instagram_for_foreground_work(args)
    result = triage_shortlist(
        wda_client_from_args(args),
        output_dir=args.output_dir or args.evidence_dir,
        prefix=args.prefix,
        max_candidates_per_scenario=args.max_candidates_per_scenario,
        source_deadline_seconds=args.source_deadline_seconds,
        max_source_scrolls=args.max_source_scrolls,
        verify_top=args.verify_top,
        verification_deadline_seconds=args.verification_deadline_seconds,
        per_candidate_deadline_seconds=args.per_candidate_deadline_seconds,
        shortlist_size=args.shortlist_size,
        source_open_wait_seconds=args.source_open_wait_seconds,
    )
    print(f"manifest: {result.manifest}")
    print(f"report: {result.report}")
    summary = result.payload.get("summary", {})
    print(f"triage candidates found: {summary.get('triage_candidates_found', 0)}")
    print(f"triage unique handles: {summary.get('triage_unique_handles', 0)}")
    print(f"triage elapsed seconds: {summary.get('triage_elapsed_seconds')}")
    print(f"verified count: {summary.get('verified_count', 0)}")
    print(f"verification elapsed seconds: {summary.get('verification_elapsed_seconds')}")
    print(f"shortlist count: {summary.get('shortlist_count', 0)}")
    print(f"total elapsed seconds: {summary.get('elapsed_seconds')}")
    return 0


def handle_instagram_benchmark_ranking_quality(args: argparse.Namespace) -> int:
    if not args.no_launch:
        launch_instagram_for_foreground_work(args)
    result = benchmark_ranking_quality(
        wda_client_from_args(args),
        output_dir=args.output_dir or args.evidence_dir,
        prefix=args.prefix,
        themes=tuple(args.theme) if args.theme else None,  # type: ignore[arg-type]
        candidates_per_theme=args.candidates_per_theme,
        verify_top=args.verify_top,
        comparison_size=args.comparison_size,
        comparison_start_rank=args.comparison_start_rank,
        source_deadline_seconds=args.source_deadline_seconds,
        verification_deadline_seconds=args.verification_deadline_seconds,
        per_candidate_deadline_seconds=args.per_candidate_deadline_seconds,
        max_source_scrolls=args.max_source_scrolls,
        source_open_wait_seconds=args.source_open_wait_seconds,
    )
    print(f"manifest: {result.manifest}")
    print(f"report: {result.report}")
    summary = result.payload.get("summary", {})
    print(f"runs: {summary.get('runs', 0)}")
    print(f"runs with >=5 credible top leads: {summary.get('runs_with_at_least_5_credible_top_leads', 0)}")
    print(f"pass rate: {summary.get('pass_rate')}")
    print(f"target passed: {summary.get('target_passed')}")
    print(f"top precision: {summary.get('top_precision')}")
    print(f"comparison precision: {summary.get('comparison_precision')}")
    print(f"ranking lift vs comparison: {summary.get('ranking_lift_vs_comparison')}")
    return 0


def launch_instagram_for_foreground_work(args: argparse.Namespace) -> None:
    client = client_from_args(args)
    device = client.select_device(device_selector_from_args(args))
    ensure_unlocked_or_attempt_wda(args, client, device.identifier)
    app = client.find_app(device.identifier, "Instagram")
    client.launch_app(device.identifier, app.bundle_identifier)


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


def handle_wda_url(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    device = client.select_device(device_selector_from_args(args))
    url, artifact = client.coredevice_wda_url(device.identifier, port=args.port)
    print(f"url: {url}")
    print(f"device: {device.name} ({device.identifier})")
    print(f"evidence: {artifact}")
    return 0


def handle_wda_locked(args: argparse.Namespace) -> int:
    locked = wda_client_from_args(args).locked()
    value = "unknown" if locked is None else str(locked).lower()
    print(f"locked: {value}")
    return 0


def handle_wda_unlock(args: argparse.Namespace) -> int:
    wda = wda_client_from_args(args)
    wda.unlock()
    print("unlock-attempted: true")
    locked = wda.locked()
    if locked is not None:
        print(f"wda-locked: {str(locked).lower()}")
    if args.verify:
        client = client_from_args(args)
        device = client.select_device(device_selector_from_args(args))
        data, artifact = client.lock_state(device.identifier)
        result = data.get("result", {})
        passcode_required = result.get("passcodeRequired") if isinstance(result, dict) else None
        value = "unknown" if not isinstance(passcode_required, bool) else str(passcode_required).lower()
        print(f"passcode-required: {value}")
        print(f"evidence: {artifact}")
        if passcode_required is True:
            print("result: human-unlock-required")
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
        device = client.select_device(device_selector_from_args(args))
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
    if locked_after is None and passcode_required is False:
        print("result: verified-unlocked")
        return 0
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
    device = client.select_device(device_selector_from_args(args, config=config))
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


def ensure_unlocked_or_attempt_wda(args: argparse.Namespace, client: DeviceCtl, device_id: str) -> None:
    try:
        client.require_unlocked(device_id)
        return
    except DeviceLocked:
        pass

    wda_client_from_args(args).unlock()
    client.require_unlocked(device_id)


def passcode_required_from_lock_state(data: dict[str, object]) -> bool | None:
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    value = result.get("passcodeRequired")
    return value if isinstance(value, bool) else None


def bool_value(value: bool | None) -> str:
    return "unknown" if value is None else str(value).lower()


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


def handle_ui_elements(args: argparse.Namespace) -> int:
    path = UIController(
        wda_client_from_args(args),
        evidence_base=args.evidence_dir,
    ).save_elements(args.output, visible_only=not args.all)
    print(f"elements: {path}")
    return 0


def handle_ui_annotated_screenshot(args: argparse.Namespace) -> int:
    screenshot, elements, html = UIController(
        wda_client_from_args(args),
        evidence_base=args.evidence_dir,
    ).annotated_screenshot(args.output, visible_only=not args.all)
    print(f"screenshot: {screenshot}")
    print(f"elements: {elements}")
    print(f"annotation: {html}")
    return 0


def handle_ui_tap(args: argparse.Namespace) -> int:
    UIController(wda_client_from_args(args), evidence_base=args.evidence_dir).tap(args.x, args.y)
    print(f"tapped: {args.x:g},{args.y:g}")
    return 0


def handle_ui_tap_text(args: argparse.Namespace) -> int:
    element = UIController(wda_client_from_args(args), evidence_base=args.evidence_dir).tap_text(
        args.text,
        exact=args.exact,
    )
    print(f"tapped: {element.index}\t{element.type}\t{element.text}")
    return 0


def handle_ui_wait_text(args: argparse.Namespace) -> int:
    element = UIController(wda_client_from_args(args), evidence_base=args.evidence_dir).wait_text(
        args.text,
        timeout=args.timeout,
        interval=args.interval,
        exact=args.exact,
    )
    print(f"found: {element.index}\t{element.type}\t{element.text}")
    return 0


def handle_ui_scroll_until_text(args: argparse.Namespace) -> int:
    element = UIController(wda_client_from_args(args), evidence_base=args.evidence_dir).scroll_until_text(
        args.text,
        max_scrolls=args.max_scrolls,
        exact=args.exact,
        start_x=args.from_x,
        start_y=args.from_y,
        end_x=args.to_x,
        end_y=args.to_y,
        duration=args.duration,
    )
    print(f"found: {element.index}\t{element.type}\t{element.text}")
    return 0


def handle_ui_type(args: argparse.Namespace) -> int:
    UIController(wda_client_from_args(args), evidence_base=args.evidence_dir).type_text(
        args.text,
        frequency=args.frequency,
    )
    print(f"typed: {len(args.text)} chars")
    return 0


def handle_ui_clear_field(args: argparse.Namespace) -> int:
    element = UIController(wda_client_from_args(args), evidence_base=args.evidence_dir).clear_field(
        args.text,
        exact=args.exact,
    )
    if element is None:
        print("cleared: focused-field")
    else:
        print(f"cleared: {element.index}\t{element.type}\t{element.text}")
    return 0


def handle_ui_drag(args: argparse.Namespace) -> int:
    UIController(wda_client_from_args(args), evidence_base=args.evidence_dir).drag(
        args.from_x,
        args.from_y,
        args.to_x,
        args.to_y,
        duration=args.duration,
    )
    print(f"dragged: {args.from_x:g},{args.from_y:g} -> {args.to_x:g},{args.to_y:g}")
    return 0


def handle_ui_press_button(args: argparse.Namespace) -> int:
    UIController(wda_client_from_args(args), evidence_base=args.evidence_dir).press_button(
        args.name,
        duration=args.duration,
    )
    print(f"pressed: {args.name}")
    return 0


def handle_ui_back(args: argparse.Namespace) -> int:
    UIController(wda_client_from_args(args), evidence_base=args.evidence_dir).back()
    print("back: true")
    return 0
