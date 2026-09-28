"""无第三方依赖的 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ServiceError, ValidationFailed
from .service import DEFAULT_SCOPE, SightingLinkageService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Any


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(self, service: SightingLinkageService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = lambda: self._actor(normalized_headers)  # noqa: E731

            if method == "POST" and path == "/users":
                result = self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                )
                return Response(201, result)
            if method == "POST" and path == "/sight_records":
                return Response(201, self.service.submit_record(actor(), payload))
            if method == "GET" and len(parts) == 3 and parts[0] == "sight_records" and parts[2] == "explain":
                scope = normalized_headers.get("x-linkage-scope", DEFAULT_SCOPE)
                return Response(200, self.service.explain_record(actor(), parts[1], scope=scope))
            if method == "GET" and len(parts) == 2 and parts[0] == "sight_records":
                return Response(200, self.service.get_record(actor(), parts[1]))
            if method == "POST" and path == "/linkage/runs":
                scope = str(payload.get("scope", DEFAULT_SCOPE))
                result = self.service.suggest_linkage(
                    actor(), payload.get("record_ids"), scope=scope
                )
                return Response(200 if result.get("created") is False else 201, result)
            if method == "GET" and len(parts) == 3 and parts[0] == "linkage" and parts[1] == "runs":
                return Response(200, self.service.run_view(int(parts[2]), actor=actor()))
            if method == "POST" and path == "/incidents/confirm":
                result = self.service.confirm_incident(
                    actor(), int(payload["run_id"]), list(payload["members"]),
                    str(payload.get("note", "")), payload.get("event_id"),
                )
                return Response(200 if result.get("replayed") else 201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "incidents" and parts[2] == "dissolve":
                evidence_run = payload.get("run_id")
                result = self.service.dissolve_incident(
                    actor(), parts[1], str(payload.get("reason", "")),
                    None if evidence_run is None else int(evidence_run),
                )
                return Response(200, result)
            if method == "GET" and path == "/incidents":
                return Response(200, {"incidents": self.service.list_incidents(actor())})
            if method == "GET" and len(parts) == 2 and parts[0] == "incidents":
                return Response(200, self.service.get_incident(actor(), parts[1]))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    import threading

    # 单个离线服务共享一个 SQLite 连接；用锁把并发 HTTP 请求串行化。
    server_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "SightingLinkage/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with server_lock:
                response = application.handle(self.command, self.path, dict(self.headers.items()), body)
                encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动多源野生动物目击关联归并 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("sighting_linkage.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database, cross_thread=True)
    application = JsonApplication(SightingLinkageService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
