import contextlib
import io
import unittest
from unittest.mock import Mock, patch
from openclaw_iphone import cli
from openclaw_iphone.errors import WDAUnavailable
from openclaw_iphone.execution import Budget
from openclaw_iphone.wda import WDAClient
from test_cli_config import FakeDeviceCtl, FakeWDA

class ReadHealthTests(unittest.TestCase):
    def test_screen_reads_capped_without_shortening_mutation_timeout(self):
        default = WDAClient(url="http://wda.test", timeout=30)
        with patch.object(default, "_send", return_value=b"{}") as send:
            default._request("/source")
            self.assertEqual(send.call_args.kwargs["timeout"], 30)
        wda = WDAClient(url="http://wda.test", timeout=30, read_timeout=3)
        with patch.object(wda, "_send", return_value=b"{}") as send:
            for path in ("/source", "/screenshot"):
                wda._request(path)
                self.assertEqual(send.call_args.kwargs["timeout"], 3)
            wda._request("/session/one/element/ref/click", method="POST", payload={})
            self.assertEqual(send.call_args.kwargs["timeout"], 30)
            wda.budget = Budget.seconds(0.5)
            wda._request("/source")
            self.assertLessEqual(send.call_args.kwargs["timeout"], 0.5)
        for value in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                WDAClient(url="http://wda.test", read_timeout=value)

    def test_doctor_does_not_equate_ready_with_usable_source(self):
        args = cli.build_parser().parse_args(["doctor", "--check-ui"])
        wda = FakeWDA()
        wda.source = Mock(side_effect=WDAUnavailable("PRIVATE"))
        with patch("openclaw_iphone.cli.client_from_args", return_value=FakeDeviceCtl()), \
             patch("openclaw_iphone.cli.resolve_wda_url_from_args", return_value="http://wda.test"), \
             patch("openclaw_iphone.cli.WDAClient", return_value=wda), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.handle_doctor(args), 1)
        self.assertIn("screen-read-failed", output.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual(wda.unlock_count, 0)
