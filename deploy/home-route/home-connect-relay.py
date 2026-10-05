import select
import socket
import socketserver


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        remote = None
        self.request.settimeout(20)
        try:
            header = b""
            while b"\r\n\r\n" not in header:
                part = self.request.recv(4096)
                if not part or len(header) > 65536:
                    return
                header += part
            line = header.split(b"\r\n", 1)[0].decode()
            method, target, _ = line.split(" ", 2)
            host, port = target.rsplit(":", 1)
            if method != "CONNECT" or int(port) != 443:
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            remote = socket.create_connection((host, int(port)), timeout=20)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.request.settimeout(None)
            remote.settimeout(None)
            while True:
                ready, _, _ = select.select([self.request, remote], [], [], 60)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (remote if source is self.request else self.request).sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            if remote:
                remote.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


with Server(("127.0.0.1", 18781), Handler) as server:
    print("home-relay-ready", flush=True)
    server.serve_forever()
