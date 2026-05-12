from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from agent_runtime.frontend_debug.frontend_debug_session import FrontendDebugSession

logger = logging.getLogger(__name__)

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

_RESERVED_ELEMENT_SELECTED_PATH = "/__agent_frontend_debug__/element-selected"


class ThreadedFrontendDebugProxyManager:
    """基于本机线程 HTTP Server 的前端调试代理管理器。

    每个前端调试会话独占一个绑定到 127.0.0.1 的随机端口，因此浏览器地址栏可以保持与
    原始前端页面相同的 path/query，避免 React Router/Vue Router 因代理前缀改变 location.pathname。
    """

    def __init__(self) -> None:
        # session_id → 已启动的本机代理实例。
        self._proxies: dict[str, _FrontendDebugProxyServer] = {}
        self._lock = threading.RLock()

    def start_session_proxy(
        self,
        *,
        session_id: str,
        target_url: str,
        on_element_selected: Callable[[str, dict[str, Any]], None],
    ) -> str:
        """为调试会话启动本机代理，并返回代理基础地址。"""
        with self._lock:
            existing = self._proxies.get(session_id)
            if existing is not None:
                return existing.proxy_base_url
            proxy = _FrontendDebugProxyServer(
                session_id=session_id,
                target_url=target_url,
                on_element_selected=on_element_selected,
            )
            proxy.start()
            self._proxies[session_id] = proxy
            return proxy.proxy_base_url


