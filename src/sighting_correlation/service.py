"""多源目击记录关联与统一事件治理的领域用例。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Iterable, Mapping

from .clock import SystemClock, isoformat
from .contracts import SightingRecord, SightingValidationError
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .explain import build_version_view
from .jsonio import canonical_json, content_digest
from .linking import ALGORITHM_VERSION, evaluate_links
from .storage import initialize, transaction

ROLE_PERMISSIONS = {
    "observer": {"sighting.report"},
    "specialist": {
        "link.evaluate", "incident.confirm", "incident.split", "incident.read", "sighting.read"
    },
    "auditor": {"incident.read", "sighting.read", "audit.read"},
}


class SightingLinkService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    # ------------------------------------------------------------------ 用户

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    # -------------------------------------------------------------- 原始上报

    def report_sighting(
        self, actor_id: str, raw: Mapping[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        """登记一条不可变的目击上报；重复提交稳定返回既有结果。"""

        self._require(actor_id, "sighting.report")
        if not idempotency_key.strip():
            raise ValidationFailed("缺少 Idempotency-Key")
        request_digest = content_digest([raw])
        scope = "sighting_report"
        existing = self._idempotent_response(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        try:
            record = SightingRecord.from_dict(raw)
        except SightingValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        if record.reported_by != actor_id:
            raise Forbidden("只能以自己的名义提交目击上报")
        self._user(record.reported_by)
        for user_id in record.visible_to:
            if self.connection.execute(
                "SELECT 1 FROM users WHERE user_id=?", (user_id,)
            ).fetchone() is None:
                raise ValidationFailed(f"可见范围包含未知用户: {user_id}")
        response = {
            "sighting_id": record.sighting_id,
            "content_sha256": request_digest,
            "status": "recorded",
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO sightings(sighting_id,content_sha256,payload_json,reported_by,"
                    "observer_kind,organization,sensitive_location,observed_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        record.sighting_id,
                        request_digest,
                        canonical_json(raw),
                        record.reported_by,
                        record.observer_kind,
                        record.organization,
                        int(record.sensitive_location),
                        record.observed_at,
                        self._now(),
                    ),
                )
                grantees = {record.reported_by, *record.visible_to}
                for user_id in sorted(grantees):
                    self.connection.execute(
                        "INSERT INTO sighting_visibility(sighting_id,user_id,granted_by,created_at) "
                        "VALUES(?,?,?,?)",
                        (record.sighting_id, user_id, record.reported_by, self._now()),
                    )
                self.connection.execute(
                    "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (scope, idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit(
                    "sighting",
                    record.sighting_id,
                    "sighting.reported",
                    actor_id,
                    {
                        "content_sha256": request_digest,
                        "sensitive_location": record.sensitive_location,
                        "observer_kind": record.observer_kind,
                        "visible_to": sorted(grantees),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("目击编号重复、授权用户不存在或幂等键并发冲突") from exc
        return response

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def _record_row(self, sighting_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM sightings WHERE sighting_id=?", (sighting_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"目击记录不存在: {sighting_id}")
        return row

    def _load_record(self, sighting_id: str) -> SightingRecord:
        return SightingRecord.from_dict(json.loads(self._record_row(sighting_id)["payload_json"]))

    def _can_see_exact(self, sighting_id: str, viewer_id: str) -> bool:
        """精确位置只对提交者本人及其显式授权对象开放，归并不扩大此范围。"""

        row = self.connection.execute(
            "SELECT 1 FROM sighting_visibility WHERE sighting_id=? AND user_id=?",
            (sighting_id, viewer_id),
        ).fetchone()
        return row is not None

    def _visibility_map(
        self, sighting_ids: Iterable[str], viewer_id: str
    ) -> dict[str, bool]:
        return {
            sighting_id: self._can_see_exact(sighting_id, viewer_id)
            for sighting_id in sighting_ids
        }

    def get_sighting(self, viewer_id: str, sighting_id: str) -> dict[str, Any]:
        self._user(viewer_id)
        record = self._load_record(sighting_id)
        from .explain import project_sighting

        return project_sighting(record, self._can_see_exact(sighting_id, viewer_id))

    def list_sightings(self, viewer_id: str) -> dict[str, Any]:
        # 任何在册用户都可以浏览上报目录，但能看到什么几何由可见范围决定。
        self._user(viewer_id)
        rows = self.connection.execute(
            "SELECT sighting_id FROM sightings ORDER BY sighting_id"
        ).fetchall()
        records = {row["sighting_id"]: self._load_record(row["sighting_id"]) for row in rows}
        visibility = self._visibility_map(records, viewer_id)
        from .explain import project_sighting

        return {
            "sightings": [
                project_sighting(records[sighting_id], visibility[sighting_id])
                for sighting_id in sorted(records)
            ]
        }

    # ------------------------------------------------------------ 关联版本评估

    def _version_row(self, version_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM link_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"关联版本不存在: {version_id}")
        return row

    def evaluate_links(
        self, actor_id: str, sighting_ids: Iterable[str] | None = None
    ) -> dict[str, Any]:
        """对给定（默认全部）记录评估关联；结果按内容寻址，重复评估稳定复用。"""

        self._require(actor_id, "link.evaluate")
        if sighting_ids is None:
            rows = self.connection.execute(
                "SELECT sighting_id FROM sightings ORDER BY sighting_id"
            ).fetchall()
            selected = [row["sighting_id"] for row in rows]
        else:
            selected = sorted(set(sighting_ids))
        if len(selected) < 2:
            raise ValidationFailed("至少需要两条目击记录才能评估关联")
        records = {sighting_id: self._load_record(sighting_id) for sighting_id in selected}

        result = evaluate_links(records[sighting_id] for sighting_id in selected)
        fingerprint_input = {
            "algorithm_version": ALGORITHM_VERSION,
            "records": [
                [sighting_id, self._record_row(sighting_id)["content_sha256"]]
                for sighting_id in selected
            ],
        }
        input_digest = content_digest([fingerprint_input])

        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT version_id FROM link_versions WHERE algorithm_version=? AND input_sha256=?",
                (ALGORITHM_VERSION, input_digest),
            ).fetchone()
            if existing is None:
                cursor = self.connection.execute(
                    "INSERT INTO link_versions(algorithm_version,input_sha256,record_ids_json,"
                    "result_json,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        ALGORITHM_VERSION,
                        input_digest,
                        canonical_json(selected),
                        canonical_json(result),
                        actor_id,
                        self._now(),
                    ),
                )
                version_id = cursor.lastrowid
                self._audit(
                    "link_version",
                    str(version_id),
                    "link_version.created",
                    actor_id,
                    {"input_sha256": input_digest, "record_count": len(selected)},
                )
                reused = False
            else:
                version_id = existing["version_id"]
                reused = True
        envelope = self.get_version(actor_id, version_id)
        envelope["reused"] = reused
        envelope["input_sha256"] = input_digest
        return envelope

    def get_version(self, viewer_id: str, version_id: int) -> dict[str, Any]:
        self._user(viewer_id)
        row = self._version_row(version_id)
        result = json.loads(row["result_json"])
        records = {
            sighting_id: self._load_record(sighting_id) for sighting_id in result["record_ids"]
        }
        visibility = self._visibility_map(result["record_ids"], viewer_id)
        view = build_version_view(result, records, visibility)
        return {
            "version_id": version_id,
            "input_sha256": row["input_sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            **view,
        }

    def exclusion_report(self, viewer_id: str, sighting_id: str, version_id: int | None = None) -> dict[str, Any]:
        """给出某条记录在关联版本中被排除在候选之外的具体理由。"""

        self._user(viewer_id)
        self._record_row(sighting_id)
        if version_id is None:
            row = self.connection.execute(
                "SELECT version_id FROM link_versions ORDER BY version_id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise NotFound("尚不存在关联版本")
            version_id = row["version_id"]
        version = self.get_version(viewer_id, version_id)
        for item in version["ungrouped"]:
            if item["sighting"]["sighting_id"] == sighting_id:
                return {
                    "version_id": version_id,
                    "sighting_id": sighting_id,
                    "grouped": False,
                    "excluded_against": item["excluded_against"],
                }
        for group in version["groups"]:
            if sighting_id in group["members"]:
                return {
                    "version_id": version_id,
                    "sighting_id": sighting_id,
                    "grouped": True,
                    "group_index": group["group_index"],
                }
        raise NotFound("该记录不在此关联版本的评估范围内")

    # -------------------------------------------------------------- 统一事件

    def confirm_incident(
        self,
        actor_id: str,
        version_id: int,
        group_index: int,
        reason: str,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        """把一个候选组确认为统一事件；重复确认稳定返回既有事件。"""

        self._require(actor_id, "incident.confirm")
        if not reason.strip():
            raise ValidationFailed("确认理由不能为空")
        version_row = self._version_row(version_id)
        result = json.loads(version_row["result_json"])
        group = next(
            (candidate for candidate in result["groups"] if candidate["group_index"] == group_index),
            None,
        )
        if group is None:
            raise NotFound("该关联版本中没有这个候选组")
        members = list(group["members"])
        members_digest = content_digest([members])

        with transaction(self.connection, immediate=True):
            replay = self.connection.execute(
                "SELECT incident_id FROM incident_confirm_keys "
                "WHERE link_version_id=? AND members_digest=? AND revoked_at IS NULL",
                (version_id, members_digest),
            ).fetchone()
            if replay is not None:
                existing_id = replay["incident_id"]
                incident_row = self.connection.execute(
                    "SELECT status FROM incidents WHERE incident_id=?", (existing_id,)
                ).fetchone()
                if incident_row["status"] == "split":
                    raise InvalidState(
                        "该候选组曾被确认为事件 {}，但已被拆回；请在纳入新证据后的新关联版本上重新确认".format(existing_id)
                    )
                self._audit(
                    "incident", existing_id, "incident.confirm_replayed", actor_id,
                    {"version_id": version_id, "members": members},
                )
                return self.get_incident(actor_id, existing_id) | {"replayed": True}

            active_rows = self.connection.execute(
                "SELECT sighting_id,incident_id FROM incident_members WHERE active=1 AND sighting_id IN ({})".format(
                    ",".join("?" for _ in members)
                ),
                members,
            ).fetchall()
            if active_rows:
                blockers = ", ".join(
                    f"{row['sighting_id']}@{row['incident_id']}" for row in active_rows
                )
                raise Conflict(f"以下记录已属于存续中的统一事件: {blockers}")

            new_incident_id = incident_id or f"inc-{uuid.uuid4().hex[:12]}"
            now = self._now()
            self.connection.execute(
                "INSERT INTO incidents(incident_id,initial_version_id,latest_version_id,status,"
                "created_by,created_at) VALUES(?,?,?, 'active', ?,?)",
                (new_incident_id, version_id, version_id, actor_id, now),
            )
            for sighting_id in members:
                self.connection.execute(
                    "INSERT INTO incident_members(incident_id,sighting_id,link_version_id,"
                    "confirmed_by,confirmed_at,active) VALUES(?,?,?,?,?,1)",
                    (new_incident_id, sighting_id, version_id, actor_id, now),
                )
            self.connection.execute(
                "INSERT INTO incident_decisions(incident_id,kind,link_version_id,members_before_json,"
                "members_after_json,reason,decided_by,decided_at) VALUES(?, 'confirm', ?,?, ?,?,?,?)",
                (
                    new_incident_id,
                    version_id,
                    canonical_json([]),
                    canonical_json(members),
                    reason.strip(),
                    actor_id,
                    now,
                ),
            )
            self.connection.execute(
                "INSERT INTO incident_confirm_keys(link_version_id,members_digest,incident_id,created_at,revoked_at) "
                "VALUES(?,?,?,?,NULL) ON CONFLICT(link_version_id, members_digest) DO UPDATE SET "
                "incident_id=excluded.incident_id, created_at=excluded.created_at, revoked_at=NULL",
                (version_id, members_digest, new_incident_id, now),
            )
            self._audit(
                "incident",
                new_incident_id,
                "incident.confirmed",
                actor_id,
                {"version_id": version_id, "group_index": group_index, "members": members, "reason": reason.strip()},
            )
        return self.get_incident(actor_id, new_incident_id) | {"replayed": False}

    def split_incident(
        self,
        actor_id: str,
        incident_id: str,
        reason: str,
        remove_sighting_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """推翻既有归并：全部拆回则事件终结；部分拆回则事件保留剩余来源。"""

        self._require(actor_id, "incident.split")
        if not reason.strip():
            raise ValidationFailed("拆回理由不能为空")
        incident = self.connection.execute(
            "SELECT * FROM incidents WHERE incident_id=?", (incident_id,)
        ).fetchone()
        if incident is None:
            raise NotFound("统一事件不存在")
        if incident["status"] != "active":
            raise InvalidState("统一事件已经拆回终结")

        current_rows = self.connection.execute(
            "SELECT sighting_id FROM incident_members WHERE incident_id=? AND active=1 ORDER BY sighting_id",
            (incident_id,),
        ).fetchall()
        current = [row["sighting_id"] for row in current_rows]
        requested = sorted(set(remove_sighting_ids or []))
        unknown = [sighting_id for sighting_id in requested if sighting_id not in current]
        if unknown:
            raise ValidationFailed(f"以下记录当前不在事件中: {', '.join(unknown)}")
        removed = requested if requested else list(current)
        remaining = [sighting_id for sighting_id in current if sighting_id not in set(removed)]

        with transaction(self.connection, immediate=True):
            now = self._now()
            self.connection.execute(
                "UPDATE incident_members SET active=0 WHERE incident_id=? AND active=1 AND sighting_id IN ({})".format(
                    ",".join("?" for _ in removed)
                ),
                [incident_id, *removed],
            )
            # 保留成员的成员行原样维持 active=1，不重复插入。
            if not remaining:
                self.connection.execute(
                    "UPDATE incidents SET status='split',split_at=?,split_by=? WHERE incident_id=?",
                    (now, actor_id, incident_id),
                )
                self.connection.execute(
                    "UPDATE incident_confirm_keys SET revoked_at=? WHERE incident_id=? AND revoked_at IS NULL",
                    (now, incident_id),
                )
            self.connection.execute(
                "INSERT INTO incident_decisions(incident_id,kind,link_version_id,members_before_json,"
                "members_after_json,reason,decided_by,decided_at) VALUES(?, 'split', ?,?, ?,?,?,?)",
                (
                    incident_id,
                    incident["latest_version_id"],
                    canonical_json(current),
                    canonical_json(remaining),
                    reason.strip(),
                    actor_id,
                    now,
                ),
            )
            self._audit(
                "incident",
                incident_id,
                "incident.split" if not remaining else "incident.member_removed",
                actor_id,
                {"removed": removed, "remaining": remaining, "reason": reason.strip()},
            )
        return self.get_incident(actor_id, incident_id)

    def get_incident(self, viewer_id: str, incident_id: str) -> dict[str, Any]:
        """沿统一事件查看全部来源、历次决定与关联版本；按可见范围投影敏感位置。"""

        self._require(viewer_id, "incident.read")
        incident = self.connection.execute(
            "SELECT * FROM incidents WHERE incident_id=?", (incident_id,)
        ).fetchone()
        if incident is None:
            raise NotFound("统一事件不存在")

        member_rows = self.connection.execute(
            "SELECT sighting_id,active,confirmed_by,confirmed_at,link_version_id "
            "FROM incident_members WHERE incident_id=? ORDER BY sighting_id, rowid",
            (incident_id,),
        ).fetchall()
        all_sighting_ids = sorted({row["sighting_id"] for row in member_rows})
        records = {sighting_id: self._load_record(sighting_id) for sighting_id in all_sighting_ids}
        visibility = self._visibility_map(all_sighting_ids, viewer_id)

        from .explain import project_sighting

        active_ids = [row["sighting_id"] for row in member_rows if row["active"]]
        removed_ids = sorted(set(all_sighting_ids) - set(active_ids))
        decision_rows = self.connection.execute(
            "SELECT decision_id,kind,link_version_id,members_before_json,members_after_json,"
            "reason,decided_by,decided_at FROM incident_decisions WHERE incident_id=? ORDER BY decision_id",
            (incident_id,),
        ).fetchall()
        version_ids = sorted(
            {incident["initial_version_id"], incident["latest_version_id"]}
            | {row["link_version_id"] for row in decision_rows}
        )
        versions = [
            {
                "version_id": version_id,
                "summary": self._version_summary(viewer_id, version_id),
            }
            for version_id in version_ids
        ]

        def source_view(sighting_id: str, active: bool, status: str) -> dict[str, Any]:
            return {
                "status": status,
                "membership_active": active,
                "sighting": project_sighting(records[sighting_id], visibility[sighting_id]),
            }

        return {
            "incident_id": incident_id,
            "status": incident["status"],
            "initial_version_id": incident["initial_version_id"],
            "latest_version_id": incident["latest_version_id"],
            "created_by": incident["created_by"],
            "created_at": incident["created_at"],
            "split_by": incident["split_by"],
            "split_at": incident["split_at"],
            "active_sighting_ids": active_ids,
            "removed_sighting_ids": removed_ids,
            "sources": [
                source_view(sighting_id, sighting_id in set(active_ids), "active" if sighting_id in set(active_ids) else "removed")
                for sighting_id in all_sighting_ids
            ],
            "decisions": [
                {
                    "decision_id": row["decision_id"],
                    "kind": row["kind"],
                    "link_version_id": row["link_version_id"],
                    "members_before": json.loads(row["members_before_json"]),
                    "members_after": json.loads(row["members_after_json"]),
                    "reason": row["reason"],
                    "decided_by": row["decided_by"],
                    "decided_at": row["decided_at"],
                }
                for row in decision_rows
            ],
            "link_versions": versions,
        }

    def _version_summary(self, viewer_id: str, version_id: int) -> dict[str, Any]:
        row = self._version_row(version_id)
        result = json.loads(row["result_json"])
        return {
            "version_id": version_id,
            "input_sha256": row["input_sha256"],
            "record_count": len(result["record_ids"]),
            "group_count": len(result["groups"]),
            "created_at": row["created_at"],
        }

    def list_incidents(self, viewer_id: str, status: str | None = None) -> dict[str, Any]:
        self._require(viewer_id, "incident.read")
        if status is not None and status not in {"active", "split"}:
            raise ValidationFailed("status 必须是 active 或 split")
        sql = "SELECT incident_id,status,created_at FROM incidents"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY incident_id"
        rows = self.connection.execute(sql, params).fetchall()
        return {"incidents": [dict(row) for row in rows]}

    def list_audit_events(self, viewer_id: str, entity_type: str | None = None) -> dict[str, Any]:
        self._require(viewer_id, "audit.read")
        if entity_type:
            rows = self.connection.execute(
                "SELECT event_id,entity_type,entity_id,event_type,actor_id,payload_json,created_at "
                "FROM audit_events WHERE entity_type=? ORDER BY event_id",
                (entity_type,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT event_id,entity_type,entity_id,event_type,actor_id,payload_json,created_at "
                "FROM audit_events ORDER BY event_id"
            ).fetchall()
        return {
            "events": [
                dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows
            ]
        }
