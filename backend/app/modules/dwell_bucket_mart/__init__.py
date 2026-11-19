"""会话停留秒分桶物化。

桶名只能由会话 ``started_at`` / ``ended_at`` 算出的停留秒派生；``ended_at``
缺失时退到该会话末击点（再缺则退回开始时刻）。派生结果落独立事实表
``session_dwell_facts``，由库层**触发器**在起止/击点变更的同一事务内重算：

* 起止一变，桶名同事务整体替换，不存在「起止已变桶名仍旧」；
* 事实表的写入/更新受库层校验，桶名无法脱离停留秒与桶界独立 UPDATE；
* 事实表不允许删除，也没有手改桶名的入口。

桶界按 ``dwell_bucket_bounds`` 的版本存放，版本号由 settings
``dwell_bucket_version`` 指向；改桶界会生成新版本并把既有会话整批重物化。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

STATUS = "implemented"

SETTINGS_KEY = "dwell_bucket_version"

# 默认桶界：上界为开区间（停留秒 < max_sec），最后一档开放上界。
DEFAULT_BOUNDS: list[tuple[int | None, str]] = [
    (60, "0-59s"),
    (300, "60-299s"),
    (None, "300s+"),
]

_UNSET = object()
#: 公开哨兵：路由层用它区分「未提供」与「显式置空」。
UNSET = _UNSET

_GUARD_REASONS = {
    "DWELL_BAD_SPAN": "结束时刻不能早于开始时刻",
    "DWELL_SPAN": "分桶事实的快照与会话当前起止不一致",
    "DWELL_SECONDS": "停留秒必须由会话起止（缺则末击点）算出，不接受手填",
    "DWELL_EFFECTIVE_END": "分桶所用结束时刻与会话起止/末击点不一致",
    "DWELL_BUCKET": "桶名与停留秒、桶界版本不匹配",
    "DWELL_REVISION": "分桶事实只能由重算整体替换（revision 必须递增）",
    "DWELL_SESSION_IMMUTABLE": "分桶事实不能改挂到别的会话",
    "DWELL_FACT_IMMUTABLE": "分桶事实不允许删除，只能由重算替换",
}


class DwellError(Exception):
    """分桶物化被拒；``reason`` 即对外原因。"""

    def __init__(self, reason: str, code: str = "DWELL_REJECTED"):
        super().__init__(reason)
        self.reason = reason
        self.code = code


def translate_sqlite_error(exc: Exception) -> DwellError | None:
    """把库层触发器的 ABORT 码翻成对外原因。"""
    text = str(exc)
    for code, reason in _GUARD_REASONS.items():
        if code in text:
            return DwellError(f"{reason}（{code}）", code)
    return None


# 停留秒表达式：起止优先，缺 ended_at 用末击点，再缺退回开始时刻。
_SECONDS_SQL = """CAST(ROUND((julianday(COALESCE(
    {row}.ended_at,
    (SELECT MAX(h.event_ts) FROM hits h
      WHERE h.session_id = {row}.id AND h.channel = 'primary'),
    {row}.started_at)) - julianday({row}.started_at)) * 86400.0) AS INTEGER)"""

_EFFECTIVE_END_SQL = """COALESCE(
    {row}.ended_at,
    (SELECT MAX(h.event_ts) FROM hits h
      WHERE h.session_id = {row}.id AND h.channel = 'primary'),
    {row}.started_at)"""

_BOUNDS_VERSION_SQL = (
    "(SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'dwell_bucket_version')"
)

_BUCKET_SQL = """(SELECT b.name FROM dwell_bucket_bounds b
   WHERE b.version = {version}
     AND (b.max_sec IS NULL OR b.max_sec > {seconds})
   ORDER BY b.seq LIMIT 1)"""


def _upsert_for(session_id_sql: str) -> str:
    """按给定会话 id 表达式重物化一行的触发器体。

    写法为 ``INSERT … SELECT … WHERE … ON CONFLICT``；SQLite 要求这类语句
    带 WHERE 才能无歧义地接上 upsert 子句。
    """
    seconds = _SECONDS_SQL.format(row="s")
    effective = _EFFECTIVE_END_SQL.format(row="s")
    bucket = _BUCKET_SQL.format(version=_BOUNDS_VERSION_SQL, seconds=seconds)
    return f"""
  INSERT INTO session_dwell_facts(
    session_id, visitor_id, started_snap, ended_snap, effective_end,
    dwell_sec, bounds_version, bucket_name, revision, materialized_at
  )
  SELECT s.id, s.visitor_id, s.started_at, s.ended_at,
         {effective}, {seconds}, {_BOUNDS_VERSION_SQL}, {bucket}, 1,
         strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    FROM sessions s
   WHERE s.id = {session_id_sql}
  ON CONFLICT(session_id) DO UPDATE SET
    visitor_id = excluded.visitor_id,
    started_snap = excluded.started_snap,
    ended_snap = excluded.ended_snap,
    effective_end = excluded.effective_end,
    dwell_sec = excluded.dwell_sec,
    bounds_version = excluded.bounds_version,
    bucket_name = excluded.bucket_name,
    revision = session_dwell_facts.revision + 1,
    materialized_at = excluded.materialized_at;"""


DDL = f"""
CREATE TABLE IF NOT EXISTS dwell_bucket_bounds (
  version INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  max_sec INTEGER,
  name TEXT NOT NULL,
  PRIMARY KEY (version, seq),
  UNIQUE (version, name)
);

