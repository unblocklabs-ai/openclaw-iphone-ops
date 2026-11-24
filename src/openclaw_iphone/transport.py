"""Bounded HTTP exchanges with optional per-client keep-alive pooling; no proxies, redirects or blind write replay."""
from __future__ import annotations

import http.client
import queue
import select
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit


class TransportFailure(Exception):
    def __init__(self, category: str, phase: str) -> None:
        self.category, self.phase = category, phase
        super().__init__(category)


def _socket_looks_closed(sock: socket.socket) -> bool:
    """True when an idle keep-alive socket already has FIN/EOF or error pending."""
    try:
        readable, _, errored = select.select([sock], [], [sock], 0.0)
        return bool(readable or errored)
    except (OSError, ValueError):
        return True


class ConnectionPool:
    """Thread-safe LIFO pool of idle HTTP/1.1 connections scoped to one client."""

    def __init__(self, *, max_idle: int = 4) -> None:
        self._lock = threading.Lock()
        self._idle: dict[tuple[str, str, int], list[tuple[http.client.HTTPConnection, socket.socket]]] = {}
        self._max_idle = max_idle
        self._closed = False

    def acquire(self, key: tuple[str, str, int]) -> tuple[http.client.HTTPConnection, socket.socket] | None:
        with self._lock:
            if self._closed:
                return None
            bucket = self._idle.get(key)
            while bucket:
                conn, sock = bucket.pop()
                if conn.sock is not None and not _socket_looks_closed(sock):
                    return conn, sock
                self._discard(conn, sock)
        return None

    def release(self, key: tuple[str, str, int], conn: http.client.HTTPConnection, sock: socket.socket) -> None:
        with self._lock:
            if self._closed or conn.sock is None:
                self._discard(conn, sock)
                return
            bucket = self._idle.setdefault(key, [])
            if len(bucket) >= self._max_idle:
                self._discard(conn, sock)
                return
            bucket.append((conn, sock))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            buckets = list(self._idle.values())
            self._idle.clear()
        for bucket in buckets:
            for conn, sock in bucket:
                self._discard(conn, sock)

    @staticmethod
    def _discard(conn: http.client.HTTPConnection, sock: socket.socket) -> None:
        try:
            conn.close()
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def _resolve_addresses(hostname: str | None, port: int, remaining) -> list:
    try:
        # CoreDevice/loopback endpoints are numeric: avoid a resolver thread
        # altogether, including IPv6 addresses with a scope identifier.
        return socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
    except socket.gaierror:
        # DNS has no portable socket timeout. Only this read-only lookup may
        # outlive a deadline; it never sends HTTP or retains ownership.
        resolved: queue.Queue = queue.Queue(maxsize=1)
        def resolve() -> None:
            try:
                resolved.put(socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM))
            except OSError as exc:
                resolved.put(exc)
        threading.Thread(target=resolve, daemon=True).start()
        try:
            addresses = resolved.get(timeout=remaining())
        except queue.Empty as exc:
            raise TransportFailure("deadline", "dns") from exc
        if isinstance(addresses, OSError):
            raise TransportFailure("transport", "dns") from addresses
        return addresses


def exchange(url: str, path: str, method: str, body: bytes | None, timeout: float,
             *, max_bytes: int, pool: ConnectionPool | None = None,
             allow_stale_retry: bool = False) -> tuple[int, bytes]:
    deadline = time.monotonic() + timeout
    endpoint = urlsplit(url)
    port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
    pool_key = (endpoint.scheme, endpoint.hostname or "", port)
    addresses: list | None = None

    def remaining(phase: str = "dns") -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TransportFailure("deadline", phase)
        return seconds

    for attempt in range(2):
        phase = "dns"
        reused = pool.acquire(pool_key) if pool is not None else None
        if reused is None and addresses is None:
            addresses = _resolve_addresses(endpoint.hostname, port, lambda: remaining("dns"))

        active: list[socket.socket] = []
        aborted = threading.Event()
        def abort() -> None:
            aborted.set()
            # Shutdown interrupts buffered header/body reads, including trickles.
            for sock in active[:]:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        timer = threading.Timer(remaining(phase), abort)
        timer.daemon = True
        timer.start()
        connection = reused[0] if reused is not None else http.client.HTTPConnection(
            endpoint.hostname, endpoint.port, timeout=remaining(phase)
        )
        sock = reused[1] if reused is not None else None
        if sock is not None:
            active.append(sock)
        response = None
        keep_open = False
        try:
            if sock is None:
                phase = "connect"
                assert addresses is not None
                for family, kind, protocol, _, address in addresses:
                    candidate = socket.socket(family, kind, protocol)
                    active.append(candidate)
                    try:
                        candidate.settimeout(remaining(phase))
                        candidate.connect(address)
                    except OSError:
                        candidate.close()
                        active.remove(candidate)
                        remaining(phase)
                        continue
                    sock = candidate
                    break
                else:
                    raise TransportFailure("transport", phase)
                if endpoint.scheme == "https":
                    phase = "tls"
                    secured = ssl.create_default_context().wrap_socket(sock,
                        server_hostname=endpoint.hostname, do_handshake_on_connect=False)
                    active.append(secured)
                    sock = secured
                    sock.settimeout(remaining(phase))
                    sock.do_handshake()
                connection.sock = sock
            phase = "send"
            sock.settimeout(remaining(phase))
            headers = {"Connection": "keep-alive" if pool is not None else "close"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, endpoint.path.rstrip("/") + path, body=body, headers=headers)
            phase = "headers"
            response = connection.getresponse()
            remaining(phase)
            if response.length is not None and response.length > max_bytes:
                raise TransportFailure("response_too_large", "body")
            phase = "body"
            data = response.read(max_bytes + 1)
            remaining(phase)
            if len(data) > max_bytes:
                raise TransportFailure("response_too_large", phase)
            if response.length not in (None, 0):
                raise TransportFailure("truncated_response", phase)
            # Cancel timer before releasing socket to pool so an old timer can never abort a future lease.
            timer.cancel()
            keep_open = (
                pool is not None
                and not aborted.is_set()
                and response.version == 11
                and not response.will_close
                and connection.sock is sock
            )
            return response.status, data
        except (OSError, http.client.HTTPException) as exc:
            # On a read-only request, a stale pooled socket closed by the server can be replaced once.
            if (reused is not None and allow_stale_retry and attempt == 0
                    and phase in ("send", "headers") and time.monotonic() < deadline
                    and isinstance(exc, (http.client.RemoteDisconnected, http.client.BadStatusLine,
                                         BrokenPipeError, ConnectionResetError))):
                continue
            category = "deadline" if time.monotonic() >= deadline or isinstance(exc, TimeoutError) else "transport"
            raise TransportFailure(category, phase) from exc
        finally:
            timer.cancel()
            if response is not None:
                response.close()
            if keep_open and pool is not None and sock is not None:
                pool.release(pool_key, connection, sock)
            else:
                connection.close()
                for item in active:
                    try:
                        item.close()
                    except OSError:
                        pass
    raise TransportFailure("transport", "connect")
