from fastapi import APIRouter, HTTPException

from app.db import connect
from app.modules import dwell_bucket_mart as dwell
from app.seed import idle_timeout_sec
from app.services import ingest as ingest_svc
from app.services import sessions as sess_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        return {
            "visitors": conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"],
            "sessions": conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"],
            "hits_primary": conn.execute(
                "SELECT COUNT(*) AS c FROM hits WHERE channel='primary'"
            ).fetchone()["c"],
            "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
            "idle_timeout_sec": idle_timeout_sec(conn),
        }
    finally:
        conn.close()


@router.get("/visitors")
def list_visitors():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, label, created_at FROM visitors ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sessions")
def list_sessions(visitor_id: str | None = None):
    conn = connect()
    try:
        return sess_svc.list_sessions(conn, visitor_id)
    finally:
        conn.close()


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    conn = connect()
    try:
        detail = sess_svc.session_detail(conn, session_id)
        if not detail:
            raise HTTPException(404, "会话不存在")
        return detail
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, visitor_id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/session-proofs")
def session_proofs(session_id: str | None = None):
    conn = connect()
    try:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM session_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {"idle_timeout_sec": idle_timeout_sec(conn)}
    finally:
        conn.close()


@router.put("/settings/idle-timeout")
def put_idle_timeout(body: dict):
    sec = int(body.get("idle_timeout_sec", 1800))
    if sec < 60:
        raise HTTPException(400, "空闲超时至少 60 秒")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('idle_timeout_sec', ?)",
            (str(sec),),
        )
        conn.commit()
        return {"idle_timeout_sec": sec}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 停留秒分桶
# ---------------------------------------------------------------------------

def _dwell_http_error(exc: dwell.DwellError) -> HTTPException:
    status = 404 if exc.code == "DWELL_NOT_FOUND" else 409
    if exc.code in ("DWELL_BAD_TS", "DWELL_BAD_BOUNDS", "DWELL_BAD_STATUS"):
        status = 400
    return HTTPException(status, f"[{exc.code}] {exc.reason}")


def _reject_bucket_write(field: str | None = None) -> None:
    name = f"「{field}」" if field else ""
    raise HTTPException(
        409,
        "[DWELL_BUCKET_MANUAL_EDIT] 桶名只能由会话起止（缺则末击点）算出的停留秒"
        "按桶界版本物化，不接受手改为独立字段；"
        f"本笔未写入任何内容{name}。",
    )


_MANUAL_BUCKET_KEYS = ("bucket", "bucket_name", "dwell_bucket", "name")


@router.get("/dwell/facts")
def dwell_facts(session_id: str | None = None):
    conn = connect()
    try:
        return dwell.list_facts(conn, session_id)
    finally:
        conn.close()


@router.get("/dwell/bounds")
def dwell_bounds():
    conn = connect()
    try:
        return dwell.bounds_payload(conn)
    finally:
        conn.close()


@router.put("/dwell/bounds")
def put_dwell_bounds(body: dict):
    conn = connect()
    try:
        result = dwell.set_bounds_payload(conn, body.get("bounds"))
        return result
    except dwell.DwellError as exc:
        raise _dwell_http_error(exc) from exc
    finally:
        conn.close()


@router.post("/sessions/{session_id}/span")
def post_session_span(session_id: str, body: dict):
    if "bucket" in body or "dwell_sec" in body:
        _reject_bucket_write("bucket" if "bucket" in body else "dwell_sec")
    conn = connect()
    try:
        kwargs = {}
        if "started_at" in body:
            kwargs["started_at"] = body["started_at"]
        if "ended_at" in body:
            kwargs["ended_at"] = body["ended_at"]
        if not kwargs:
            raise HTTPException(400, "[DWELL_NO_SPAN] 须提供 started_at 或 ended_at。")
        return dwell.update_span(conn, session_id, **kwargs)
    except dwell.DwellError as exc:
        raise _dwell_http_error(exc) from exc
    finally:
        conn.close()


@router.post("/sessions/{session_id}/close")
def post_session_close(session_id: str, body: dict | None = None):
    body = body or {}
    if "bucket" in body or "dwell_sec" in body:
        _reject_bucket_write("bucket" if "bucket" in body else "dwell_sec")
    conn = connect()
    try:
        ended: object = dwell.UNSET
        if "ended_at" in body:
            ended = body["ended_at"]
        return dwell.close_session(conn, session_id, ended_at=ended)
    except dwell.DwellError as exc:
        raise _dwell_http_error(exc) from exc
    finally:
        conn.close()


@router.patch("/sessions/{session_id}")
def patch_session(session_id: str, body: dict):
    """会话字段补丁；桶名/停留秒不是可写字段，整笔拒绝。"""
    for key in body:
        if key in _MANUAL_BUCKET_KEYS:
            _reject_bucket_write(key)
    if "dwell_sec" in body:
        _reject_bucket_write("dwell_sec")
    allowed = {"started_at", "ended_at", "status"}
    unknown = [k for k in body if k not in allowed]
    if unknown:
        raise HTTPException(
            400,
            f"[DWELL_UNKNOWN_FIELD] 会话不可写字段：{', '.join(unknown)}；"
            "桶名只能由起止物化，整笔未写入。",
        )
    conn = connect()
    try:
        kwargs = {k: body[k] for k in allowed if k in body}
        if not kwargs:
            raise HTTPException(400, "[DWELL_NO_SPAN] 补丁为空。")
        return dwell.update_span(conn, session_id, **kwargs)
    except dwell.DwellError as exc:
        raise _dwell_http_error(exc) from exc
    finally:
        conn.close()