CREATE TABLE IF NOT EXISTS session_dwell_facts (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  visitor_id TEXT NOT NULL,
  started_snap TEXT NOT NULL,
  ended_snap TEXT,
  effective_end TEXT NOT NULL,
  dwell_sec INTEGER NOT NULL,
  bounds_version INTEGER NOT NULL,
  bucket_name TEXT NOT NULL,
  revision INTEGER NOT NULL,
  materialized_at TEXT NOT NULL
);

DROP TRIGGER IF EXISTS dwell_facts_guard_ins;
CREATE TRIGGER dwell_facts_guard_ins
BEFORE INSERT ON session_dwell_facts
BEGIN
  SELECT RAISE(ABORT, 'DWELL_SPAN') WHERE NOT EXISTS (
    SELECT 1 FROM sessions s
     WHERE s.id = NEW.session_id
       AND s.visitor_id = NEW.visitor_id
       AND s.started_at = NEW.started_snap
       AND IFNULL(s.ended_at, '') = IFNULL(NEW.ended_snap, '')
  );
  SELECT RAISE(ABORT, 'DWELL_SECONDS') WHERE NEW.dwell_sec <> (
    SELECT CAST(ROUND((julianday(COALESCE(
             s.ended_at,
             (SELECT MAX(h.event_ts) FROM hits h
               WHERE h.session_id = s.id AND h.channel = 'primary'),
             s.started_at)) - julianday(s.started_at)) * 86400.0) AS INTEGER)
      FROM sessions s WHERE s.id = NEW.session_id
  );
  SELECT RAISE(ABORT, 'DWELL_EFFECTIVE_END') WHERE NEW.effective_end <> (
    SELECT COALESCE(
             s.ended_at,
             (SELECT MAX(h.event_ts) FROM hits h
               WHERE h.session_id = s.id AND h.channel = 'primary'),
             s.started_at)
      FROM sessions s WHERE s.id = NEW.session_id
  );
  SELECT RAISE(ABORT, 'DWELL_BUCKET') WHERE NOT EXISTS (
    SELECT 1 FROM dwell_bucket_bounds b
     WHERE b.version = NEW.bounds_version
       AND b.name = NEW.bucket_name
       AND (b.max_sec IS NULL OR b.max_sec > NEW.dwell_sec)
       AND b.seq = (
         SELECT MIN(b2.seq) FROM dwell_bucket_bounds b2
          WHERE b2.version = NEW.bounds_version
            AND (b2.max_sec IS NULL OR b2.max_sec > NEW.dwell_sec))
  );
  SELECT RAISE(ABORT, 'DWELL_REVISION') WHERE NEW.revision <> 1;
