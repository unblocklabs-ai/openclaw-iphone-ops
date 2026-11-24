from __future__ import annotations

import base64
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

from openclaw_iphone import cache, cli
from openclaw_iphone.config import load_config
from openclaw_iphone.connection import Connection
from openclaw_iphone.d1_mlx import (
    DEFAULT_MLX_MODEL,
    D1MLXEngine,
    MAX_IMAGE_ASSET_BYTES,
    build_install_commands,
    create_d1_server,
    install_mlx_runtime,
    probe_d1_health,
)
from openclaw_iphone.devicectl import App, Device, DeviceCtl
from openclaw_iphone.errors import AppNotFound, OpenClawIPhoneError
from openclaw_iphone.goal import LiquidDecisions
from openclaw_iphone.wda import WDAStatus


TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)


class D1MLXTests(unittest.TestCase):
    def test_build_install_commands_uv_and_pip(self) -> None:
        venv = Path("/tmp/fake-mlx-venv")
        uv_cmds = build_install_commands(venv, python_executable="/usr/bin/python3", uv_path="/opt/homebrew/bin/uv")
        self.assertEqual(uv_cmds[0][:3], ["/opt/homebrew/bin/uv", "venv", "--python"])
        self.assertIn("mlx-vlm>=0.7.8", uv_cmds[1])
        self.assertIn("pillow", uv_cmds[1])

        pip_cmds = build_install_commands(venv, python_executable="/usr/bin/python3", uv_path=None)
        self.assertEqual(pip_cmds[0], ["/usr/bin/python3", "-m", "venv", str(venv)])
        self.assertEqual(pip_cmds[1][0], str(venv / "bin" / "python"))
        self.assertIn("mlx-vlm>=0.7.8", pip_cmds[1])

    def test_install_mlx_runtime_runs_commands_and_optional_prefetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / "mlx-venv"
            recorded: list[list[str]] = []

            def fake_runner(cmd: list[str], *, check: bool = False) -> None:
                self.assertTrue(check)
                recorded.append(cmd)

            with mock.patch("openclaw_iphone.d1_mlx.shutil.which", return_value=None):
                info = install_mlx_runtime(venv, model="LiquidAI/d1-omni-600M-MLX-8bit", prefetch=True, runner=fake_runner)

            self.assertEqual(info["installer"], "pip")
            self.assertTrue(info["prefetched"])
            self.assertEqual(len(recorded), 3)
            self.assertEqual(recorded[2][-1], "LiquidAI/d1-omni-600M-MLX-8bit")
            self.assertIn("mlx_vlm", recorded[2][2])

    def test_engine_extracts_images_and_cleans_up_temp_files(self) -> None:
        captured: dict[str, object] = {}

        def predictor(state: str | None, questions: dict, images: list[str]) -> dict:
            captured["state"] = state
            captured["questions"] = questions
            captured["images"] = list(images)
            captured["exists_during_call"] = [Path(p).is_file() for p in images]
            captured["bytes_during_call"] = [Path(p).read_bytes() for p in images]
            return {"answers": {"ok": 0.91}}

        engine = D1MLXEngine("LiquidAI/d1-omni-600M-MLX-8bit", predictor=predictor)
        reply = engine.predict_systemone({
            "state": [{"screenshot": "img1"}, {"text": "Goal: Open Search"}],
            "assets": {
                "img1": {
                    "type": "image",
                    "format": "png",
                    "data_base64": base64.b64encode(TINY_PNG).decode("ascii"),
                }
            },
            "questions": {"ok": {"type": "noul", "instructions": "Ready?"}},
        })

        self.assertEqual(reply["model"], "LiquidAI/d1-omni-600M-MLX-8bit")
        self.assertEqual(reply["answers"], {"ok": {"type": "noul", "noul": 0.91}})
        self.assertEqual(captured["state"], "Goal: Open Search")
        self.assertEqual(captured["exists_during_call"], [True])
        self.assertEqual(captured["bytes_during_call"], [TINY_PNG])
        for path in captured["images"]:  # type: ignore[union-attr]
            self.assertFalse(Path(path).exists(), "Temporary image asset must be deleted after predict()")

    def test_engine_normalizes_prompt_key_to_instructions(self) -> None:
        seen_questions: dict[str, object] = {}

        def predictor(state: str | None, questions: dict, images: list[str]) -> dict:
            seen_questions.update(questions)
            return {"answers": {"q": {"type": "bool", "value": True, "probability": 0.77}}}

        engine = D1MLXEngine(predictor=predictor)
        reply = engine.predict_systemone({"questions": {"q": {"type": "noul", "prompt": "Legacy prompt?"}}})
        self.assertEqual(seen_questions["q"]["instructions"], "Legacy prompt?")  # type: ignore[index]
        self.assertEqual(reply["answers"], {"q": {"type": "noul", "noul": 0.77}})

    def test_engine_rejects_bad_base64_and_oversized_asset(self) -> None:
        engine = D1MLXEngine(predictor=lambda s, q, i: {"answers": {}})
        with self.assertRaises(ValueError):
            engine.predict_systemone({"questions": {}})
        with self.assertRaises(ValueError):
            engine.predict_systemone({
                "state": [{"screenshot": "img1"}],
                "assets": {"img1": {"type": "image", "format": "jpg", "data_base64": "!!!not-base64!!!"}},
                "questions": {"a": {"type": "noul", "instructions": "x"}},
            })
        oversized = base64.b64encode(b"x" * (MAX_IMAGE_ASSET_BYTES + 1)).decode("ascii")
        with self.assertRaises(ValueError):
            engine.predict_systemone({
                "state": [{"screenshot": "img1"}],
                "assets": {"img1": {"type": "image", "format": "jpg", "data_base64": oversized}},
                "questions": {"a": {"type": "noul", "instructions": "x"}},
            })

    def test_server_rejects_non_loopback_by_default_and_serves_liquid_decisions_end_to_end(self) -> None:
        with self.assertRaises(ValueError):
            create_d1_server("0.0.0.0", 8781, engine=D1MLXEngine(predictor=lambda s, q, i: {"answers": {}}))

        seen: list[tuple[str | None, dict, int]] = []

        # Emit the exact native output schema returned by Blaizzy/mlx-vlm d1_omni.predict().
        def predictor(state: str | None, questions: dict, images: list[str]) -> dict:
            seen.append((state, questions, len(images)))
            answers = {}
            for name, spec in questions.items():
                kind = spec.get("type")
                if kind == "noul":
                    answers[name] = {"type": "bool", "value": True, "probability": 0.88}
                elif kind == "choice":
                    opts = list(spec.get("criteria", {}).keys())
                    probs = {opt: (0.9 if i == 0 else 0.1 / max(1, len(opts) - 1)) for i, opt in enumerate(opts)}
                    answers[name] = {
                        "type": "choice",
                        "value": opts[0],
                        "probabilities": probs,
                        "metadata": {"confidence": 0.9},
                    }
                elif kind == "score":
                    answers[name] = {
                        "type": "score",
                        "value": 3.0,
                        "probabilities": {"0": 0.0, "1": 0.0, "2": 0.0, "3": 1.0},
                        "metadata": {"confidence": 0.99},
                    }
            return {"answers": answers}

        engine = D1MLXEngine(DEFAULT_MLX_MODEL, predictor=predictor)
        server = create_d1_server("127.0.0.1", 0, engine=engine)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{port}/v1"
            health = probe_d1_health(base_url)
            self.assertIsNotNone(health)
            self.assertEqual(health["status"], "ok")  # type: ignore[index]
            self.assertEqual(health["backend"], "mlx-vlm")  # type: ignore[index]
            self.assertTrue(health["loaded"])  # type: ignore[index]

            client = LiquidDecisions(api_key="", model=DEFAULT_MLX_MODEL, base_url=base_url)
            self.assertFalse(client.supports_box)
            answers = client.ask({
                "input": [{
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xd9").decode("ascii")},
                        {"type": "input_text", "text": "Goal: Open Search"},
                    ],
                }],
                "questions": [
                    {"name": "done", "type": "predicate", "instructions": "Is it done?"},
                    {"name": "next", "type": "choice", "instructions": "Next action?", "choices": [
                        {"value": "Search", "description": "Tap Search"},
                        {"value": "Back", "description": "Tap Back"},
                    ]},
                    {"name": "progress", "type": "score", "instructions": "Progress?", "levels": [
                        {"description": "0"}, {"description": "1"}, {"description": "2"}, {"description": "3"},
                    ]},
                ],
            })
            self.assertAlmostEqual(answers["done"]["probability"], 0.88)
            self.assertEqual(answers["next"]["choice"], "Search")
            self.assertAlmostEqual(answers["next"]["probabilities"]["Search"], 0.9)
            self.assertAlmostEqual(answers["progress"]["score"], 3.0)
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0][2], 1)

            # Invalid JSON returns HTTP 400.
            req = urllib.request.Request(
                f"{base_url}/systemone",
                data=b"{bad",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req, timeout=2)
            self.assertEqual(err.exception.code, 400)

            # Oversized Content-Length returns HTTP 413 without reading body.
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                conn.putrequest("POST", "/v1/systemone")
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(20 * 1024 * 1024))
                conn.endheaders()
                resp = conn.getresponse()
                self.assertEqual(resp.status, 413)
            finally:
                conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_server_supports_ipv6_loopback(self) -> None:
        engine = D1MLXEngine(DEFAULT_MLX_MODEL, predictor=lambda s, q, i: {"answers": {}})
        try:
            server = create_d1_server("::1", 0, engine=engine)
        except OSError:
            self.skipTest("IPv6 loopback unavailable in this environment")
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            health = probe_d1_health(f"http://[::1]:{port}/v1")
            self.assertIsNotNone(health)
            self.assertEqual(health["status"], "ok")  # type: ignore[index]
        finally:
            server.shutdown()
            server.server_close()

    def test_engine_warmup_and_server_logs_inference_errors_to_stderr(self) -> None:
        seen_warmup: list[tuple[str | None, int]] = []

        def warmup_predictor(state: str | None, questions: dict, images: list[str]) -> dict:
            seen_warmup.append((state, len(images)))
            # Verify warmup image starts with JPEG SOI marker.
            self.assertEqual(len(images), 1)
            self.assertTrue(Path(images[0]).read_bytes().startswith(b"\xff\xd8\xff"))
            return {"answers": {"ok": {"type": "bool", "value": True, "probability": 0.99}}}

        engine = D1MLXEngine(DEFAULT_MLX_MODEL, predictor=warmup_predictor)
        engine.warmup()
        self.assertEqual(seen_warmup, [("Warmup check.", 1)])

        def failing_predictor(state: str | None, questions: dict, images: list[str]) -> dict:
            raise RuntimeError("boom-mlx-kernel")

        err_server = create_d1_server("127.0.0.1", 0, engine=D1MLXEngine(DEFAULT_MLX_MODEL, predictor=failing_predictor))
        port = err_server.server_address[1]
        thread = threading.Thread(target=err_server.serve_forever, daemon=True)
        thread.start()
        stderr_buf = io.StringIO()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/systemone",
                data=json.dumps({"state": "hi", "questions": {"ok": {"type": "noul", "instructions": "x"}}}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with contextlib.redirect_stderr(stderr_buf):
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(req, timeout=2)
            self.assertEqual(err.exception.code, 500)
            self.assertIn("boom-mlx-kernel", stderr_buf.getvalue())
        finally:
            err_server.shutdown()
            err_server.server_close()

    def test_handle_d1_serve_reexecs_symlinked_venv_and_guards_against_loops(self) -> None:
        import argparse
        import builtins
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / "mlx-venv"
            (venv / "bin").mkdir(parents=True)
            venv_py = venv / "bin" / "python"
            venv_py.symlink_to(sys.executable)

            real_import = builtins.__import__

            def fake_import(name, *args, **kwargs):
                if name == "mlx_vlm":
                    raise ImportError("no mlx_vlm")
                return real_import(name, *args, **kwargs)

            args = argparse.Namespace(host="127.0.0.1", port=8781, model=DEFAULT_MLX_MODEL, venv=str(venv), allow_non_loopback=False, warmup=True)
            exec_calls: list[tuple[str, list[str], dict[str, str]]] = []

            def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
                exec_calls.append((path, argv, env))
                raise SystemExit(0)

            with mock.patch.dict(os.environ, {"HOME": tmp}, clear=False), \
                 mock.patch.object(builtins, "__import__", side_effect=fake_import), \
                 mock.patch("openclaw_iphone.cli.os.execve", side_effect=fake_execve):
                os.environ.pop("OPENCLAW_IPHONE_D1_REEXEC", None)
                with self.assertRaises(SystemExit):
                    cli.handle_d1_serve(args)

            self.assertEqual(len(exec_calls), 1)
            exec_path, exec_argv, exec_env = exec_calls[0]
            self.assertEqual(exec_path, str(venv_py))
            self.assertEqual(exec_env["OPENCLAW_IPHONE_D1_REEXEC"], "1")
            self.assertIn(f"--model={DEFAULT_MLX_MODEL}", exec_argv)
            self.assertIn("--warmup", exec_argv)

            # With sentinel already set, never loop — raise clean OpenClawIPhoneError.
            with mock.patch.dict(os.environ, {"HOME": tmp, "OPENCLAW_IPHONE_D1_REEXEC": "1"}, clear=False), \
                 mock.patch.object(builtins, "__import__", side_effect=fake_import):
                with self.assertRaises(OpenClawIPhoneError):
                    cli.handle_d1_serve(args)

    def test_liquid_supports_box_default_by_model_and_override(self) -> None:
        self.assertFalse(LiquidDecisions(api_key="", model="LiquidAI/d1-omni-600M-MLX-8bit", base_url="http://127.0.0.1:8781/v1").supports_box)
        self.assertFalse(LiquidDecisions(api_key="", model="d1-omni-2B", base_url="http://127.0.0.1:8781/v1").supports_box)
        self.assertFalse(LiquidDecisions(api_key="", model="LiquidAI/d1-3B", base_url="http://127.0.0.1:8781/v1").supports_box)
        self.assertTrue(LiquidDecisions(api_key="k", model="d1.2").supports_box)
        self.assertTrue(LiquidDecisions(api_key="k", model="d1.2-ultra").supports_box)
        self.assertTrue(
            LiquidDecisions(api_key="", model="LiquidAI/d1-omni-600M-MLX-8bit",
                            base_url="http://127.0.0.1:8781/v1", supports_box=True).supports_box
        )

    def test_cli_d1_install_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.env"
            cfg.write_text("", encoding="utf-8")
            venv = Path(tmp) / "mlx-venv"
            env = {**os.environ, "HOME": tmp, "OPENCLAW_IPHONE_CONFIG": str(cfg)}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch("openclaw_iphone.cli.install_mlx_runtime", return_value={
                     "venv": str(venv),
                     "python": str(venv / "bin" / "python"),
                     "model": DEFAULT_MLX_MODEL,
                     "installer": "uv",
                     "prefetched": False,
                 }):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = cli.main(["d1", "install", "--venv", str(venv), "--port", "8799", "--configure-navigator"])
                self.assertEqual(rc, 0)
                loaded = load_config(env=env)
                self.assertEqual(loaded.get("OPENCLAW_IPHONE_GOAL_ENABLED"), "1")
                self.assertEqual(loaded.get("OPENCLAW_IPHONE_GOAL_PROVIDER"), "liquid")
                self.assertEqual(loaded.get("OPENCLAW_IPHONE_GOAL_MODEL"), DEFAULT_MLX_MODEL)
                self.assertEqual(loaded.get("OPENCLAW_IPHONE_LIQUID_BASE_URL"), "http://127.0.0.1:8799/v1")

                out2 = io.StringIO()
                with contextlib.redirect_stdout(out2):
                    rc2 = cli.main(["d1", "install", "--venv", str(venv), "--port", "8799", "--configure-guard"])
                self.assertEqual(rc2, 0)
                loaded2 = load_config(env=env)
                self.assertEqual(loaded2.get("OPENCLAW_IPHONE_LOCAL_GUARD_URL"), "http://127.0.0.1:8799/v1")
                self.assertEqual(loaded2.get("OPENCLAW_IPHONE_LOCAL_GUARD_MODEL"), DEFAULT_MLX_MODEL)

                out3 = io.StringIO()
                with mock.patch("openclaw_iphone.cli.is_mlx_installed", return_value=True), \
                     mock.patch("openclaw_iphone.cli.probe_d1_health", return_value={"status": "ok", "model": DEFAULT_MLX_MODEL, "loaded": True}), \
                     contextlib.redirect_stdout(out3):
                    rc3 = cli.main(["d1", "status"])
                self.assertEqual(rc3, 0)
                self.assertIn("mlx-installed: true", out3.getvalue())
                self.assertIn("server: reachable", out3.getvalue())


class RuntimeCacheTests(unittest.TestCase):
    def test_wda_endpoint_cache_ttl_clear_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            cache.set_cached_wda_endpoint(
                udid="UDID-1", identifier="CORE-1", name="Pearl iPhone", model="iPhone 12 Pro", url="http://[fd00::1]:8100"
            )
            path = cache.runtime_cache_path()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            hit = cache.get_cached_wda_endpoint("UDID-1")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["url"], "http://[fd00::1]:8100")  # type: ignore[index]
            self.assertEqual(cache.get_cached_wda_endpoint("pearl iphone")["udid"], "UDID-1")  # type: ignore[index]

            # Expire via time patch.
            with mock.patch("openclaw_iphone.cache.time.time", return_value=10**12):
                self.assertIsNone(cache.get_cached_wda_endpoint("UDID-1"))

            cache.clear_cached_wda_endpoint("UDID-1")
            self.assertIsNone(cache.get_cached_wda_endpoint("UDID-1"))

    def test_last_app_cache_roundtrip_and_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            self.assertIsNone(cache.get_cached_last_app("UDID-1"))
            cache.set_cached_last_app("UDID-1", "com.atebits.Tweetie2")
            self.assertEqual(cache.get_cached_last_app("UDID-1"), "com.atebits.Tweetie2")
            self.assertEqual(cache.runtime_cache_path().stat().st_mode & 0o777, 0o600)
            with mock.patch("openclaw_iphone.cache.time.time", return_value=10**12):
                self.assertIsNone(cache.get_cached_last_app("UDID-1"))

    def test_devicectl_find_app_uses_cache_and_refreshes_on_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            ctl = DeviceCtl()
            first_apps = [App(name="Safari", bundle_identifier="com.apple.mobilesafari")]
            second_apps = first_apps + [App(name="Instagram", bundle_identifier="com.burbn.instagram")]
            list_calls = 0

            def fake_list_apps(device_id: str, *, include_all: bool = False, keep: bool = False):
                nonlocal list_calls
                list_calls += 1
                return (first_apps if list_calls == 1 else second_apps), Path(tmp) / "apps.json"

            with mock.patch.object(ctl, "list_apps", side_effect=fake_list_apps):
                app1 = ctl.find_app("DEV-1", "Safari")
                self.assertEqual(app1.bundle_identifier, "com.apple.mobilesafari")
                self.assertEqual(list_calls, 1)

                # Cached hit: no live list_apps call.
                app2 = ctl.find_app("DEV-1", "Safari")
                self.assertEqual(app2.bundle_identifier, "com.apple.mobilesafari")
                self.assertEqual(list_calls, 1)

                # Cache miss for newly installed app automatically refreshes live.
                app3 = ctl.find_app("DEV-1", "Instagram")
                self.assertEqual(app3.bundle_identifier, "com.burbn.instagram")
                self.assertEqual(list_calls, 2)

                # Truly nonexistent app raises AppNotFound after one live refresh.
                with self.assertRaises(AppNotFound):
                    ctl.find_app("DEV-1", "NonexistentApp")
                self.assertEqual(list_calls, 3)

    def test_connection_uses_cached_wda_endpoint_without_devicectl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            cache.set_cached_wda_endpoint(
                udid="UDID-1", identifier="CORE-1", name="Pearl", model="iPhone", url="http://127.0.0.1:8100"
            )
            ctl = DeviceCtl()
            lock_path = Path(tmp) / "control.lock"

            class _FakeSessionCM:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            with mock.patch.object(ctl, "select_device", side_effect=AssertionError("should not shell out to select_device")), \
                 mock.patch.object(ctl, "coredevice_wda_url", side_effect=AssertionError("should not shell out to coredevice_wda_url")), \
                 mock.patch("openclaw_iphone.connection.WDAClient.status", return_value=WDAStatus(url="http://127.0.0.1:8100", payload={"value": {"ready": True}}, ready=True)), \
                 mock.patch("openclaw_iphone.connection.WDAClient.session", return_value=_FakeSessionCM()):
                with Connection(ctl, device="UDID-1", lock_path=lock_path) as conn:
                    self.assertTrue(conn.valid)
                    self.assertEqual(conn.device.udid, "UDID-1")  # type: ignore[union-attr]
                    self.assertEqual(conn.wda.url, "http://127.0.0.1:8100")  # type: ignore[union-attr]

    def test_connection_clears_corrupted_cache_url_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            cache.set_cached_wda_endpoint(
                udid="UDID-1", identifier="CORE-1", name="Pearl", model="iPhone", url="not-a-valid-url"
            )
            ctl = DeviceCtl()
            lock_path = Path(tmp) / "control.lock"

            class _FakeSessionCM:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            live_device = Device(name="Pearl", identifier="CORE-1", state="connected", model="iPhone", udid="UDID-1")
            with mock.patch.object(ctl, "select_device", return_value=live_device) as sel, \
                 mock.patch.object(ctl, "coredevice_wda_url", return_value=("http://127.0.0.1:8100", Path(tmp) / "d.json")) as url_mock, \
                 mock.patch("openclaw_iphone.connection.WDAClient.session", return_value=_FakeSessionCM()):
                with Connection(ctl, device="UDID-1", lock_path=lock_path) as conn:
                    self.assertTrue(conn.valid)
                    self.assertEqual(conn.wda.url, "http://127.0.0.1:8100")  # type: ignore[union-attr]
            sel.assert_called_once()
            url_mock.assert_called_once()
            refreshed = cache.get_cached_wda_endpoint("UDID-1")
            self.assertEqual(refreshed["url"], "http://127.0.0.1:8100")  # type: ignore[index]

    def test_watchdog_failure_counter_increments_resets_and_handles_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            self.assertEqual(cache.update_watchdog_failures("DEV-1", failed=True), 1)
            self.assertEqual(cache.update_watchdog_failures("DEV-1", failed=True), 2)
            self.assertEqual(cache.update_watchdog_failures("DEV-1", failed=False), 0)
            self.assertEqual(cache.update_watchdog_failures("DEV-1", failed=True), 1)

            # Corrupted non-int value recovers cleanly instead of raising ValueError.
            cache.watchdog_state_path().write_text('{"DEV-1": {"consecutive_failures": "corrupt"}}', encoding="utf-8")
            self.assertEqual(cache.update_watchdog_failures("DEV-1", failed=True), 1)

            # Corrupted non-string app entry is rejected cleanly.
            cache.runtime_cache_path().parent.mkdir(parents=True, exist_ok=True)
            cache.runtime_cache_path().write_text(
                '{"apps": {"DEV-1": {"updated_at": 9999999999, "items": [{"name": 123, "bundle_identifier": "x"}]}}}',
                encoding="utf-8",
            )
            self.assertIsNone(cache.get_cached_apps("DEV-1"))


if __name__ == "__main__":
    unittest.main()
