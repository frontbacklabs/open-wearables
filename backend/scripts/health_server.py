"""Minimal HTTP health server.

Cloud Run's startup probe requires a service container to listen on ``$PORT``.
A Celery worker serves no HTTP, so we run this tiny server in the background
alongside the worker (see ``scripts/start/worker_cloudrun.sh``) purely to satisfy
that probe. It responds 200 to any GET request.
"""

import http.server
import os
import socketserver


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:
        """Silence per-request access logging."""


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    with socketserver.TCPServer(("", port), _HealthHandler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
