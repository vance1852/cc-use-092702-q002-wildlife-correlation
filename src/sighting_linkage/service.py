"""多源野生动物目击关联归并的领域用例。

设计约束：
- 原始上报不可变，事件只引用记录编号；
- 关联结果按输入内容摘要幂等，重复运行返回同一 run；
- 事件版本只追加，重复确认返回既有版本，只有新记录（新输入）才能产生新版本；
- 坐标等敏感字段按每条记录自己的 visibility 脱敏，归并不会扩大披露范围。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .clock import SystemClock, isoformat
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .linkage import (
    ALGORITHM_VERSION,
    StoredRecord,
    build_pairwise,
    connected_components,
)
from .models import SightRecordInput
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    # 社区护林员 / 科研人员 / 巡护员：只能上报与查看自己的原始记录
    "reporter": {"sight_record.submit", "record.read_own"},
    # 生态专员：跑关联、确认或拆回统一事件、读取生态可见记录
    "specialist": {"linkage.run", "incident.write", "incident.read", "record.read_ecology"},
    # 调度室：读取事件并据此派遣，只见 dispatch 可见的位置
    "dispatcher": {"incident.read", "record.read_dispatch"},
    # 审计：读取历次决定与关联解释，位置仍按 visibility 脱敏
    "auditor": {"incident.read", "audit.read"},
}

DEFAULT_SCOPE = "default"

# visibility 由松到严；数值表示“坐标可见所需的最低披露档角色”。
_COORD_ROLE_TIER = {"ecology": ("specialist",), "dispatch": ("specialist", "dispatcher")}


class SightingLinkageService:
    """在单个 SQLite 连接上提供全部关联归并操作。"""

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

    def submit_record(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "sight_record.submit")
        try:
            item = SightRecordInput.from_dict(raw)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if item.observer_id != actor_id and self._user(actor_id)["role"] != "specialist":
            raise Forbidden("只能以自己的观察者身份上报；代录需由生态专员操作")
        raw_text = canonical_json(raw)
        digest = content_digest([raw])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO sight_records("
                    "record_id,observer_id,observer_role,observed_at,time_uncertainty_seconds,"
                    "valley,latitude,longitude,location_radius_meters,location_precision_text,"
                    "species_code,species_status,species_confidence,image_summary_json,"
                    "sensitive,visibility,raw_json,content_sha256,submitted_by,submitted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item.record_id, item.observer_id, item.observer_role, item.observed_at,
                        item.time_uncertainty_seconds, item.valley,
                        None if item.latitude is None else format(item.latitude, "f"),
                        None if item.longitude is None else format(item.longitude, "f"),
                        item.location_radius_meters, item.location_precision_text,
                        item.species_code, item.species_status, format(item.species_confidence, "f"),
                        None if item.image_summary is None else canonical_json(item.image_summary.as_dict()),
                        int(item.sensitive), item.visibility, raw_text, digest, actor_id, self._now(),
                    ),
                )
                self._audit("sight_record", item.record_id, "sight_record.submitted", actor_id, {
                    "sensitive": item.sensitive, "visibility": item.visibility, "sha256": digest,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"目击记录已存在或内容重复: {item.record_id}") from exc
        return {"record_id": item.record_id, "content_sha256": digest}

    def _load_record_row(self, record_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM sight_records WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound(f"目击记录不存在: {record_id}")
        return row

    def _can_view_coordinates(self, row: sqlite3.Row, viewer_id: str, viewer_role: str) -> bool:
        """坐标可见性只取决于该条记录自己的 visibility，与它是否归入事件无关。"""

        if viewer_id == row["submitted_by"] or viewer_id == row["observer_id"]:
            return True
        allowed = _COORD_ROLE_TIER.get(row["visibility"], ())
        return viewer_role in allowed

    def record_view(self, row: sqlite3.Row, viewer_id: str, viewer_role: str) -> dict[str, Any]:
        """按查看者身份对单条原始记录做字段级脱敏。"""

        coordinates_visible = self._can_view_coordinates(row, viewer_id, viewer_role)
        view: dict[str, Any] = {
            "record_id": row["record_id"],
            "observer_id": row["observer_id"],
            "observer_role": row["observer_role"],
            "observed_at": row["observed_at"],
            "time_uncertainty_seconds": row["time_uncertainty_seconds"],
            "valley": row["valley"],
            "location_precision_text": row["location_precision_text"],
            "species_code": row["species_code"],
            "species_status": row["species_status"],
            "species_confidence": row["species_confidence"],
            "image_summary": None if row["image_summary_json"] is None else json.loads(row["image_summary_json"]),
            "sensitive": bool(row["sensitive"]),
            "visibility": row["visibility"],
            "submitted_by": row["submitted_by"],
            "submitted_at": row["submitted_at"],
            "content_sha256": row["content_sha256"],
            "coordinates_visible": coordinates_visible,
        }
        if coordinates_visible and row["latitude"] is not None:
            view["latitude"] = row["latitude"]
            view["longitude"] = row["longitude"]
            view["location_radius_meters"] = row["location_radius_meters"]
        elif row["latitude"] is None:
            view["coordinates_status"] = "record_has_no_coordinates"
        else:
            view["coordinates_status"] = "redacted_by_visibility"
        # 原始 JSON 快照只对提交者本人开放；其他岗位看到脱敏视图，
        # 快照本身仍在存储层独立保留，可走专门审计通道调取。
        if viewer_id == row["submitted_by"]:
            view["raw_snapshot_available"] = True
            view["raw_json"] = json.loads(row["raw_json"])
        else:
            view["raw_snapshot_available"] = False
        return view

    def get_record(self, actor_id: str, record_id: str) -> dict[str, Any]:
        viewer = self._user(actor_id)
        row = self._load_record_row(record_id)
        is_own = row["submitted_by"] == actor_id or row["observer_id"] == actor_id
        if not is_own and viewer["role"] not in {"specialist", "dispatcher", "auditor"}:
            raise Forbidden("只能查看自己上报的记录")
        if not is_own and viewer["role"] == "dispatcher":
            self._require(actor_id, "record.read_dispatch")
        return self.record_view(row, actor_id, viewer["role"])

    # -------------------------------------------------------------- 关联运行

    @staticmethod
    def _stored_from_row(row: sqlite3.Row) -> StoredRecord:
        image = None if row["image_summary_json"] is None else json.loads(row["image_summary_json"])
        return StoredRecord(
            record_id=row["record_id"],
            observer_id=row["observer_id"],
            observer_role=row["observer_role"],
            observed_at=row["observed_at"],
            time_uncertainty_seconds=row["time_uncertainty_seconds"],
            valley=row["valley"],
            latitude=None if row["latitude"] is None else Decimal(row["latitude"]),
            longitude=None if row["longitude"] is None else Decimal(row["longitude"]),
            location_radius_meters=row["location_radius_meters"],
            location_precision_text=row["location_precision_text"],
            species_code=row["species_code"],
            species_status=row["species_status"],
            species_confidence=Decimal(row["species_confidence"]),
            image_class=None if image is None else image["image_class"],
            image_confidence=None if image is None else Decimal(str(image["confidence"])),
            image_tags=() if image is None else tuple(image["tags"]),
            sensitive=bool(row["sensitive"]),
        )

    def _snapshot_digest(self, rows: Sequence[sqlite3.Row]) -> str:
        snapshots = [
            {
                "record_id": row["record_id"],
                "observed_at": row["observed_at"],
                "time_uncertainty_seconds": row["time_uncertainty_seconds"],
                "valley": row["valley"],
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "location_radius_meters": row["location_radius_meters"],
                "species_code": row["species_code"],
                "species_status": row["species_status"],
                "species_confidence": row["species_confidence"],
                "image_summary_json": row["image_summary_json"],
                "observer_id": row["observer_id"],
                "observer_role": row["observer_role"],
                "content_sha256": row["content_sha256"],
            }
            for row in rows
        ]
        return content_digest([snapshots, ALGORITHM_VERSION])

    def suggest_linkage(
        self, actor_id: str, record_ids: Iterable[str] | None = None, *, scope: str = DEFAULT_SCOPE
    ) -> dict[str, Any]:
        """对一组（默认全部）记录计算可解释关联；同输入永远返回同一 run。"""

        self._require(actor_id, "linkage.run")
        ids = sorted(set(record_ids)) if record_ids is not None else None
        with transaction(self.connection, immediate=True):
            if ids is None:
                rows = list(self.connection.execute(
                    "SELECT * FROM sight_records ORDER BY record_id"
                ).fetchall())
            else:
                if not ids:
                    raise ValidationFailed("记录编号列表不能为空")
                placeholders = ",".join("?" for _ in ids)
                rows = list(self.connection.execute(
                    f"SELECT * FROM sight_records WHERE record_id IN ({placeholders}) ORDER BY record_id", ids
                ).fetchall())
                if len(rows) != len(ids):
                    found = {row["record_id"] for row in rows}
                    raise NotFound(f"目击记录不存在: {sorted(set(ids) - found)}")
            if len(rows) < 2:
                raise ValidationFailed("至少需要两条记录才能计算关联")

            input_digest = self._snapshot_digest(rows)
            existing = self.connection.execute(
                "SELECT run_id FROM linkage_runs WHERE scope=? AND input_sha256=?",
                (scope, input_digest),
            ).fetchone()
            if existing is not None:
                run_id = existing["run_id"]
                created = False
            else:
                records = [self._stored_from_row(row) for row in rows]
                pairs = build_pairwise(records)
                groups = connected_components(records, pairs)
                result = {
                    "record_ids": [row["record_id"] for row in rows],
                    "pairs": pairs,
                    "suggested_groups": groups,
                }
                cursor = self.connection.execute(
                    "INSERT INTO linkage_runs(scope,input_sha256,algorithm_version,result_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scope, input_digest, ALGORITHM_VERSION, canonical_json(result), actor_id, self._now()),
                )
                run_id = cursor.lastrowid
                for position, row in enumerate(rows):
                    self.connection.execute(
                        "INSERT INTO linkage_run_records(run_id,record_id,position) VALUES(?,?,?)",
                        (run_id, row["record_id"], position),
                    )
                self._audit("linkage_run", str(run_id), "linkage.computed", actor_id, {
                    "scope": scope, "record_count": len(rows), "input_sha256": input_digest,
                    "candidate_pairs": sum(1 for pair in pairs if pair["candidate"]),
                })
                created = True
        return self.run_view(run_id, actor=actor_id, created=created)

    def _load_run(self, run_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM linkage_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFound(f"关联运行不存在: {run_id}")
        return row

    def _redact_pair(
        self, pair: dict[str, Any], can_view: Mapping[str, bool]
    ) -> dict[str, Any]:
        """对无权查看至少一方坐标的查看者，去除可反推位置的距离数值。"""

        redacted = json.loads(canonical_json(pair))
        space = redacted["dimensions"]["space"]
        if space["detail"].get("basis") == "coordinates" and not (
            can_view[pair["record_a"]] and can_view[pair["record_b"]]
        ):
            overlap = space["detail"].get("circles_overlap")
            space["detail"] = {
                "basis": "coordinates",
                "circles_overlap": overlap,
                "distance_redacted": True,
            }
            verb = "相交" if overlap else "不相交"
            space["notes"] = [
                f"位置相容性已在服务端按双方完整坐标判定（不确定性圆{verb}）；"
                "当前查看者无权查看其中至少一方的精确坐标，距离数值不予披露"
            ]
        return redacted

    def run_view(self, run_id: int, *, actor: str | None = None, created: bool | None = None) -> dict[str, Any]:
        row = self._load_run(run_id)
        viewer_id = actor if actor is not None else row["created_by"]
        viewer = self._user(viewer_id)
        if viewer["role"] not in {"specialist", "dispatcher", "auditor"}:
            raise Forbidden("当前角色不能查看关联运行结果")
        result = json.loads(row["result_json"])
        can_view = {}
        for record_id in result["record_ids"]:
            record_row = self._load_record_row(record_id)
            can_view[record_id] = self._can_view_coordinates(record_row, viewer_id, viewer["role"])
        pairs = [self._redact_pair(pair, can_view) for pair in result["pairs"]]
        view = {
            "run_id": row["run_id"],
            "scope": row["scope"],
            "algorithm_version": row["algorithm_version"],
            "input_sha256": row["input_sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "record_ids": result["record_ids"],
            "candidate_pairs": [pair for pair in pairs if pair["candidate"]],
            "excluded_pairs": [pair for pair in pairs if not pair["candidate"]],
            "suggested_groups": result["suggested_groups"],
        }
        if created is not None:
            view["created"] = created
        return view

    def explain_record(self, actor_id: str, record_id: str, *, scope: str = DEFAULT_SCOPE) -> dict[str, Any]:
        """查看某条记录在最新关联运行中被纳入或排除的逐对理由。"""

        viewer = self._user(actor_id)
        record_row = self._load_record_row(record_id)
        own_record = record_row["submitted_by"] == actor_id or record_row["observer_id"] == actor_id
        if viewer["role"] == "reporter" and not own_record:
            raise Forbidden("只能查看与自己上报记录相关的关联解释")
        if viewer["role"] not in {"specialist", "auditor", "dispatcher", "reporter"}:
            raise Forbidden("当前角色不能查看关联解释")
        row = self.connection.execute(
            "SELECT r.run_id FROM linkage_runs r "
            "JOIN linkage_run_records m ON m.run_id=r.run_id "
            "WHERE r.scope=? AND m.record_id=? ORDER BY r.run_id DESC LIMIT 1",
            (scope, record_id),
        ).fetchone()
        if row is None:
            raise NotFound("该记录尚未参与任何关联计算")
        run_row = self._load_run(row["run_id"])
        result = json.loads(run_row["result_json"])
        can_view = {}
        for rid in result["record_ids"]:
            can_view[rid] = self._can_view_coordinates(self._load_record_row(rid), actor_id, viewer["role"])
        pairs = [self._redact_pair(pair, can_view) for pair in result["pairs"]]
        involving = [
            pair for pair in pairs
            if record_id in (pair["record_a"], pair["record_b"])
        ]
        return {
            "record_id": record_id,
            "run_id": run_row["run_id"],
            "candidate_links": [pair for pair in involving if pair["candidate"]],
            "excluded_from": [
                {
                    "other_record_id": pair["record_b"] if pair["record_a"] == record_id else pair["record_a"],
                    "score": pair["score"],
                    "exclusion_reasons": pair["exclusion_reasons"],
                }
                for pair in involving
                if not pair["candidate"]
            ],
        }

    # -------------------------------------------------------------- 统一事件

    def _validate_group(self, result: Mapping[str, Any], members: Sequence[str]) -> None:
        if len(members) < 2:
            raise ValidationFailed("统一事件至少需要两条记录")
        member_set = set(members)
        if not member_set.issubset(set(result["record_ids"])):
            raise ValidationFailed("成员记录不属于该关联运行")
        # 成员必须由候选边连通（允许 A~B、B~C 的传递式归并）。
        parent = {record_id: record_id for record_id in result["record_ids"]}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for pair in result["pairs"]:
            if pair["candidate"]:
                ra, rb = find(pair["record_a"]), find(pair["record_b"])
                if ra != rb:
                    parent[rb] = ra
        roots = {find(member) for member in members}
        if len(roots) != 1:
            raise InvalidState("所选记录之间没有候选关联连通，不能确认为同一事件")

    def confirm_incident(
        self,
        actor_id: str,
        run_id: int,
        members: Sequence[str],
        note: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """生态专员确认形成统一事件；重复确认稳定返回既有版本。"""

        self._require(actor_id, "incident.write")
        if not note.strip():
            raise ValidationFailed("确认意见不能为空")
        ordered_members = sorted(members)
        run_row = self._load_run(run_id)
        result = json.loads(run_row["result_json"])
        self._validate_group(result, ordered_members)
        with transaction(self.connection, immediate=True):
            target_event = event_id
            if target_event is None:
                # 同一 run 已经确认过事件：重复确认直接返回既有结果，不产生新版本。
                prior = self.connection.execute(
                    "SELECT event_id FROM incident_versions WHERE run_id=? ORDER BY version_no LIMIT 1",
                    (run_id,),
                ).fetchone()
                if prior is not None:
                    target_event = prior["event_id"]

            if target_event is None:
                target_event = f"EVT-{run_id}-{'-'.join(ordered_members)}"
                if self.connection.execute(
                    "SELECT 1 FROM incidents WHERE event_id=?", (target_event,)
                ).fetchone() is not None:
                    raise Conflict("派生事件编号已存在，请显式指定 event_id")
                self.connection.execute(
                    "INSERT INTO incidents(event_id,current_version,created_by,created_at) VALUES(?,?,?,?)",
                    (target_event, 1, actor_id, self._now()),
                )
                version_no = 1
            else:
                incident = self.connection.execute(
                    "SELECT * FROM incidents WHERE event_id=?", (target_event,)
                ).fetchone()
                if incident is None:
                    raise NotFound(f"统一事件不存在: {target_event}")
                same_run = self.connection.execute(
                    "SELECT * FROM incident_versions WHERE event_id=? AND run_id=?",
                    (target_event, run_id),
                ).fetchone()
                if same_run is not None:
                    # 幂等：同一 run 上重复确认只能拿回既有版本。
                    if json.loads(same_run["members_json"]) != ordered_members:
                        raise Conflict("该关联运行在此事件上已有确认，且成员名单不同")
                    self._audit("incident", target_event, "incident.confirm.replayed", actor_id, {
                        "run_id": run_id, "version_no": same_run["version_no"],
                    })
                    return {"event_id": target_event, "version_no": same_run["version_no"],
                            "state": same_run["state"], "replayed": True}
                version_no = incident["current_version"] + 1

            self.connection.execute(
                "INSERT INTO incident_versions(event_id,version_no,run_id,state,members_json,note,decided_by,decided_at) "
                "VALUES(?,?,?,'confirmed',?,?,?,?)",
                (target_event, version_no, run_id, canonical_json(ordered_members),
                 note.strip(), actor_id, self._now()),
            )
            self.connection.execute(
                "UPDATE incidents SET current_version=? WHERE event_id=?", (version_no, target_event)
            )
            self._audit("incident", target_event, "incident.confirmed", actor_id, {
                "run_id": run_id, "version_no": version_no, "members": ordered_members,
            })
        return {"event_id": target_event, "version_no": version_no, "state": "confirmed", "replayed": False}

    def dissolve_incident(
        self, actor_id: str, event_id: str, reason: str, run_id: int | None = None
    ) -> dict[str, Any]:
        """后续证据推翻判断时拆回：追加 dissolved 版本，成员与历次决定保留。

        run_id 可指向加入新记录后产生的关联运行；为空表示仅凭线下证据人工拆回。
        """

        self._require(actor_id, "incident.write")
        if not reason.strip():
            raise ValidationFailed("拆回理由不能为空")
        if run_id is not None:
            self._load_run(run_id)
        with transaction(self.connection, immediate=True):
            incident = self.connection.execute(
                "SELECT * FROM incidents WHERE event_id=?", (event_id,)
            ).fetchone()
            if incident is None:
                raise NotFound(f"统一事件不存在: {event_id}")
            current = self.connection.execute(
                "SELECT * FROM incident_versions WHERE event_id=? AND version_no=?",
                (event_id, incident["current_version"]),
            ).fetchone()
            if current["state"] == "dissolved":
                raise InvalidState("事件已经拆回，需要新的关联运行才能再次确认")
            if run_id is not None:
                prior = self.connection.execute(
                    "SELECT 1 FROM incident_versions WHERE event_id=? AND run_id=?",
                    (event_id, run_id),
                ).fetchone()
                if prior is not None:
                    raise InvalidState("该关联运行已在此事件上形成过决定")
            version_no = incident["current_version"] + 1
            self.connection.execute(
                "INSERT INTO incident_versions(event_id,version_no,run_id,state,members_json,note,decided_by,decided_at) "
                "VALUES(?,?,?,'dissolved',?,?,?,?)",
                (event_id, version_no, run_id, current["members_json"],
                 reason.strip(), actor_id, self._now()),
            )
            self.connection.execute(
                "UPDATE incidents SET current_version=? WHERE event_id=?", (version_no, event_id)
            )
            self._audit("incident", event_id, "incident.dissolved", actor_id, {
                "evidence_run_id": run_id, "version_no": version_no, "reason": reason.strip(),
            })
        return {"event_id": event_id, "version_no": version_no, "state": "dissolved"}

    def list_incidents(self, actor_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "incident.read")
        rows = self.connection.execute(
            "SELECT i.event_id,i.current_version,v.state,v.run_id,v.decided_at "
            "FROM incidents i JOIN incident_versions v "
            "ON v.event_id=i.event_id AND v.version_no=i.current_version "
            "ORDER BY i.event_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_incident(self, actor_id: str, event_id: str) -> dict[str, Any]:
        """沿统一事件查看全部来源版本、成员记录（脱敏）与未纳入记录的排除理由。"""

        viewer = self._require(actor_id, "incident.read")
        incident = self.connection.execute(
            "SELECT * FROM incidents WHERE event_id=?", (event_id,)
        ).fetchone()
        if incident is None:
            raise NotFound(f"统一事件不存在: {event_id}")
        version_rows = self.connection.execute(
            "SELECT * FROM incident_versions WHERE event_id=? ORDER BY version_no", (event_id,)
        ).fetchall()
        current = version_rows[-1]
        # dissolved 版本可能不引用关联运行（纯线下证据拆回），排除理由沿最近确认版本追溯。
        run_for_exclusions = current["run_id"]
        if run_for_exclusions is None:
            for previous in reversed(version_rows[:-1]):
                if previous["run_id"] is not None:
                    run_for_exclusions = previous["run_id"]
                    break
        current_result = json.loads(self._load_run(run_for_exclusions)["result_json"]) if run_for_exclusions else {"pairs": []}
        current_members = set(json.loads(current["members_json"]))

        # 沿全部历史版本收集“曾经作为来源”的所有原始记录。
        all_source_ids: set[str] = set()
        versions_view: list[dict[str, Any]] = []
        for row in version_rows:
            members = json.loads(row["members_json"])
            all_source_ids.update(members)
            versions_view.append({
                "version_no": row["version_no"],
                "run_id": row["run_id"],
                "state": row["state"],
                "members": members,
                "note": row["note"],
                "decided_by": row["decided_by"],
                "decided_at": row["decided_at"],
            })

        records = {
            record_id: self.record_view(self._load_record_row(record_id), actor_id, viewer["role"])
            for record_id in sorted(all_source_ids)
        }

        # 当前版本运行中，与成员相关但未纳入的记录及其排除理由。
        exclusions: list[dict[str, Any]] = []
        for pair in current_result["pairs"]:
            if pair["candidate"]:
                continue
            side = None
            if pair["record_a"] in current_members:
                side = pair["record_b"]
            elif pair["record_b"] in current_members:
                side = pair["record_a"]
            if side is not None:
                redacted = self._redact_pair(pair, {
                    rid: self._can_view_coordinates(self._load_record_row(rid), actor_id, viewer["role"])
                    for rid in (pair["record_a"], pair["record_b"])
                })
                exclusions.append({
                    "member_record_id": pair["record_a"] if side == pair["record_b"] else pair["record_b"],
                    "excluded_record_id": side,
                    "score": pair["score"],
                    "exclusion_reasons": pair["exclusion_reasons"],
                    "dimensions": redacted["dimensions"],
                })

        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM audit_events "
            "WHERE entity_type='incident' AND entity_id=? ORDER BY event_id", (event_id,)
        ).fetchall()

        return {
            "event_id": event_id,
            "state": current["state"],
            "current_version": incident["current_version"],
            "current_run_id": current["run_id"],
            "versions": versions_view,
            "source_records": records,
            "excluded_candidates": sorted(exclusions, key=lambda item: item["excluded_record_id"]),
            "decision_history": [
                dict(row) | {"payload": json.loads(row["payload_json"])} for row in events
            ],
        }
