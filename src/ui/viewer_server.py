"""Local HTTP host for the Sketchfab Viewer API page (Chrome/Edge --app dock)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_STATIC = Path(__file__).resolve().parent / "static"
_HOST = "127.0.0.1"
_PORT = 8765

_server: ThreadingHTTPServer | None = None
_lock = threading.Lock()
_state = {
    "uid": "",
    "name": "",
    "thumb": "",
}


def get_state() -> dict:
    return dict(_state)


def set_model(uid: str, name: str = "", thumb: str = "") -> None:
    _state["uid"] = (uid or "").strip()
    _state["name"] = (name or "").strip()
    _state["thumb"] = (thumb or "").strip()


def viewer_url(uid: str | None = None, name: str = "", thumb: str = "", *, overlay: bool = False) -> str:
    u = (uid or _state.get("uid") or "").strip()
    start_server()
    q = f"uid={u}"
    if name:
        q += f"&name={_q(name)}"
    if thumb:
        q += f"&thumb={_q(thumb)}"
    if overlay:
        q += "&overlay=1"
    return f"http://{_HOST}:{_PORT}/viewer.html?{q}"


def inspector_url(uid: str | None = None, name: str = "") -> str:
    u = (uid or _state.get("uid") or "").strip()
    start_server()
    q = f"uid={u}"
    if name:
        q += f"&name={_q(name)}"
    return f"http://{_HOST}:{_PORT}/inspector.html?{q}"


def _q(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


def start_server() -> int:
    global _server
    with _lock:
        if _server is not None:
            return _PORT

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: N802
                return

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path or "/"
                if path in ("/", "/viewer.html"):
                    html = (_STATIC / "viewer.html").read_bytes()
                    self._send(200, html, "text/html; charset=utf-8")
                    return
                if path == "/inspector.html":
                    html = (_STATIC / "inspector.html").read_bytes()
                    self._send(200, html, "text/html; charset=utf-8")
                    return
                if path == "/api/model":
                    qs = parse_qs(parsed.query or "")
                    uid = (qs.get("uid") or [""])[0] or _state.get("uid") or ""
                    payload = {
                        "uid": uid,
                        "name": _state.get("name") or "",
                        "thumb": _state.get("thumb") or "",
                    }
                    self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
                    return
                if path.startswith("/static/"):
                    rel = path[len("/static/") :]
                    fp = _STATIC / rel
                    if fp.is_file() and _STATIC in fp.resolve().parents:
                        ctype = "application/javascript" if fp.suffix == ".js" else "text/css"
                        self._send(200, fp.read_bytes(), ctype)
                        return
                self._send(404, b"not found", "text/plain")

        try:
            _server = ThreadingHTTPServer((_HOST, _PORT), Handler)
        except OSError:
            # Port busy — assume our previous process or another instance owns it.
            _server = None
            return _PORT

        t = threading.Thread(target=_server.serve_forever, daemon=True)
        t.start()
        return _PORT


def stop_server() -> None:
    """Stop the local viewer HTTP server (app shutdown). Non-blocking."""
    global _server
    with _lock:
        srv = _server
        _server = None
    if srv is not None:

        def _stop() -> None:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass

        threading.Thread(target=_stop, daemon=True).start()
