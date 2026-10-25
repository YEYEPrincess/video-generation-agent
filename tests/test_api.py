"""HTTP 层：契约、状态码、幂等头。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def _client(settings):
    return TestClient(create_app(settings))


def test_healthz(settings):
    with _client(settings) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_index_serves_html(settings):
    with _client(settings) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "任务编排" in r.text


def test_create_then_get(settings):
    with _client(settings) as c:
        r = c.post("/tasks", json={"topic": "为什么年轻人开始重看老动画"})
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "PENDING"
        assert body["task_id"].startswith("t_")

        d = c.get(f"/tasks/{body['task_id']}").json()
        assert d["progress"] == {"done": 0, "total": 7}
        assert len(d["stages"]) == 7
        assert d["task"]["status"] == "PENDING"


def test_unknown_task_404(settings):
    with _client(settings) as c:
        assert c.get("/tasks/t_not_exist").status_code == 404


def test_empty_topic_422(settings):
    with _client(settings) as c:
        assert c.post("/tasks", json={"topic": ""}).status_code == 422
        assert c.post("/tasks", json={"topic": "x" * 201}).status_code == 422


def test_idempotency_header_dedupes(settings):
    with _client(settings) as c:
        h = {"Idempotency-Key": "abc-123"}
        a = c.post("/tasks", json={"topic": "同一次点击"}, headers=h).json()
        b = c.post("/tasks", json={"topic": "同一次点击"}, headers=h).json()
        assert a["task_id"] == b["task_id"]
        assert b["deduplicated"] is True
        assert len(c.get("/tasks").json()["tasks"]) == 1


def test_events_endpoint(settings):
    with _client(settings) as c:
        tid = c.post("/tasks", json={"topic": "事件"}).json()["task_id"]
        e = c.get(f"/tasks/{tid}/events").json()["events"]
        assert len(e) >= 1
        assert e[0]["type"] == "state_transition"
