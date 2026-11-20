"""Run the demo on 127.0.0.1 using the Python standard library."""

import argparse
import json
import secrets
import sqlite3
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from pilot import DecisionError, Pilot


def make_server(pilot, port=0):
    token = secrets.token_urlsafe(24)
    page = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
    page = page.replace("__DEMO_TOKEN__", token).encode()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body, mime="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def json_reply(self, status, payload):
            self.reply(status, json.dumps(payload, ensure_ascii=False).encode())

        def allowed(self):
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                self.json_reply(HTTPStatus.FORBIDDEN, {"error": "Откройте адрес 127.0.0.1."})
                return False
            if self.path == "/" or self.headers.get("X-Demo-Token") == token:
                return True
            self.json_reply(HTTPStatus.FORBIDDEN, {"error": "Обновите страницу стенда."})
            return False

        def do_GET(self):
            if not self.allowed():
                return
            if self.path == "/":
                self.reply(HTTPStatus.OK, page, "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self.json_reply(HTTPStatus.OK, pilot.snapshot())
            else:
                self.json_reply(HTTPStatus.NOT_FOUND, {"error": "Страница отсутствует."})

        def do_POST(self):
            if not self.allowed():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not (0 < length <= 8192):
                    raise DecisionError("Некорректный размер запроса.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise DecisionError("Ожидается объект JSON.")
                if self.path == "/api/submit":
                    state = pilot.submit(**data)
                elif self.path == "/api/resolve":
                    state = pilot.resolve(**data)
                elif self.path == "/api/mode":
                    pilot.set_mode(**data)
                    state = None
                else:
                    self.json_reply(HTTPStatus.NOT_FOUND, {"error": "Действие отсутствует."})
                    return
            except (ValueError, TypeError) as exc:
                message = str(exc) if isinstance(exc, DecisionError) else "Некорректный запрос."
                self.json_reply(HTTPStatus.BAD_REQUEST, {"error": message})
                return
            except sqlite3.Error:
                self.json_reply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Стенд временно недоступен."})
                return
            self.json_reply(HTTPStatus.OK, {"state": state})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--hold-seconds", type=int, default=300)
    parser.add_argument("--db", type=Path, help="Optional persistent demo database.")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="saferoute-") as tmp:
        pilot = Pilot(args.db or Path(tmp) / "pilot.sqlite", args.hold_seconds)
        with make_server(pilot, args.port) as server:
            address = f"http://127.0.0.1:{server.server_port}"
            print(address, flush=True)
            if not args.no_browser:
                webbrowser.open(address)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
