"""Supervised before/after benchmark. No screenshots, UI text, or model calls.

Only foregrounds Calculator/Settings, verifies app identity and measures reads.
Use exactly this harness and settings against both source trees. Never replay a
failed/uncertain activation. Output is private, unique, and content-free.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", required=True, type=Path)
parser.add_argument("--output", required=True, type=Path)
parser.add_argument("--arm", required=True)
parser.add_argument("--repeats", type=int, default=5)
args = parser.parse_args()
if not 1 <= args.repeats <= 10:
    parser.error("repeats must be between 1 and 10")
args.source = args.source.resolve()
package_dir = args.source / "src/openclaw_iphone"
if not (package_dir / "__init__.py").is_file():
    parser.error("--source must contain src/openclaw_iphone/__init__.py")
source_files = sorted(package_dir.rglob("*.py"))
sys.path.insert(0, str(args.source / "src"))

from openclaw_iphone import __version__
from openclaw_iphone.actions import Condition, Executor, Grant
from openclaw_iphone.config import load_config
from openclaw_iphone.connection import TaskConnection
from openclaw_iphone.devicectl import DeviceCtl
from openclaw_iphone.evidence import write_private
from openclaw_iphone.tasks import Limits, TaskSpec, run_task


# sys.path does not displace cached modules. Only attest files actually belonging
# to this source tree, before configuration or any device work.
source_paths = {path.resolve() for path in source_files}
for name, module in tuple(sys.modules.items()):
    if name == "openclaw_iphone" or name.startswith("openclaw_iphone."):
        origin = getattr(module, "__file__", None)
        if origin is None or Path(origin).resolve() not in source_paths:
            parser.error(f"--source does not match imported module {name}")


CALC, SETTINGS = "com.apple.calculator", "com.apple.Preferences"
APPROVED = {CALC, SETTINGS}
pin = load_config().device
if not pin:
    raise SystemExit("An existing explicit physical-device configuration is required")
args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
stage = "initialization"
sequence = 0
metadata = {
    "arm": args.arm, "version": __version__, "repeats": args.repeats,
    "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    "source_sha256": {str(p.relative_to(args.source)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in source_files},
    "task_seconds": 90, "request_seconds": 20, "verification_seconds": 20,
    "freshness_seconds": 30, "model_calls": 0,
}
write_private(args.output / "manifest.json", json.dumps(metadata, indent=2) + "\n")


def record(data):
    global sequence
    sequence += 1
    data = dict(data, arm=args.arm, at=datetime.now(timezone.utc).isoformat())
    write_private(args.output / f"{sequence:03}.json", json.dumps(data, indent=2) + "\n")
    print(json.dumps({k: v for k, v in data.items() if k != "transport"}), flush=True)


def progress(value):
    global stage
    stage = value
    print(json.dumps({"stage": value}), flush=True)


def connection():
    return TaskConnection(DeviceCtl(timeout=20, evidence_base=str(args.output / "device-evidence")),
                          device=pin, seconds=90)


def identity(wda, expected):
    """Independent read-only check, outside the runtime being measured."""
    wda.require_unlocked()
    one, two = wda.active_app(), wda.active_app()
    if (one.get("bundleId") != expected or two.get("bundleId") != expected or
            type(one.get("pid")) is not int or one["pid"] <= 0 or one["pid"] != two.get("pid")):
        raise RuntimeError("Independent foreground verification failed")


def wait_identity(conn, expected):
    """Bounded read-only setup settling; never repeat the activation."""
    wda = conn.require_active()
    end = min(time.monotonic() + 20, conn.budget.deadline)
    previous = wda.deadline
    wda.deadline = min(previous, end) if previous is not None else end
    try:
        while True:
            wda.require_unlocked()
            one, two = wda.active_app(), wda.active_app()
            if (one.get("bundleId") == expected and two.get("bundleId") == expected and
                    type(one.get("pid")) is int and one["pid"] > 0 and one["pid"] == two.get("pid")):
                return
            if time.monotonic() >= end:
                raise RuntimeError("Independent foreground wait expired")
            conn.budget.sleep(min(0.2, end - time.monotonic()))
    finally:
        wda.deadline = previous


def reset(destination):
    """Identical untimed setup across arms, never an input retry."""
    started = time.monotonic()
    conn = connection()
    sent = False
    try:
        with conn:
            wda = conn.require_active()
            obs = Executor(conn, ()).observe()
            if obs.secure or obs.app not in APPROVED:
                raise RuntimeError("Unexpected or secure screen; benchmark stopped")
            identity(wda, obs.app)
            if obs.app != destination:
                sent = True
                wda.activate_app(destination)
            wait_identity(conn, destination)
    except Exception as exc:
        record({"kind": "setup", "status": "stopped", "activation_attempted": sent,
                "error_type": type(exc).__name__, "seconds": time.monotonic() - started,
                "transport": conn.metrics.summary(), "cleanup_failed": conn.cleanup_failed})
        raise
    record({"kind": "setup", "status": "verified", "activation_attempted": sent,
            "seconds": time.monotonic() - started, "transport": conn.metrics.summary(),
            "cleanup_failed": conn.cleanup_failed})
    if conn.cleanup_failed:
        raise RuntimeError("Setup cleanup failed")


def transition(repeat):
    progress(f"transition {repeat}: reset to Calculator")
    reset(CALC)
    progress(f"transition {repeat}: measured task")
    grant = Grant("activate", CALC, "Open Settings", destination=SETTINGS,
                  after=(Condition("app", SETTINGS),))
    spec = TaskSpec("Open Settings from Calculator", (grant,), grant.after,
                    limits=Limits(seconds=90, max_steps=3, freshness=30, verification_seconds=20))
    conn = connection()
    started = time.monotonic()
    try:
        with conn:
            ex = Executor(conn, spec.grants, freshness=30, verification_seconds=20)
            acquisition = time.monotonic() - started
            result = run_task(ex, spec)
        seconds = time.monotonic() - started
    except Exception as exc:
        record({"kind": "transition", "repeat": repeat, "status": "stopped",
                "error_type": type(exc).__name__, "seconds": time.monotonic() - started,
                "transport": conn.metrics.summary(), "cleanup_failed": conn.cleanup_failed})
        raise
    result.pop("snapshot", None)
    result.update(kind="transition", repeat=repeat, seconds=seconds,
                  acquisition_seconds=acquisition, transport=conn.metrics.summary(),
                  cleanup_failed=conn.cleanup_failed)
    record(result)
    if result["status"] != "completed" or result["verification"] != "satisfied" or conn.cleanup_failed:
        raise RuntimeError("Task was not verified; no activation replay")
    # Separate connection makes verification independent of cached runtime state.
    check = connection()
    with check:
        identity(check.require_active(), SETTINGS)
    record({"kind": "independent_check", "repeat": repeat, "status": "verified",
            "cleanup_failed": check.cleanup_failed, "transport": check.metrics.summary()})
    if check.cleanup_failed:
        raise RuntimeError("Independent check cleanup failed")


def settled_wait(app):
    progress(f"settled waits: {app}")
    reset(app)
    for repeat in range(1, args.repeats + 1):
        conn = connection()
        try:
            with conn:
                wda = conn.require_active()
                identity(wda, app)
                ex = Executor(conn, (), freshness=30, verification_seconds=20)
                # Exclude ownership and independent checks from the wait metric.
                from openclaw_iphone.execution import Metrics
                original_metrics = wda.metrics
                wait_metrics = Metrics()
                wda.metrics = wait_metrics
                started = time.monotonic()
                try:
                    state, obs = ex.wait((Condition("app", app),))
                finally:
                    elapsed = time.monotonic() - started
                    wda.metrics = original_metrics
                if state != "satisfied" or obs.app != app:
                    raise RuntimeError("App wait did not verify expected identity")
                identity(wda, app)
        except Exception as exc:
            record({"kind": "settled_wait", "app": app, "repeat": repeat, "status": "stopped",
                    "error_type": type(exc).__name__, "cleanup_failed": conn.cleanup_failed})
            raise
        record({"kind": "settled_wait", "app": app, "repeat": repeat, "status": "verified",
                "seconds": elapsed, "transport": wait_metrics.summary(), "cleanup_failed": conn.cleanup_failed})
        if conn.cleanup_failed:
            raise RuntimeError("Wait cleanup failed")


try:
    for repeat in range(1, args.repeats + 1):
        transition(repeat)
    settled_wait(SETTINGS)
    settled_wait(CALC)
    progress("finished; Calculator foreground")
except Exception as exc:
    record({"kind": "stopped", "stage": stage, "error_type": type(exc).__name__})
    raise SystemExit(1) from None