END;

DROP TRIGGER IF EXISTS dwell_facts_guard_upd;
CREATE TRIGGER dwell_facts_guard_upd
BEFORE UPDATE ON session_dwell_facts
BEGIN
  SELECT RAISE(ABORT, 'DWELL_SESSION_IMMUTABLE') WHERE NEW.session_id <> OLD.session_id;
  SELECT RAISE(ABORT, 'DWELL_REVISION') WHERE NEW.revision <> OLD.revision + 1;
  SELECT RAISE(ABORT, 'DWELL_SPAN') WHERE NOT EXISTS (
    SELECT 1 FROM sessions s
     WHERE s.id = NEW.session_id
       AND s.visitor_id = NEW.visitor_id
       AND s.started_at = NEW.started_snap
       AND IFNULL(s.ended_at, '') = IFNULL(NEW.ended_snap, '')
  );
  SELECT RAISE(ABORT, 'DWELL_SECONDS') WHERE NEW.dwell_sec <> (
    SELECT CAST(ROUND((julianday(COALESCE(
             s.ended_at,
             (SELECT MAX(h.event_ts) FROM hits h
               WHERE h.session_id = s.id AND h.channel = 'primary'),
             s.started_at)) - julianday(s.started_at)) * 86400.0) AS INTEGER)
      FROM sessions s WHERE s.id = NEW.session_id
  );
  SELECT RAISE(ABORT, 'DWELL_EFFECTIVE_END') WHERE NEW.effective_end <> (
    SELECT COALESCE(
             s.ended_at,
             (SELECT MAX(h.event_ts) FROM hits h
               WHERE h.session_id = s.id AND h.channel = 'primary'),
             s.started_at)
      FROM sessions s WHERE s.id = NEW.session_id
  );
  SELECT RAISE(ABORT, 'DWELL_BUCKET') WHERE NOT EXISTS (
    SELECT 1 FROM dwell_bucket_bounds b
     WHERE b.version = NEW.bounds_version
       AND b.name = NEW.bucket_name
       AND (b.max_sec IS NULL OR b.max_sec > NEW.dwell_sec)
       AND b.seq = (
         SELECT MIN(b2.seq) FROM dwell_bucket_bounds b2
          WHERE b2.version = NEW.bounds_version
            AND (b2.max_sec IS NULL OR b2.max_sec > NEW.dwell_sec))
  );
END;

DROP TRIGGER IF EXISTS dwell_facts_guard_del;
CREATE TRIGGER dwell_facts_guard_del
BEFORE DELETE ON session_dwell_facts
BEGIN
  SELECT RAISE(ABORT, 'DWELL_FACT_IMMUTABLE');
END;

DROP TRIGGER IF EXISTS dwell_sessions_guard_order_ins;
CREATE TRIGGER dwell_sessions_guard_order_ins
BEFORE INSERT ON sessions
WHEN NEW.ended_at IS NOT NULL AND NEW.ended_at < NEW.started_at
BEGIN
  SELECT RAISE(ABORT, 'DWELL_BAD_SPAN');
END;

DROP TRIGGER IF EXISTS dwell_sessions_guard_order_upd;
CREATE TRIGGER dwell_sessions_guard_order_upd
BEFORE UPDATE OF started_at, ended_at ON sessions
WHEN NEW.ended_at IS NOT NULL AND NEW.ended_at < NEW.started_at
BEGIN
  SELECT RAISE(ABORT, 'DWELL_BAD_SPAN');
END;

DROP TRIGGER IF EXISTS dwell_sessions_materialize_ins;
CREATE TRIGGER dwell_sessions_materialize_ins
AFTER INSERT ON sessions
BEGIN
{_upsert_for("NEW.id")}
END;

