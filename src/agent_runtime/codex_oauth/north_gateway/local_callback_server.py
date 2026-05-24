# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_runtime.codex_oauth.service import CodexOAuthError, CodexOAuthService

logger = logging.getLogger(__name__)


_SUCCESS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Codex Authorization Successful</title></head>
<body style="font-family: system-ui; background: #0f172a; color: #e2e8f0; display: grid; place-items: center; height: 100vh; margin: 0;">
  <main style="text-align: center;"><h1>Codex 授权成功</h1><p>你可以关闭此窗口并返回 Spread AI。</p></main>
  <script>setTimeout(() => window.close(), 1500)</script>
</body></html>"""


def _error_html(message: str) -> str:
    escaped = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Codex Authorization Failed</title></head>
<body style="font-family: system-ui; background: #0f172a; color: #e2e8f0; display: grid; place-items: center; height: 100vh; margin: 0;">
  <main style="text-align: center; max-width: 720px;"><h1 style="color: #f87171;">Codex 授权失败</h1><p style="color: #cbd5e1;">{escaped}</p></main>
</body></html>"""


class CodexOAuthLocalCallbackServer:
    def __init__(self, service: CodexOAuthService) -> None:
        self._service = service
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        host, port, callback_path = self._service.local_callback_bind_address()
        service = self._service

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != callback_path:
                    self._send_text(404, "Not found")
                    return
                params = urllib.parse.parse_qs(parsed.query)
                error = _first(params, "error")
                error_description = _first(params, "error_description")
                if error:
                    self._send_html(400, _error_html(error_description or error))
                    return
                state = _first(params, "state")
                code = _first(params, "code")
                if not state or not code:
                    self._send_html(400, _error_html("缺少 Codex OAuth state 或 code。"))
                    return
                try:
                    service.complete_login(state=state, code=code)
                except CodexOAuthError as exc:
                    self._send_html(400, _error_html(str(exc)))
                    return
                except Exception as exc:
                    logger.exception("Codex OAuth 本地回调处理失败")
                    self._send_html(500, _error_html(str(exc)))
                    return
                self._send_html(200, _SUCCESS_HTML)

            def _send_html(self, status_code: int, body: str) -> None:
                payload = body.encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _send_text(self, status_code: int, body: str) -> None:
                payload = body.encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                logger.debug("Codex OAuth callback: " + format, *args)

        try:
            self._server = ThreadingHTTPServer((host, port), Handler)
        except OSError as exc:
            logger.warning("Codex OAuth 本地回调服务启动失败 host=%s port=%s err=%s", host, port, exc)
            return
        self._thread = threading.Thread(target=self._server.serve_forever, name="codex-oauth-callback", daemon=True)
        self._thread.start()
        logger.info("Codex OAuth 本地回调服务已启动 host=%s port=%s path=%s", host, port, callback_path)

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        server.shutdown()
        server.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=3)
        logger.info("Codex OAuth 本地回调服务已停止")


def _first(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    if not values:
        return None
    value = values[0]
    return value if value else None
