"""Production WSGI entrypoint for BZ Bets.

Render probes the root path with HEAD requests while detecting/monitoring the
service. The Flask '/' route performs the full prediction pipeline, so allowing
HEAD to reach Flask needlessly rebuilds models, calls external APIs and writes
tracking data. This middleware short-circuits those probes before Flask runs.
"""

from app import app as flask_app


class LightweightProbeMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        method = (environ.get("REQUEST_METHOD") or "GET").upper()
        path = environ.get("PATH_INFO") or "/"

        if path == "/health" or (path == "/" and method == "HEAD"):
            body = b"OK"
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/plain; charset=utf-8"),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-store"),
                ],
            )
            return [body]

        return self.app(environ, start_response)


application = LightweightProbeMiddleware(flask_app)
