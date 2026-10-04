"""Real HTTP boundary: slow headers/bodies, bounded reads and uncertain writes."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest

from openclaw_iphone.errors import WDAOutcomeUnknown, WDAUnavailable
from openclaw_iphone.wda import WDAClient


class DeadlineTests(unittest.TestCase):
    @contextmanager
    def server(self, mode):
        writes = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                writes.append((self.command, self.path))
                body = b'{"value":{"ready":true}}'
                try:
                    if mode == "headers":
                        self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                        for _ in range(20):
                            self.wfile.write(b"X-Slow: yes\r\n")
                            self.wfile.flush()
                            time.sleep(0.02)
                        self.wfile.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                    else:
                        self.send_response(500 if mode == "error" else 200)
                        self.send_header("Content-Length", "50000000" if mode == "oversized" else str(len(body)))
                        self.end_headers()
                        if mode == "oversized":
                            return
                        for byte in body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.02)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = respond
            do_POST = respond

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                yield WDAClient(url=f"http://127.0.0.1:{server.server_port}", timeout=0.08), writes
            finally:
                server.shutdown()
                thread.join()

    def test_trickling_headers_success_and_error_bodies_obey_elapsed_deadline(self):
        for mode in ("headers", "body", "error"):
            with self.subTest(mode=mode), self.server(mode) as (client, writes):
                started = time.monotonic()
                with self.assertRaises(WDAUnavailable):
                    client.status()
                self.assertLess(time.monotonic() - started, 0.3)
                self.assertEqual(writes, [("GET", "/status")])

    def test_slow_mutation_is_unknown_and_is_never_replayed(self):
        with self.server("body") as (client, writes):
            started = time.monotonic()
            with self.assertRaises(WDAOutcomeUnknown):
                client._json_post("/actions", {})
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertEqual(writes, [("POST", "/actions")])

    def test_oversized_body_is_rejected_before_reading_it(self):
        with self.server("oversized") as (client, writes):
            with self.assertRaises(WDAUnavailable) as caught:
                client.status()
            self.assertEqual(caught.exception.category, "response_too_large")
            self.assertEqual(writes, [("GET", "/status")])
