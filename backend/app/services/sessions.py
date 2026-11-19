from app.modules import dwell_bucket_mart as dwell


def _dwell_fields(conn, session_id: str) -> dict:
    fact = dwell.fact_dict(conn, session_id)
    if not fact:
        return {"dwell_sec": None, "dwell_bucket": None, "dwell": None}
    return {
        "dwell_sec": fact["dwell_sec"],
        "dwell_bucket": fact["bucket_name"],
        "dwell": fact,
    }


def list_sessions(conn, visitor_id: str | None = None):
    if visitor_id:
        rows = conn.execute(
            """
            SELECT id, visitor_id, started_at, ended_at, hit_count, status
            FROM sessions WHERE visitor_id=?
            ORDER BY started_at
            """,
            (visitor_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, visitor_id, started_at, ended_at, hit_count, status
            FROM sessions ORDER BY started_at
            """
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item.update(_dwell_fields(conn, item["id"]))
        out.append(item)
    return out


def session_detail(conn, session_id: str):
    sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        return None
    hits = conn.execute(
        """
        SELECT id, visitor_id, session_id, event_ts, page_path, event_name, channel
        FROM hits WHERE session_id=? AND channel='primary'
        ORDER BY event_ts, id
        """,
        (session_id,),
    ).fetchall()
    proof = conn.execute(
        "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return {
        "session": dict(sess),
        "hits": [dict(h) for h in hits],
        "proof": dict(proof) if proof else None,
        "dwell": _dwell_fields(conn, session_id),
    }
