"""Pytest fixtures: isolated SQLite DB per test with seeded example data."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture(autouse=True)
def _clean_rag_flags(monkeypatch):
    # A developer's local .env (auto-loaded by config) may set RAG_ENABLED=1 and
    # real API keys. Force a known-OFF baseline so tests never depend on it — and
    # never accidentally hit the live Claude API. Tests that need RAG on flip
    # these via their own monkeypatch.
    from app import config
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    monkeypatch.setattr(config, "READONLY", False)


@pytest.fixture()
def client(tmp_path):
    # Build a throwaway engine bound to a per-test SQLite file and rebind the
    # globals the app captured at import time (no module reloads — that breaks
    # SQLAlchemy's mapped classes on Python 3.9).
    from app import database, seed
    from app.database import get_db
    from app.main import app

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    database.engine = test_engine
    database.SessionLocal = TestSession
    seed.engine = test_engine
    seed.SessionLocal = TestSession

    def _get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db

    # TestClient(... ) triggers the startup event → init_db() creates the schema
    # and seeds the example strategy into the temp DB.
    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()
