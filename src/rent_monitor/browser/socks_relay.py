"""Loopback HTTP CONNECT adapter for authenticated SOCKS5, with byte accounting."""

from __future__ import annotations

import select
import socket
import socketserver
import struct
import threading


def read_exact(sock: socket.socket, length: int) -> bytes:
    result = b""
    while len(result) < length:
        part = sock.recv(length - len(result))
        if not part:
            raise OSError("Proxy connection closed")
        result += part
    return result


class SocksRelay:
    def __init__(self, credentials) -> None:
        self.credentials = credentials
        self.up_bytes = 0
        self.down_bytes = 0
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()
        self._stopping = threading.Event()
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                owner._handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self.up_bytes + self.down_bytes

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stopping.set()
        with self._lock:
            sockets = tuple(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def _handle(self, client: socket.socket) -> None:
        upstream = None
        established = False
        try:
            client.settimeout(20)
            raw = b""
            while b"\r\n\r\n" not in raw:
                part = client.recv(4096)
                if not part or len(raw) > 65536:
                    return
                raw += part
            headers, remaining = raw.split(b"\r\n\r\n", 1)
            method, authority, _ = headers.split(b"\r\n", 1)[0].split(b" ", 2)
            if method != b"CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            host, port = authority.decode("ascii").rsplit(":", 1)
            # Browser traffic needs HTTPS; never turn this into an arbitrary LAN relay.
            if int(port) != 443:
                raise OSError("Unsupported destination port")
            credentials = self.credentials
            upstream = socket.create_connection((credentials.host, credentials.port), timeout=20)
            with self._lock:
                self._sockets.update((client, upstream))
            upstream.sendall(bytes([5, 1, 2]))
            if read_exact(upstream, 2) != bytes([5, 2]):
                raise OSError("SOCKS method rejected")
            username = credentials.username.encode()
            password = credentials.password.encode()
            if not 1 <= len(username) <= 255 or not 1 <= len(password) <= 255:
                raise OSError("Invalid SOCKS credentials")
            upstream.sendall(
                bytes([1, len(username)]) + username + bytes([len(password)]) + password
            )
            if read_exact(upstream, 2) != bytes([1, 0]):
                raise OSError("SOCKS authentication rejected")
            encoded = host.encode("idna")
            if len(encoded) > 255:
                raise OSError("Invalid destination")
            upstream.sendall(bytes([5, 1, 0, 3, len(encoded)]) + encoded + struct.pack("!H", 443))
            reply = read_exact(upstream, 4)
            if reply[0] != 5 or reply[1] != 0:
                raise OSError("SOCKS destination rejected")
            size = {1: 4, 4: 16}.get(reply[3])
            if reply[3] == 3:
                size = read_exact(upstream, 1)[0]
            if size is None:
                raise OSError("Invalid SOCKS reply")
            read_exact(upstream, size + 2)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            established = True
            if remaining:
                upstream.sendall(remaining)
                with self._lock:
                    self.up_bytes += len(remaining)
            upstream.settimeout(None)
            client.settimeout(None)
            while not self._stopping.is_set():
                ready, _, _ = select.select((client, upstream), (), (), 10)
                for source in ready:
                    payload = source.recv(65536)
                    if not payload:
                        return
                    destination = upstream if source is client else client
                    destination.sendall(payload)
                    with self._lock:
                        if source is client:
                            self.up_bytes += len(payload)
                        else:
                            self.down_bytes += len(payload)
        except (OSError, ValueError, UnicodeError):
            if not established:
                try:
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                except OSError:
                    pass
        finally:
            with self._lock:
                self._sockets.discard(client)
                self._sockets.discard(upstream)
            if upstream is not None:
                upstream.close()
