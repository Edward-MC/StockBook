"""API tests for the RAG router: master switch, read-only 403, rate limit."""
import importlib


def _enable_rag(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")


def test_status_reports_disabled_by_default(client):
    r = client.get("/api/rag/status")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_ask_blocked_when_disabled(client):
    r = client.post("/api/rag/ask", json={"question": "hi"})
    assert r.status_code == 403
    assert "未启用" in r.json()["detail"]


def test_ask_blocked_in_readonly(client, monkeypatch):
    _enable_rag(monkeypatch)
    from app import config
    monkeypatch.setattr(config, "READONLY", True)
    r = client.post("/api/rag/ask", json={"question": "hi"})
    assert r.status_code == 403


def test_ask_calls_answer_and_returns_payload(client, monkeypatch):
    _enable_rag(monkeypatch)
    from app.rag import ask
    monkeypatch.setattr(ask, "answer",
                        lambda db, q: {"answer": "摘要…", "citations": []})
    r = client.post("/api/rag/ask", json={"question": "红利怎么看?"})
    assert r.status_code == 200
    assert r.json()["answer"] == "摘要…"


def test_ask_rate_limited(client, monkeypatch):
    _enable_rag(monkeypatch)
    from app import config
    from app.rag import ask
    from app.routers import rag as rag_router
    monkeypatch.setattr(config, "RAG_DAILY_LIMIT", 1)
    monkeypatch.setattr(rag_router, "_limiter",
                        rag_router.limiter.DailyLimiter(config.RAG_DAILY_LIMIT))
    monkeypatch.setattr(ask, "answer", lambda db, q: {"answer": "x", "citations": []})
    assert client.post("/api/rag/ask", json={"question": "1"}).status_code == 200
    r = client.post("/api/rag/ask", json={"question": "2"})
    assert r.status_code == 429
