import json

import pytest
from fastapi.testclient import TestClient

from app import seed
from app.db import connect
from app.modules import dwell_bucket_mart as dwell


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FIXTURES_DIR", str(tmp_path / "fixtures"))
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "seed_hits.json").write_text(
        json.dumps(
            {
                "visitors": [
                    {"id": "V-1", "label": "甲", "created_at": "2026-11-17T08:00:00Z"}
                ],
                "hits": [
                    {"id": "H1", "visitor_id": "V-1", "event_ts": "2026-11-17T08:10:00Z",
                     "page_path": "/home", "event_name": "view"},
                    {"id": "H2", "visitor_id": "V-1", "event_ts": "2026-11-17T08:13:00Z",
                     "page_path": "/item", "event_name": "click"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (fixtures / "ingest_batch.json").write_text(
        json.dumps({"batch_id": "B-1", "hits": []}), encoding="utf-8"
    )

    # db.py 在导入时解析环境变量，这里重载取值。
    import app.db as db_mod

    monkeypatch.setattr(db_mod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "data" / "clickspan.db")
    monkeypatch.setattr(db_mod, "FIXTURES_DIR", fixtures)
    import app.seed as seed_mod

    monkeypatch.setattr(seed_mod, "FIXTURES_DIR", fixtures)

    seed_mod.init_db()
    return tmp_path


@pytest.fixture()
def client(env):
    from app.main import app

    with TestClient(app) as c:
        yield c


def _fact(conn, sid):
    return dwell.fact_dict(conn, sid)


def test_materialized_on_seed(client):
    rows = client.get("/api/dwell/facts").json()
    assert len(rows) == 1
    row = rows[0]
    assert (row["dwell_sec"], row["bucket_name"]) == (180, "60-299s")
    assert row["verified"] is True


def test_manual_bucket_edit_fails_closed(client):
    r = client.patch("/api/sessions/S-V-1-1", json={"dwell_bucket": "300s+"})
    assert r.status_code == 409
    assert "DWELL_BUCKET_MANUAL_EDIT" in r.json()["detail"]
    r = client.patch("/api/sessions/S-V-1-1", json={"bucket": "300s+"})
    assert r.status_code == 409
    # 整笔失败：起止与桶名都未动
    row = client.get("/api/dwell/facts").json()[0]
    assert (row["dwell_sec"], row["bucket_name"], row["revision"]) == (180, "60-299s", 1)


def test_span_change_rewrites_bucket_same_txn(client):
    before = client.get("/api/sessions").json()[0]
    assert before["dwell_bucket"] == "60-299s"
    r = client.post(
        "/api/sessions/S-V-1-1/span", json={"ended_at": "2026-11-17T08:10:30Z"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["ended_at"] == "2026-11-17T08:10:30Z"
    assert body["dwell"]["dwell_sec"] == 30
    assert body["dwell"]["bucket_name"] == "0-59s"
    row = client.get("/api/dwell/facts").json()[0]
    assert row["verified"] is True
    assert row["started_snap"] == row["session_started_at"]
    assert row["ended_snap"] == row["session_ended_at"]


def test_close_session_bucket_follows_span(client):
    r = client.post("/api/sessions/S-V-1-1/close", json={"ended_at": "2026-11-17T08:20:00Z"})
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["status"] == "closed"
    assert body["dwell"]["dwell_sec"] == 600
    assert body["dwell"]["bucket_name"] == "300s+"
    # 关闭后改起止仍同步重算（不因 closed 而冻结）
    r = client.post(
        "/api/sessions/S-V-1-1/span", json={"ended_at": "2026-11-17T08:14:00Z"}
    )
    assert r.status_code == 200
    assert r.json()["dwell"]["bucket_name"] == "60-299s"


def test_missing_ended_at_falls_back_to_last_hit(client):
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        dwell.apply_span(conn, "S-V-1-1", ended_at=None)
        conn.commit()
        fact = _fact(conn, "S-V-1-1")
        assert fact["ended_snap"] is None
        assert fact["effective_end"] == "2026-11-17T08:13:00Z"
        assert fact["dwell_sec"] == 180
    finally:
        conn.close()


def test_bounds_version_recomputes_all(client):
    r = client.put(
        "/api/dwell/bounds",
        json={
            "bounds": [
                {"max_sec": 120, "name": "短"},
                {"max_sec": None, "name": "长"},
            ]
        },
    )
    assert r.status_code == 200
    assert r.json()["version"] == 2
    row = client.get("/api/dwell/facts").json()[0]
    assert (row["bucket_name"], row["bounds_version"]) == ("长", 2)
    assert row["verified"] is True


def test_bad_bounds_rejected(client):
    r = client.put(
        "/api/dwell/bounds",
        json={
            "bounds": [
                {"max_sec": None, "name": "开放"},
                {"max_sec": 60, "name": "后置"},
            ]
        },
    )
    assert r.status_code == 400


# 库层对抗：绕过 API 直接改库也须被拒
def test_db_rejects_direct_bucket_update(client):
    conn = connect()
    try:
        conn.execute("UPDATE sessions SET hit_count=hit_count")  # 开启隐式事务前先探测
        with pytest.raises(Exception) as exc:
            conn.execute(
                "UPDATE session_dwell_facts SET bucket_name='300s+' WHERE session_id='S-V-1-1'"
            )
        assert "DWELL_REVISION" in str(exc.value) or "DWELL_BUCKET" in str(exc.value)
        conn.rollback()
        fact = _fact(conn, "S-V-1-1")
        assert fact["bucket_name"] == "60-299s"
    finally:
        conn.close()


def test_db_direct_span_update_rematerializes(client):
    """绕过 API 直连 SQL 改起止：同事务内桶名也被重算，不可能留下旧桶名。"""
    conn = connect()
    try:
        conn.execute(
            "UPDATE sessions SET ended_at='2026-11-17T08:10:10Z' WHERE id='S-V-1-1'"
        )
        conn.commit()
        sess = conn.execute("SELECT * FROM sessions WHERE id='S-V-1-1'").fetchone()
        assert sess["ended_at"] == "2026-11-17T08:10:10Z"
        fact = _fact(conn, "S-V-1-1")
        assert fact["ended_snap"] == "2026-11-17T08:10:10Z"
        assert fact["dwell_sec"] == 10
        assert fact["bucket_name"] == "0-59s"
        assert fact["revision"] == 2
    finally:
        conn.close()


def test_db_rejects_backwards_span(client):
    conn = connect()
    try:
        with pytest.raises(Exception) as exc:
            conn.execute(
                "UPDATE sessions SET ended_at='2026-11-17T08:00:00Z' WHERE id='S-V-1-1'"
            )
        assert "DWELL_BAD_SPAN" in str(exc.value)
        conn.rollback()
        fact = _fact(conn, "S-V-1-1")
        assert (fact["dwell_sec"], fact["bucket_name"]) == (180, "60-299s")
    finally:
        conn.close()


def test_db_rejects_fact_with_wrong_seconds(client):
    conn = connect()
    try:
        with pytest.raises(Exception) as exc:
            conn.execute(
                """
                INSERT INTO session_dwell_facts(
                  session_id, visitor_id, started_snap, ended_snap, effective_end,
                  dwell_sec, bounds_version, bucket_name, revision, materialized_at
                ) VALUES ('S-V-1-1','V-1','2026-11-17T08:10:00Z','2026-11-17T08:13:00Z',
                          '2026-11-17T08:13:00Z', 30, 1, '0-59s', 2, 'now')
                """
            )
        assert "DWELL_SECONDS" in str(exc.value) or "DWELL_BUCKET" in str(exc.value)
        conn.rollback()
    finally:
        conn.close()


def test_db_rejects_fact_delete(client):
    conn = connect()
    try:
        with pytest.raises(Exception) as exc:
            conn.execute("DELETE FROM session_dwell_facts WHERE session_id='S-V-1-1'")
        assert "DWELL_FACT_IMMUTABLE" in str(exc.value)
        conn.rollback()
    finally:
        conn.close()


def test_new_hit_rematerializes_open_session(client, env):
    # 先经正规路径把会话起止置空（停留秒退到末击点）。
    r = client.post("/api/sessions/S-V-1-1/span", json={"ended_at": None})
    assert r.status_code == 200
    assert r.json()["dwell"]["effective_end"] == "2026-11-17T08:13:00Z"

    # 新击点到来：写入击点本身即由触发器同事务重算末击点退路。
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)"
            " VALUES ('H3','V-1','S-V-1-1','2026-11-17T08:20:00Z','/pay','view','primary')"
        )
        conn.commit()
        fact = _fact(conn, "S-V-1-1")
        assert fact["ended_snap"] is None
        assert fact["effective_end"] == "2026-11-17T08:20:00Z"
        assert fact["dwell_sec"] == 600
        assert fact["bucket_name"] == "300s+"
        assert fact["revision"] == 3
    finally:
        conn.close()


def test_ingest_extending_session_rematerializes(client, env):
    r = client.post("/api/ingest/run")
    assert r.json()["ok"] is True
    # 夹具无击点；改为经 API 追加后校验桶名跟随
    r = client.post(
        "/api/sessions/S-V-1-1/span", json={"ended_at": "2026-11-17T08:40:00Z"}
    )
    assert r.json()["dwell"]["bucket_name"] == "300s+"
    assert client.get("/api/dwell/facts").json()[0]["verified"] is True


def test_proofs_join_readonly(client):
    rows = client.get("/api/dwell/facts").json()
    assert rows and rows[0]["verified"] is True
    before = json.dumps(rows, sort_keys=True)
    assert client.get("/api/dwell/facts").json()
    assert json.dumps(client.get("/api/dwell/facts").json(), sort_keys=True) == before
