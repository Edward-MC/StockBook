"""Tests for prompt assembly (pure) and the holdings snapshot."""
import pytest

from app.rag import ask


def test_build_prompt_includes_question_snapshot_and_excerpts():
    chunks = [
        {"text": "红利策略偏好高股息。", "notion_url": "u1", "title_path": "策略/红利"},
        {"text": "中证500代表中小盘。", "notion_url": "u2", "title_path": "笔记/宽基"},
    ]
    prompt = ask.build_prompt("我该如何看红利?", chunks, "总资产：100万\n- 红利：目标20%")
    assert "我该如何看红利?" in prompt
    assert "红利策略偏好高股息。" in prompt
    assert "u1" in prompt                  # source link present for citation
    assert "策略/红利" in prompt            # title_path present for citation
    assert "总资产：100万" in prompt        # snapshot injected


def test_build_prompt_truncates_long_excerpt():
    long_text = "字" * 5000
    prompt = ask.build_prompt("q", [{"text": long_text, "notion_url": "u", "title_path": "t"}], "")
    assert prompt.count("字") <= 1000


def test_build_prompt_no_chunks_states_no_context():
    prompt = ask.build_prompt("q", [], "snap")
    assert "没有" in prompt or "未找到" in prompt


def test_holdings_snapshot_no_strategy(client):
    from app import database
    from app.rag.snapshot import holdings_snapshot
    db = database.SessionLocal()
    try:
        assert isinstance(holdings_snapshot(db), str)
    finally:
        db.close()


def test_answer_raises_without_api_key(client, monkeypatch):
    # With no ANTHROPIC_API_KEY, answer() must raise RuntimeError rather than
    # attempt a network call. Stub retrieval/embedding so we reach the guard.
    from app import config, database
    from app.rag import embed, store
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(embed, "embed_one", lambda q: [0.1, 0.2])
    monkeypatch.setattr(store, "search", lambda db, vec, k=None: [])
    db = database.SessionLocal()
    try:
        with pytest.raises(RuntimeError):
            ask.answer(db, "红利怎么看?")
    finally:
        db.close()
