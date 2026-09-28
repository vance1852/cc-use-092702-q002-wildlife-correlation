"""无第三方依赖的 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import ServiceError, ValidationFailed
from .service import SightingLinkService
from .storage import connect, initialize


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(
        self,
        service: SightingLinkService | None = None,
        *,
        connector: Callable[[], SightingLinkService] | None = None,
    ) -> None:
        # 单元测试直接传入同线程共享的服务；真实多线程服务器传入连接工厂，
        # 每个工作线程使用自己的 SQLite 连接。
        self._service = service
        self._connector = connector
        self._local = threading.local()

    def service_for_thread(self) -> SightingLinkService:
        if self._service is not None:
            return self._service
        cached = getattr(self._local, "service", None)
        if cached is None:
            assert self._connector is not None
            cached = self._connector()
            self._local.service = cached
        return cached

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
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = lambda: self._actor(normalized_headers)

            if method == "POST" and path == "/users":
                result = self.service_for_thread().create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                )
                return Response(201, result)

            if method == "POST" and path == "/sightings":
                key = normalized_headers.get("idempotency-key", "").strip()
                if not key:
                    raise ValidationFailed("缺少 Idempotency-Key")
                result = self.service_for_thread().report_sighting(actor(), payload, key)
                return Response(201, result)
            if method == "GET" and path == "/sightings":
                return Response(200, self.service_for_thread().list_sightings(actor()))
            if method == "GET" and len(parts) == 2 and parts[0] == "sightings":
                return Response(200, self.service_for_thread().get_sighting(actor(), parts[1]))

            if method == "POST" and path == "/link_versions":
                ids = payload.get("sighting_ids")
                result = self.service_for_thread().evaluate_links(actor(), ids)
                return Response(201, result)
            if method == "GET" and len(parts) == 2 and parts[0] == "link_versions":
                return Response(200, self.service_for_thread().get_version(actor(), int(parts[1])))
            if method == "GET" and len(parts) == 4 and parts[0] == "sightings" and parts[2] == "exclusion":
                version_id = int(parts[3]) if parts[3] != "latest" else None
                return Response(
                    200, self.service_for_thread().exclusion_report(actor(), parts[1], version_id)
                )

            if method == "POST" and path == "/incidents":
                result = self.service_for_thread().confirm_incident(
                    actor(),
                    int(payload["link_version_id"]),
                    int(payload["group_index"]),
                    payload["reason"],
                    payload.get("incident_id"),
                )
                return Response(201, result)
            if method == "GET" and path == "/incidents":
                result = self.service_for_thread().list_incidents(actor(), query.get("status"))
                return Response(200, result)
            if method == "GET" and len(parts) == 2 and parts[0] == "incidents":
                return Response(200, self.service_for_thread().get_incident(actor(), parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "incidents" and parts[2] == "split":
                result = self.service_for_thread().split_incident(
                    actor(),
                    parts[1],
                    payload["reason"],
                    payload.get("remove_sighting_ids"),
                )
                return Response(200, result)

            if method == "GET" and path == "/audit_events":
                return Response(
                    200, self.service_for_thread().list_audit_events(actor(), query.get("entity_type"))
                )

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SightingCorrelation/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
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
    parser = argparse.ArgumentParser(description="启动多源目击记录关联归并 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("sighting_correlation.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    # 启动时在主连接上初始化一次结构；工作线程之后各开各的连接。
    bootstrap = connect(args.database)
    initialize(bootstrap)
    bootstrap.close()

    def connector() -> SightingLinkService:
        return SightingLinkService(connect(args.database))

    application = JsonApplication(connector=connector)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