DROP TRIGGER IF EXISTS dwell_sessions_materialize_upd;
CREATE TRIGGER dwell_sessions_materialize_upd
AFTER UPDATE OF started_at, ended_at ON sessions
WHEN NEW.started_at <> OLD.started_at
  OR IFNULL(NEW.ended_at, '') <> IFNULL(OLD.ended_at, '')
BEGIN
{_upsert_for("NEW.id")}
END;

DROP TRIGGER IF EXISTS dwell_hits_materialize_ins;
CREATE TRIGGER dwell_hits_materialize_ins
AFTER INSERT ON hits
WHEN NEW.session_id IS NOT NULL
 AND EXISTS (
   SELECT 1 FROM sessions s
    WHERE s.id = NEW.session_id AND s.ended_at IS NULL
 )
BEGIN
{_upsert_for("NEW.session_id")}
END;

DROP TRIGGER IF EXISTS dwell_hits_materialize_del;
CREATE TRIGGER dwell_hits_materialize_del
AFTER DELETE ON hits
WHEN OLD.session_id IS NOT NULL
 AND EXISTS (
   SELECT 1 FROM sessions s
    WHERE s.id = OLD.session_id AND s.ended_at IS NULL
 )
BEGIN
{_upsert_for("OLD.session_id")}
END;

DROP TRIGGER IF EXISTS dwell_hits_materialize_upd_old;
CREATE TRIGGER dwell_hits_materialize_upd_old
AFTER UPDATE OF event_ts, session_id ON hits
WHEN OLD.session_id IS NOT NULL
 AND EXISTS (
   SELECT 1 FROM sessions s
    WHERE s.id = OLD.session_id AND s.ended_at IS NULL
 )
BEGIN
{_upsert_for("OLD.session_id")}
END;

DROP TRIGGER IF EXISTS dwell_hits_materialize_upd_new;
CREATE TRIGGER dwell_hits_materialize_upd_new
AFTER UPDATE OF event_ts, session_id ON hits
WHEN NEW.session_id IS NOT NULL
 AND NEW.session_id IS NOT OLD.session_id
 AND EXISTS (
   SELECT 1 FROM sessions s
    WHERE s.id = NEW.session_id AND s.ended_at IS NULL
 )
