"""Fixed-target TCP relay for the Docker Desktop evaluation VM."""
import select
import socket
import socketserver
import sys
from threading import Thread


CONTROL_PORT = 7800
GATEWAY_PORT = 7801
PROXY_PORT = 3128


def transfer(client, target):
    with socket.create_connection(target, timeout=15) as upstream:
        client.settimeout(60)
        upstream.settimeout(60)
        peers = {client: upstream, upstream: client}
        while peers:
            readable, _, _ = select.select(list(peers), [], [], 300)
            if not readable:
                return
            for connection in readable:
                data = connection.recv(64 * 1024)
                other = peers[connection]
                if data:
                    other.sendall(data)
                else:
                    other.shutdown(socket.SHUT_WR)
                    del peers[connection]


class RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(agent, gateway_port, snowflake_host):
    class ConnectProxy(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(10)
            line = self.rfile.readline(1024).decode("ascii", errors="replace").split()
            total = 0
            while header := self.rfile.readline(4096):
                total += len(header)
                if total > 16 * 1024:
                    return
                if header == b"\r\n":
                    break
            if len(line) != 3 or line[:2] != ["CONNECT", f"{snowflake_host}:443"]:
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            try:
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                transfer(self.request, (snowflake_host, 443))
            except OSError:
                pass

    def handler(target):
        class FixedRelay(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    transfer(self.request, target)
                except OSError:
                    pass
        return FixedRelay

    with RelayServer(("0.0.0.0", CONTROL_PORT), handler((agent, 8000))) as control:
        with RelayServer(("0.0.0.0", GATEWAY_PORT), handler(("host.docker.internal", gateway_port))) as gateway:
            with RelayServer(("0.0.0.0", PROXY_PORT), ConnectProxy) as proxy:
                for server in (control, gateway):
                    Thread(target=server.serve_forever, daemon=True).start()
                proxy.serve_forever()


if __name__ == "__main__":
    serve(sys.argv[1], int(sys.argv[2]), sys.argv[3])