class HttpFrontendDebugElementSelectionPublisher:
    """通过 AgentRuntime 自身通信 REST API 发布前端元素选择事件。

    调试代理运行在独立线程中，直接操作 CommunicationGateway 的 asyncio 消息总线会有跨线程/跨事件循环风险。
    因此这里通过本机 HTTP POST 回到 FastAPI 服务，由原有通信接口在正确事件循环中发布 SSE 事件。
    """

    def publish_element_selected(
        self,
        *,
        session: FrontendDebugSession,
        payload: dict[str, Any],
    ) -> None:
        """发布 frontend.debug.element.selected 事件到调试会话绑定的通信流。"""
        if not session.communication_stream_id:
            raise ValueError("前端调试会话缺少 communication_stream_id，无法回填聊天输入框。")
        api_base_url = _runtime_api_base_url()
        stream_id = urllib.parse.quote(session.communication_stream_id, safe="")
        url = f"{api_base_url}/api/communication/streams/{stream_id}/messages"
        body = json.dumps(
            {
                "message_type": "frontend.debug.element.selected",
                "payload": payload,
                "topic": "agent.scene",
                "source": "frontend_debug_proxy",
                "actor_id": session.agent_instance_id,
                "correlation_id": session.request_id,
                "metadata": {
                    "task_id": session.task_id,
                    "agent_instance_id": session.agent_instance_id,
                    "trace_id": session.trace_id,
                    "frontend_debug_session_id": session.session_id,
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read()


class _FrontendDebugProxyServer:
    """单个前端调试会话的本机代理服务。"""

    def __init__(
        self,
        *,
        session_id: str,
        target_url: str,
        on_element_selected: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.session_id = session_id
        self.target_url = target_url
        self.target_parts = urlsplit(target_url)
        self.target_origin = urlunsplit((self.target_parts.scheme, self.target_parts.netloc, "", "", ""))
        self.target_route = _target_route_from_parts(self.target_parts)
        self.on_element_selected = on_element_selected
        handler = _build_request_handler(self)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name=f"frontend-debug-proxy-{session_id}",
            daemon=True,
        )

    @property
    def proxy_base_url(self) -> str:
        """当前代理服务的本机基础地址。"""
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        """启动代理服务线程。"""
        self._thread.start()
        logger.info("前端调试代理已启动: session=%s, proxy=%s, target=%s", self.session_id, self.proxy_base_url, self.target_url)

    def stop(self) -> None:
        """停止代理服务。"""
        self._httpd.shutdown()
        self._httpd.server_close()


def _build_request_handler(proxy: _FrontendDebugProxyServer) -> type[BaseHTTPRequestHandler]:
    """为单个调试代理创建绑定会话上下文的 HTTP handler。"""

    class FrontendDebugProxyRequestHandler(BaseHTTPRequestHandler):
        server_version = "AgentFrontendDebugProxy/0.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.debug("前端调试代理: " + format, *args)

        def do_GET(self) -> None:
            self._proxy_request("GET")

        def do_HEAD(self) -> None:
            self._proxy_request("HEAD")

        def do_OPTIONS(self) -> None:
            self._proxy_request("OPTIONS")

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] == _RESERVED_ELEMENT_SELECTED_PATH:
                self._handle_element_selected()
                return
            self._proxy_request("POST")

        def do_PUT(self) -> None:
            self._proxy_request("PUT")

        def do_PATCH(self) -> None:
            self._proxy_request("PATCH")

        def do_DELETE(self) -> None:
            self._proxy_request("DELETE")

        def _handle_element_selected(self) -> None:
            """接收注入脚本发送的 DOM 元素信息，并转交领域服务发布到聊天流。"""
            try:
                content_length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
                payload = json.loads(raw_body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("元素选择载荷必须是 JSON object。")
                proxy.on_element_selected(proxy.session_id, payload)
                self._send_json(200, {"status": "ok"})
            except Exception as exc:  # pragma: no cover - 代理线程错误需要返回给浏览器
                logger.exception("前端元素选择事件处理失败: session=%s", proxy.session_id)
                self._send_json(500, {"status": "error", "message": str(exc)})

        def _proxy_request(self, method: str) -> None:
            """转发浏览器请求到原始前端目标地址，并对 HTML 响应注入调试脚本。"""
            upstream_url = ""
            started_at = time.perf_counter()
            try:
                if method in {"GET", "HEAD"} and _should_redirect_proxy_root(self.path, proxy.target_route):
                    logger.info(
                        "前端调试代理根路径跳转: session=%s, method=%s, path=%s, location=%s",
                        proxy.session_id,
                        method,
                        self.path,
                        proxy.target_route,
                    )
                    self.send_response(302)
                    self.send_header("Location", proxy.target_route)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                upstream_url = proxy.target_origin + self.path
                logger.info(
                    "前端调试代理收到请求: session=%s, method=%s, path=%s, upstream=%s",
                    proxy.session_id,
                    method,
                    self.path,
                    upstream_url,
                )
                body = self._read_body_if_present(method)
                headers = self._build_upstream_headers()
                request = urllib.request.Request(
                    upstream_url,
                    data=body,
                    headers=headers,
                    method=method,
                )
                try:
                    response = urllib.request.urlopen(request, timeout=30)
                    status = response.status
                    response_headers = response.headers
                    response_body = response.read() if method != "HEAD" else b""
                except urllib.error.HTTPError as http_error:
                    status = http_error.code
                    response_headers = http_error.headers
                    response_body = http_error.read() if method != "HEAD" else b""
                    logger.warning(
                        "前端调试代理上游返回错误: session=%s, method=%s, upstream=%s, status=%s, bytes=%s",
                        proxy.session_id,
                        method,
                        upstream_url,
                        status,
                        len(response_body),
                    )

                injected = False
                content_type = response_headers.get("Content-Type", "")
                if method == "GET" and _is_html_response(content_type):
                    response_body = _inject_debug_script(
                        response_body,
                        content_type,
                        session_id=proxy.session_id,
                    )
                    content_type = _ensure_utf8_text_html(content_type)
                    injected = True
                    logger.info(
                        "前端调试代理已注入调试脚本: session=%s, path=%s, upstream=%s",
                        proxy.session_id,
                        self.path,
                        upstream_url,
                    )

                elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                logger.info(
                    "前端调试代理返回响应: session=%s, method=%s, path=%s, upstream=%s, status=%s, content_type=%s, bytes=%s, injected=%s, elapsed_ms=%s",
                    proxy.session_id,
                    method,
                    self.path,
                    upstream_url,
                    status,
                    content_type,
                    len(response_body),
                    injected,
                    elapsed_ms,
                )
                self.send_response(status)
                self._send_response_headers(response_headers, len(response_body), content_type)
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(response_body)
            except Exception as exc:  # pragma: no cover - 代理线程错误需要返回给浏览器
                logger.exception(
                    "前端调试代理转发失败: session=%s, method=%s, path=%s, upstream=%s",
                    proxy.session_id,
                    method,
                    self.path,
                    upstream_url or "<not-built>",
                )
                self._send_json(502, {"status": "error", "message": str(exc)})

        def _read_body_if_present(self, method: str) -> bytes | None:
            if method not in {"POST", "PUT", "PATCH", "DELETE"}:
                return None
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            return self.rfile.read(content_length) if content_length > 0 else None

        def _build_upstream_headers(self) -> dict[str, str]:
            headers: dict[str, str] = {}
            for key, value in self.headers.items():
                lowered = key.lower()
                if lowered in _HOP_BY_HOP_HEADERS or lowered in {"host", "content-length"}:
                    continue
                if lowered == "accept-encoding":
                    continue
                if lowered == "origin":
                    headers[key] = proxy.target_origin
                    continue
                if lowered == "referer":
                    headers[key] = _rewrite_referer_to_target(value, proxy.proxy_base_url, proxy.target_origin)
                    continue
                headers[key] = value
            headers["Host"] = proxy.target_parts.netloc
            headers["Accept-Encoding"] = "identity"
            return headers

        def _send_response_headers(self, response_headers: Any, content_length: int, content_type: str) -> None:
            for key, value in response_headers.items():
                lowered = key.lower()
                if lowered in _HOP_BY_HOP_HEADERS or lowered in {"content-length", "content-encoding"}:
                    continue
                if lowered == "content-security-policy":
                    # 调试代理需要执行注入脚本；删除 CSP 响应头避免本地开发调试脚本被阻止。
                    continue
                if lowered == "location":
                    value = _rewrite_location_to_proxy(value, proxy.target_origin, proxy.proxy_base_url)
                if lowered == "content-type":
                    value = content_type
                self.send_header(key, value)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return FrontendDebugProxyRequestHandler


def _is_html_response(content_type: str) -> bool:
    return "text/html" in content_type.lower()


def _target_route_from_parts(parts: urllib.parse.SplitResult) -> str:
    """提取 target_url 中需要保留在代理地址上的 path/query/fragment。"""
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    fragment = f"#{parts.fragment}" if parts.fragment else ""
    return f"{path}{query}{fragment}"


def _should_redirect_proxy_root(request_path: str, target_route: str) -> bool:
    """判断浏览器是否误打开了代理根路径，若 target_route 非根路径则应跳转到完整调试路径。"""
    parsed_request = urllib.parse.urlsplit(request_path)
    request_is_root = parsed_request.path in {"", "/"} and not parsed_request.query
    target_is_root = target_route in {"", "/"}
    return request_is_root and not target_is_root


def _ensure_utf8_text_html(content_type: str) -> str:
    lowered = content_type.lower()
    if "charset=" in lowered:
        return content_type
    return "text/html; charset=utf-8"


def _inject_debug_script(body: bytes, content_type: str, *, session_id: str) -> bytes:
    """向 HTML 文档注入元素选择工具条脚本。"""
    charset = "utf-8"
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip() or "utf-8"
            break
    try:
        html = body.decode(charset, errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")
    script = _debug_script(session_id)
    lower_html = html.lower()
    body_index = lower_html.rfind("</body>")
    if body_index >= 0:
        html = html[:body_index] + script + html[body_index:]
    else:
        html += script
    return html.encode("utf-8")


def _debug_script(session_id: str) -> str:
    """生成注入到目标页面的 DOM 元素选择脚本。"""
    session_json = json.dumps(session_id, ensure_ascii=False)
    return f"""
<script data-agent-frontend-debug-ui="script">
(() => {{
  const SESSION_ID = {session_json};
  const ENDPOINT = "/__agent_frontend_debug__/element-selected";
  if (window.__agentFrontendDebugInstalled) return;
  window.__agentFrontendDebugInstalled = true;

  let selecting = false;
  let selectedElement = null;

  const overlay = document.createElement("div");
  overlay.setAttribute("data-agent-frontend-debug-ui", "overlay");
  Object.assign(overlay.style, {{
    position: "fixed",
    zIndex: "2147483646",
    pointerEvents: "none",
    border: "2px solid #2563eb",
    background: "rgba(37, 99, 235, 0.12)",
    borderRadius: "3px",
    display: "none",
    boxSizing: "border-box",
  }});
  document.documentElement.appendChild(overlay);

  const toolbar = document.createElement("div");
  toolbar.setAttribute("data-agent-frontend-debug-ui", "toolbar");
  Object.assign(toolbar.style, {{
    position: "fixed",
    top: "12px",
    right: "12px",
    zIndex: "2147483647",
    display: "flex",
    alignItems: "center",
    gap: "8px",
    padding: "8px",
    borderRadius: "10px",
    border: "1px solid rgba(15, 23, 42, 0.18)",
    background: "rgba(255, 255, 255, 0.96)",
    boxShadow: "0 12px 30px rgba(15, 23, 42, 0.18)",
    fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    fontSize: "12px",
    color: "#0f172a",
  }});

  const chooseButton = createButton("选择元素");
  const sendButton = createButton("发送");
  const statusText = document.createElement("span");
  statusText.textContent = "未选择";
  statusText.style.maxWidth = "220px";
  statusText.style.overflow = "hidden";
  statusText.style.textOverflow = "ellipsis";
  statusText.style.whiteSpace = "nowrap";
  sendButton.disabled = true;
  toolbar.append(chooseButton, sendButton, statusText);
  document.documentElement.appendChild(toolbar);

  chooseButton.addEventListener("click", (event) => {{
    event.preventDefault();
    event.stopPropagation();
    if (selecting) {{
      stopSelecting(true);
    }} else {{
      startSelecting();
    }}
  }});

  sendButton.addEventListener("click", async (event) => {{
    event.preventDefault();
    event.stopPropagation();
    if (!selectedElement) return;
    const payload = serializeElement(selectedElement);
    statusText.textContent = "发送中...";
    sendButton.disabled = true;
    try {{
      const response = await fetch(ENDPOINT, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(payload),
      }});
      if (!response.ok) throw new Error(await response.text());
      statusText.textContent = "已发送到聊天输入框";
    }} catch (error) {{
      console.error("[frontend-debug] send selected element failed", error);
      statusText.textContent = "发送失败，查看控制台";
      sendButton.disabled = false;
    }}
  }});

  function createButton(text) {{
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    Object.assign(button.style, {{
      border: "1px solid #cbd5e1",
      borderRadius: "7px",
      padding: "5px 9px",
      background: "#ffffff",
      color: "#0f172a",
      cursor: "pointer",
      fontSize: "12px",
      lineHeight: "16px",
    }});
    button.addEventListener("mouseenter", () => {{ if (!button.disabled) button.style.background = "#f1f5f9"; }});
    button.addEventListener("mouseleave", () => {{ button.style.background = "#ffffff"; }});
    return button;
  }}

  function startSelecting() {{
    selecting = true;
    selectedElement = null;
    sendButton.disabled = true;
    chooseButton.textContent = "取消选择";
    statusText.textContent = "移动鼠标选择元素";
    document.addEventListener("mousemove", handleMouseMove, true);
    document.addEventListener("click", handleElementClick, true);
  }}

  function stopSelecting(clearSelection) {{
    selecting = false;
    chooseButton.textContent = "选择元素";
    document.removeEventListener("mousemove", handleMouseMove, true);
    document.removeEventListener("click", handleElementClick, true);
    if (clearSelection) {{
      selectedElement = null;
      sendButton.disabled = true;
      statusText.textContent = "已取消";
      hideOverlay();
    }}
  }}

  function handleMouseMove(event) {{
    if (!selecting) return;
    const element = findSelectableElement(event.target);
    if (!element) return;
    showOverlay(element);
    statusText.textContent = describeElement(element);
  }}

  function handleElementClick(event) {{
    if (!selecting) return;
    const element = findSelectableElement(event.target);
    if (!element) return;
    event.preventDefault();
    event.stopPropagation();
    selectedElement = element;
    showOverlay(element);
    stopSelecting(false);
    statusText.textContent = `已选择 ${{describeElement(element)}}`;
    sendButton.disabled = false;
  }}

  function findSelectableElement(candidate) {{
    if (!(candidate instanceof Element)) return null;
    if (candidate.closest("[data-agent-frontend-debug-ui]")) return null;
    return candidate;
  }}

  function showOverlay(element) {{
    const rect = element.getBoundingClientRect();
    Object.assign(overlay.style, {{
      display: "block",
      left: `${{Math.max(rect.left, 0)}}px`,
      top: `${{Math.max(rect.top, 0)}}px`,
      width: `${{Math.max(rect.width, 0)}}px`,
      height: `${{Math.max(rect.height, 0)}}px`,
    }});
  }}

  function hideOverlay() {{
    overlay.style.display = "none";
  }}

  function serializeElement(element) {{
    const rect = element.getBoundingClientRect();
    const attributes = {{}};
    for (const attr of Array.from(element.attributes || []).slice(0, 30)) {{
      attributes[attr.name] = limit(attr.value, 1000);
    }}
    return {{
      session_id: SESSION_ID,
      page_url: location.href,
      document_title: document.title,
      selector: buildCssSelector(element),
      xpath: buildXPath(element),
      tag_name: element.tagName.toLowerCase(),
      id: element.id || null,
      class_name: element.className && typeof element.className === "string" ? element.className : null,
      text: limit((element.innerText || element.textContent || "").trim(), 2000),
      outer_html: limit(element.outerHTML || "", 8000),
      attributes,
      bounding_rect: {{
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        top: Math.round(rect.top),
        left: Math.round(rect.left),
        bottom: Math.round(rect.bottom),
        right: Math.round(rect.right),
      }},
      viewport: {{ width: window.innerWidth, height: window.innerHeight }},
    }};
  }}

  function describeElement(element) {{
    const id = element.id ? `#${{element.id}}` : "";
    const cls = typeof element.className === "string" && element.className.trim()
      ? "." + element.className.trim().split(/\s+/).slice(0, 3).join(".")
      : "";
    return `${{element.tagName.toLowerCase()}}${{id}}${{cls}}`;
  }}

  function buildCssSelector(element) {{
    const escapeCss = (value) => window.CSS && typeof CSS.escape === "function"
      ? CSS.escape(value)
      : String(value).replace(/[^a-zA-Z0-9_-]/g, "\\\\$&");
    if (element.id) return `#${{escapeCss(element.id)}}`;
    const parts = [];
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.documentElement) {{
      let part = current.tagName.toLowerCase();
      const classList = Array.from(current.classList || []).slice(0, 3);
      if (classList.length) part += "." + classList.map((item) => escapeCss(item)).join(".");
      const parent = current.parentElement;
      if (parent) {{
        const sameTagSiblings = Array.from(parent.children).filter((child) => child.tagName === current.tagName);
        if (sameTagSiblings.length > 1) part += `:nth-of-type(${{sameTagSiblings.indexOf(current) + 1}})`;
      }}
      parts.unshift(part);
      current = parent;
      if (parts.length >= 6) break;
    }}
    return parts.join(" > ");
  }}

  function buildXPath(element) {{
    const parts = [];
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE) {{
      let index = 1;
      let sibling = current.previousElementSibling;
      while (sibling) {{
        if (sibling.tagName === current.tagName) index += 1;
        sibling = sibling.previousElementSibling;
      }}
      parts.unshift(`${{current.tagName.toLowerCase()}}[${{index}}]`);
      current = current.parentElement;
    }}
    return "/" + parts.join("/");
  }}

  function limit(value, maxLength) {{
    if (!value || value.length <= maxLength) return value || "";
    return value.slice(0, maxLength) + `...<truncated ${{value.length - maxLength}} chars>`;
  }}
}})();
</script>
"""


def _rewrite_referer_to_target(value: str, proxy_base_url: str, target_origin: str) -> str:
    if value.startswith(proxy_base_url):
        return target_origin + value[len(proxy_base_url):]
    return value


def _rewrite_location_to_proxy(value: str, target_origin: str, proxy_base_url: str) -> str:
    if value.startswith(target_origin):
        return proxy_base_url + value[len(target_origin):]
    return value


def _runtime_api_base_url() -> str:
    """读取当前 AgentRuntime REST API 本机访问地址。"""
    try:
        from agent_runtime.config import ConfigRegistry

        runtime_cfg = ConfigRegistry.instance().agent_runtime_service
        host = runtime_cfg.service_host
        if host in {"0.0.0.0", "::", ""}:
            host = "127.0.0.1"
        return f"http://{host}:{runtime_cfg.service_port}"
    except Exception:
        return "http://127.0.0.1:8000"