BEGIN
{_upsert_for("NEW.session_id")}
END;
"""


# ---------------------------------------------------------------------------
# 时间 / 桶界
# ---------------------------------------------------------------------------

def canonical_ts(value) -> str:
    """把 ISO-8601 时间规整成 ``YYYY-MM-DDTHH:MM:SSZ``。"""
    if value is None or str(value).strip() == "":
        raise DwellError("时间不能为空", "DWELL_BAD_TS")
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DwellError(
            f"时间格式须为 ISO-8601（如 2026-11-17T08:10:00Z）：{text}",
            "DWELL_BAD_TS",
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond:
        raise DwellError("时间仅支持秒级精度", "DWELL_BAD_TS")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def dwell_seconds(started_at: str, effective_end: str) -> int:
    seconds = (_parse_ts(effective_end) - _parse_ts(started_at)).total_seconds()
    if seconds < 0:
        raise DwellError("结束时刻不能早于开始时刻", "DWELL_BAD_SPAN")
    return int(round(seconds))


def normalize_bounds(raw) -> list[tuple[int | None, str]]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise DwellError("桶界至少需要两档", "DWELL_BAD_BOUNDS")
    out: list[tuple[int | None, str]] = []
    names: set[str] = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise DwellError("桶界每档须为 {max_sec, name}", "DWELL_BAD_BOUNDS")
        name = str(item.get("name") or "").strip()
        if not name:
            raise DwellError("桶名不能为空", "DWELL_BAD_BOUNDS")
        if name in names:
            raise DwellError(f"桶名重复：{name}", "DWELL_BAD_BOUNDS")
        names.add(name)
        max_sec = item.get("max_sec")
        if max_sec is None or max_sec == "":
            if idx != len(raw) - 1:
                raise DwellError("只有最后一档可以开放上界", "DWELL_BAD_BOUNDS")
            out.append((None, name))
            continue
        try:
            max_sec_int = int(max_sec)
        except (TypeError, ValueError) as exc:
            raise DwellError(f"桶界上界须为整数秒：{max_sec}", "DWELL_BAD_BOUNDS") from exc
        if max_sec_int <= 0:
            raise DwellError("桶界上界须为正整数", "DWELL_BAD_BOUNDS")
        prev = out[-1][0] if out else 0
        if prev is None:
            raise DwellError("开放上界后不能再有档位", "DWELL_BAD_BOUNDS")
        if max_sec_int <= prev:
            raise DwellError("桶界上界须严格递增", "DWELL_BAD_BOUNDS")
        out.append((max_sec_int, name))
    if out[-1][0] is not None:
        raise DwellError("最后一档须为开放上界（max_sec=null）", "DWELL_BAD_BOUNDS")
    return out


def bucket_for(seconds: int, bounds: list[tuple[int | None, str]]) -> str:
    for max_sec, name in bounds:
        if max_sec is None or seconds < max_sec:
            return name
    raise DwellError("桶界未覆盖该停留秒", "DWELL_BAD_BOUNDS")


def bounds_rows(conn, version: int | None = None) -> list[dict]:
    if version is None:
        version = current_version(conn)
    rows = conn.execute(
        "SELECT version, seq, max_sec, name FROM dwell_bucket_bounds "
        "WHERE version=? ORDER BY seq",
        (version,),
    ).fetchall()
    return [dict(r) for r in rows]


def bounds_for(conn, version: int) -> list[tuple[int | None, str]]:
    rows = bounds_rows(conn, version)
    if not rows:
        raise DwellError(f"桶界版本 {version} 不存在", "DWELL_BAD_BOUNDS")
    return [(r["max_sec"], r["name"]) for r in rows]


def current_version(conn) -> int:
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (SETTINGS_KEY,)
    ).fetchone()
    if not row:
        raise DwellError("桶界版本未初始化", "DWELL_NOT_INITIALIZED")
    return int(row["value"])


def current_bounds(conn) -> tuple[int, list[tuple[int | None, str]]]:
    version = current_version(conn)
    return version, bounds_for(conn, version)


def ensure_defaults(conn) -> None:
    """首次启动写入默认桶界版本。"""
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (SETTINGS_KEY,)
    ).fetchone()
    if row:
        return
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?)", (SETTINGS_KEY, "1")
    )
    for seq, (max_sec, name) in enumerate(DEFAULT_BOUNDS, start=1):
        conn.execute(
            "INSERT INTO dwell_bucket_bounds(version, seq, max_sec, name) VALUES (1,?,?,?)",
            (seq, max_sec, name),
        )


# ---------------------------------------------------------------------------
# 会话起止（物化由触发器完成）
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session(conn, session_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        raise DwellError(f"会话不存在：{session_id}", "DWELL_NOT_FOUND")
    return row


def _effective_end(conn, sess: sqlite3.Row) -> str:
    if sess["ended_at"]:
        return sess["ended_at"]
    last = conn.execute(
        "SELECT MAX(event_ts) AS ts FROM hits "
        "WHERE session_id=? AND channel='primary'",
        (sess["id"],),
    ).fetchone()["ts"]
    return last or sess["started_at"]


def _fact(conn, session_id: str):
    return conn.execute(
        "SELECT * FROM session_dwell_facts WHERE session_id=?", (session_id,)
    ).fetchone()


def apply_span(
    conn,
    session_id: str,
    *,
    started_at=_UNSET,
    ended_at=_UNSET,
    status=_UNSET,
) -> dict:
    """在调用方事务内改会话起止；分桶由库层触发器同事务重算。

    调用前事务须已开启；本函数不 commit/rollback。任何校验失败抛
    :class:`DwellError`，调用方回滚即整笔失败。
    """
    sess = _session(conn, session_id)
    new_started = (
        sess["started_at"] if started_at is _UNSET else canonical_ts(started_at)
    )
    new_ended = (
        sess["ended_at"]
        if ended_at is _UNSET
        else (None if ended_at is None else canonical_ts(ended_at))
    )
    new_status = sess["status"] if status is _UNSET else str(status)
    if new_status not in ("open", "closed"):
        raise DwellError("会话状态只能是 open / closed", "DWELL_BAD_STATUS")
    if new_ended is not None:
        dwell_seconds(new_started, new_ended)  # 顺序校验

    span_changed = new_started != sess["started_at"] or new_ended != sess["ended_at"]
    if span_changed or new_status != sess["status"]:
        try:
            conn.execute(
                "UPDATE sessions SET started_at=?, ended_at=?, status=? WHERE id=?",
                (new_started, new_ended, new_status, session_id),
            )
        except sqlite3.IntegrityError as exc:
            raise translate_sqlite_error(exc) or exc

    fresh = _session(conn, session_id)
    return {"session": dict(fresh), "dwell": fact_dict(conn, session_id)}


def update_span(conn, session_id: str, **kwargs) -> dict:
    """独立事务版的 :func:`apply_span`。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = apply_span(conn, session_id, **kwargs)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def close_session(conn, session_id: str, *, ended_at=_UNSET) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        sess = _session(conn, session_id)
        if ended_at is _UNSET:
            ended_at = (
                sess["ended_at"]
                if sess["ended_at"]
                else _effective_end(conn, sess)
            )
        result = apply_span(
            conn, session_id, ended_at=ended_at, status="closed"
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# 物化（桶界切换 / 补写）
# ---------------------------------------------------------------------------

def _write_fact(
    conn,
    sess: sqlite3.Row,
    version: int,
    bounds: list[tuple[int | None, str]],
) -> None:
    effective_end = _effective_end(conn, sess)
    seconds = dwell_seconds(sess["started_at"], effective_end)
    bucket = bucket_for(seconds, bounds)
    existing = _fact(conn, sess["id"])
    try:
        if existing:
            conn.execute(
                """
                UPDATE session_dwell_facts
                   SET visitor_id=?, started_snap=?, ended_snap=?, effective_end=?,
                       dwell_sec=?, bounds_version=?, bucket_name=?,
                       revision=revision+1, materialized_at=?
                 WHERE session_id=?
                """,
                (
                    sess["visitor_id"],
                    sess["started_at"],
                    sess["ended_at"],
                    effective_end,
                    seconds,
                    version,
                    bucket,
                    _now(),
                    sess["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO session_dwell_facts(
                  session_id, visitor_id, started_snap, ended_snap, effective_end,
                  dwell_sec, bounds_version, bucket_name, revision, materialized_at
                ) VALUES (?,?,?,?,?,?,?,?,1,?)
                """,
                (
                    sess["id"],
                    sess["visitor_id"],
                    sess["started_at"],
                    sess["ended_at"],
                    effective_end,
                    seconds,
                    version,
                    bucket,
                    _now(),
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise translate_sqlite_error(exc) or exc


def set_bounds_payload(conn, raw) -> dict:
    """生成新的桶界版本，并同事务把全部会话重物化到新版本。"""
    bounds = normalize_bounds(raw)
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = current_version(conn) + 1
        for seq, (max_sec, name) in enumerate(bounds, start=1):
            conn.execute(
                "INSERT INTO dwell_bucket_bounds(version, seq, max_sec, name) "
                "VALUES (?,?,?,?)",
                (version, seq, max_sec, name),
            )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            (SETTINGS_KEY, str(version)),
        )
        sessions = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
        for sess in sessions:
            _write_fact(conn, sess, version, bounds)
        conn.commit()
        return {
            "version": version,
            "bounds": [
                {"max_sec": m, "name": n} for m, n in bounds
            ],
            "rematerialized": len(sessions),
        }
    except Exception:
        conn.rollback()
        raise


def backfill(conn) -> int:
    """为缺分桶事实的会话补物化（启动时调用，不重写已有事实）。"""
    version, bounds = current_bounds(conn)
    written = 0
    for sess in conn.execute("SELECT * FROM sessions ORDER BY id").fetchall():
        if _fact(conn, sess["id"]) is None:
            _write_fact(conn, sess, version, bounds)
            written += 1
    return written


# ---------------------------------------------------------------------------
# 只读核证
# ---------------------------------------------------------------------------

def fact_dict(conn, session_id: str) -> dict | None:
    row = _fact(conn, session_id)
    if not row:
        return None
    bounds = bounds_for(conn, row["bounds_version"])
    return {
        "session_id": row["session_id"],
        "visitor_id": row["visitor_id"],
        "started_snap": row["started_snap"],
        "ended_snap": row["ended_snap"],
        "effective_end": row["effective_end"],
        "dwell_sec": row["dwell_sec"],
        "bounds_version": row["bounds_version"],
        "bucket_name": row["bucket_name"],
        "revision": row["revision"],
        "materialized_at": row["materialized_at"],
        "bounds": [{"max_sec": m, "name": n} for m, n in bounds],
    }


def list_facts(conn, session_id: str | None = None) -> list[dict]:
    sql = """
        SELECT f.*, s.started_at AS session_started_at, s.ended_at AS session_ended_at,
               s.status AS session_status
          FROM session_dwell_facts f
          JOIN sessions s ON s.id = f.session_id
    """
    params: tuple = ()
    if session_id:
        sql += " WHERE f.session_id = ?"
        params = (session_id,)
    sql += " ORDER BY s.started_at, f.session_id"

    live_version = current_version(conn)
    version_cache: dict[int, list[tuple[int | None, str]]] = {}
    out = []
    for row in conn.execute(sql, params).fetchall():
        version = row["bounds_version"]
        if version not in version_cache:
            version_cache[version] = bounds_for(conn, version)
        bounds = version_cache[version]
        sess = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (row["session_id"],)
        ).fetchone()
        expected_end = _effective_end(conn, sess) if sess else None
        try:
            expected_sec = (
                dwell_seconds(sess["started_at"], expected_end) if sess else None
            )
            expected_bucket = (
                bucket_for(expected_sec, bounds) if expected_sec is not None else None
            )
        except DwellError:
            expected_sec, expected_bucket = None, None
        verified = bool(
            sess
            and row["started_snap"] == sess["started_at"]
            and (row["ended_snap"] or "") == (sess["ended_at"] or "")
            and row["effective_end"] == expected_end
            and row["dwell_sec"] == expected_sec
            and row["bucket_name"] == expected_bucket
            and version == live_version
        )
        out.append(
            {
                "session_id": row["session_id"],
                "visitor_id": row["visitor_id"],
                "session_started_at": row["session_started_at"],
                "session_ended_at": row["session_ended_at"],
                "session_status": row["session_status"],
                "started_snap": row["started_snap"],
                "ended_snap": row["ended_snap"],
                "effective_end": row["effective_end"],
                "dwell_sec": row["dwell_sec"],
                "bounds_version": version,
                "live_bounds_version": live_version,
                "bucket_name": row["bucket_name"],
                "revision": row["revision"],
                "materialized_at": row["materialized_at"],
                "bounds": [{"max_sec": m, "name": n} for m, n in bounds],
                "expected_dwell_sec": expected_sec,
                "expected_bucket_name": expected_bucket,
                "verified": verified,
            }
        )
    return out


def bounds_payload(conn) -> dict:
    versions = [
        r["version"]
        for r in conn.execute(
            "SELECT DISTINCT version FROM dwell_bucket_bounds ORDER BY version"
        ).fetchall()
    ]
    return {
        "current_version": current_version(conn),
        "versions": [
            {"version": v, "bounds": bounds_rows(conn, v)} for v in versions
        ],
    }
