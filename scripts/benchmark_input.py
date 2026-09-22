"""Physical Safari input benchmark; synthetic local content, no accounts or models.

Run on the USB host with PYTHONPATH pointing at the build under test. Fixture
loading/focus is excluded from operation timings but reported separately.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
import uuid

import openclaw_iphone
from openclaw_iphone.actions import Condition, Executor, Grant
from openclaw_iphone.adaptive import AdaptiveAct, parse_scope
from openclaw_iphone.config import load_config
from openclaw_iphone.connection import TaskConnection
from openclaw_iphone.devicectl import DeviceCtl
from openclaw_iphone.evidence import write_private
from openclaw_iphone.observations import Selector

APP = "com.apple.mobilesafari"
FIELD = Selector("XCUIElementTypeTextField", label="Synthetic input")
PAGE = b'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local input benchmark</title><style>body{font:22px system-ui;padding:20px}
input,button{font:22px system-ui;padding:12px;margin:12px 0}label{display:block}</style>
<h1>Local input benchmark</h1><label for="entry">Synthetic input</label>
<input id="entry" aria-label="Synthetic input" inputmode="numeric" autocomplete="off"
autocorrect="off" autocapitalize="off"><br>
<button onclick="document.getElementById('result').textContent='Completed'">Finish</button>
<p id="result" role="status">Ready</p>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True, help="Host LAN address reachable from the iPhone")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--digits", choices=("121212", "111111", "123456"), default="121212")
    parser.add_argument("--auto-submit", action="store_true", help="Replace the field with a locally verified destination")
    parser.add_argument("--cases", nargs="+", choices=("keypad", "grant-keypad", "sequential", "tap"),
                        default=["keypad", "grant-keypad", "sequential", "tap"])
    args = parser.parse_args()
    if not 1 <= args.runs <= 10:
        parser.error("runs must be 1–10")
    if args.output.exists() or not args.output.parent.is_dir():
        parser.error("output must be a new file inside an existing private directory")
    pin = load_config().device
    if not pin:
        parser.error("an existing explicit device pin is required")
    os.umask(0o077)
    fixture_id = uuid.uuid4().hex
    page = PAGE
    if args.auto_submit:
        page += ("<script>const field=document.getElementById('entry');"
                 "field.addEventListener('input',()=>{if(field.value.length===6){"
                 f"document.getElementById('result').textContent=field.value==='{args.digits}'?'Completed':'Incorrect input';"
                 "field.remove();}});</script>").encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/" + fixture_id:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((args.address, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://{args.address}:{server.server_port}/{fixture_id}"
    input_path = args.output.parent / ("synthetic-" + fixture_id)
    write_private(input_path, args.digits)  # Synthetic patterns catch merged/lost touches.
    results = []
    connection = TaskConnection(DeviceCtl(), device=pin, seconds=1500)
    destination = Condition("exists", APP, Selector("XCUIElementTypeStaticText", label="Completed"))
    after = ({"kind": "exists", "app": APP, "target": {"role": destination.target.role, "label": destination.target.label}}
             if args.auto_submit else {"kind": "value", "app": APP,
                 "target": {"role": FIELD.role, "label": FIELD.label}, "value": args.digits})
    try:
        with connection:
            for case in args.cases:
                for run in range(args.runs):
                    setup_started = time.monotonic()
                    ex = Executor(connection, (), verification_seconds=8)
                    wda = connection.require_active()
                    wda.open_url(url + f"?case={case}&run={run}")
                    state, _ = ex.wait((Condition("actionable", APP, FIELD),))
                    if state != "satisfied":
                        raise RuntimeError("Synthetic fixture not ready")
                    actor = AdaptiveAct(ex, parse_scope({"apps": [APP], "operations": ["tap", "input", "keypad"],
                                                         "inputs": {"test": str(input_path)}}))
                    if case != "tap":
                        focus = actor.act({"op": "act", "action": "tap", "instruction": "Synthetic input",
                                           "target": {"role": FIELD.role, "label": FIELD.label},
                                           "after": [{"kind": "focused", "app": APP,
                                                      "target": {"role": FIELD.role, "label": FIELD.label}}]})
                        if focus["verification"] != "satisfied":
                            raise RuntimeError("Synthetic field not focused")
                    setup_seconds = time.monotonic() - setup_started
                    grant = Grant("keypad", APP, "Enter synthetic digits", FIELD, text_id="test",
                                  after=(destination,) if args.auto_submit else ())
                    if case == "grant-keypad":
                        ex = Executor(connection, (grant,), texts={"test": args.digits}, verification_seconds=8)
                        offer, = ex.offers(ex.observe())
                    started, index = time.monotonic(), len(connection.metrics.events)
                    if case == "grant-keypad":
                        step = ex.execute(offer.id)
                        result = {"dispatch": step.dispatch, "verification": step.verification, "reason": step.reason}
                    elif case == "tap":
                        result = actor.act({"op": "act", "action": "tap", "instruction": "Finish",
                            "target": {"role": "XCUIElementTypeButton", "label": "Finish"},
                            "after": [{"kind": "exists", "app": APP,
                                       "target": {"role": "XCUIElementTypeStaticText", "label": "Completed"}}]})
                    else:
                        result = actor.act({"op": "act", "action": "keypad" if case == "keypad" else "input",
                            "instruction": "Synthetic input", "target": {"role": FIELD.role, "label": FIELD.label},
                            "text_ref": "test", **({"strategy": "sequential"} if case == "sequential" else {}),
                            **({"after": [after]} if case == "keypad" or args.auto_submit else {})})
                    events = connection.metrics.events[index:]
                    row = {"case": case, "run": run, "seconds": time.monotonic() - started,
                           "setup_seconds": setup_seconds, "completed": result.get("verification") == "satisfied",
                           "result": {k: result[k] for k in ("dispatch", "verification", "reason")},
                           "device_calls": len(events), "events": events}
                    results.append(row)
                    print(json.dumps({k: v for k, v in row.items() if k != "events"}), flush=True)
                    if not row["completed"]:
                        raise RuntimeError("Synthetic operation did not verify; no replay")
    finally:
        server.shutdown()
        server.server_close()
        input_path.unlink()  # Only the exact synthetic file created above.
        package_dir = Path(openclaw_iphone.__file__).parent
        write_private(args.output, json.dumps({"label": args.label, "results": results,
            "auto_submit": args.auto_submit,
            "pattern": {"121212": "alternating", "111111": "repeated", "123456": "mixed"}[args.digits],
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "source_sha256": {str(p.relative_to(package_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(package_dir.rglob("*.py"))},
            "cleanup_warning": connection.cleanup_failed, "transport": connection.metrics.summary()}, indent=2))


if __name__ == "__main__":
    main()
