"""One bounded HTTP exchange. No proxies, redirects, pooling or automatic replay."""
from __future__ import annotations

import http.client
import queue
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit


class TransportFailure(Exception):
    def __init__(self, category: str, phase: str) -> None:
        self.category, self.phase = category, phase
        super().__init__(category)


def exchange(url: str, path: str, method: str, body: bytes | None, timeout: float,
             *, max_bytes: int) -> tuple[int, bytes]:
    deadline = time.monotonic() + timeout
    endpoint = urlsplit(url)
    phase = "dns"

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TransportFailure("deadline", phase)
        return seconds

    port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
    try:
        # CoreDevice/loopback endpoints are numeric: avoid a resolver thread
        # altogether, including IPv6 addresses with a scope identifier.
        addresses = socket.getaddrinfo(endpoint.hostname, port, type=socket.SOCK_STREAM,
                                       flags=socket.AI_NUMERICHOST)
    except socket.gaierror:
        # DNS has no portable socket timeout. Only this read-only lookup may
        # outlive a deadline; it never sends HTTP or retains ownership.
        resolved = queue.Queue(maxsize=1)
        def resolve() -> None:
            try:
                resolved.put(socket.getaddrinfo(endpoint.hostname, port, type=socket.SOCK_STREAM))
            except OSError as exc:
                resolved.put(exc)
        threading.Thread(target=resolve, daemon=True).start()
        try:
            addresses = resolved.get(timeout=remaining())
        except queue.Empty as exc:
            raise TransportFailure("deadline", phase) from exc
        if isinstance(addresses, OSError):
            raise TransportFailure("transport", phase) from addresses

    active: list[socket.socket] = []
    def abort() -> None:
        # Shutdown interrupts buffered header/body reads, including trickles.
        for sock in active[:]:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    timer = threading.Timer(remaining(), abort)
    timer.daemon = True
    timer.start()
    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port,
                                            timeout=remaining())
    response = None
    try:
        phase = "connect"
        for family, kind, protocol, _, address in addresses:
            sock = socket.socket(family, kind, protocol)
            active.append(sock)
            try:
                sock.settimeout(remaining())
                sock.connect(address)
            except OSError:
                sock.close()
                active.remove(sock)
                remaining()
                continue
            break
        else:
            raise TransportFailure("transport", phase)
        if endpoint.scheme == "https":
            phase = "tls"
            secured = ssl.create_default_context().wrap_socket(sock,
                server_hostname=endpoint.hostname, do_handshake_on_connect=False)
            active.append(secured)
            sock = secured
            sock.settimeout(remaining())
            sock.do_handshake()
        connection.sock = sock
        phase = "send"
        sock.settimeout(remaining())
        connection.request(method, endpoint.path.rstrip("/") + path, body=body,
            headers={"Content-Type": "application/json"} if body is not None else {})
        phase = "headers"
        response = connection.getresponse()
        remaining()
        if response.length is not None and response.length > max_bytes:
            raise TransportFailure("response_too_large", "body")
        phase = "body"
        data = response.read(max_bytes + 1)
        remaining()
        if len(data) > max_bytes:
            raise TransportFailure("response_too_large", phase)
        if response.length not in (None, 0):
            raise TransportFailure("truncated_response", phase)
        return response.status, data
    except (OSError, http.client.HTTPException) as exc:
        category = "deadline" if time.monotonic() >= deadline or isinstance(exc, TimeoutError) else "transport"
        raise TransportFailure(category, phase) from exc
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()
        for sock in active:
            sock.close()
