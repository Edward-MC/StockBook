"""Application configuration.

Local-first, single-user. Settings come from environment variables with
sensible defaults so the app runs with zero configuration.
"""
import os
from pathlib import Path

# Project root (one level above the app/ package).
BASE_DIR = Path(__file__).resolve().parent.parent

# Load a local .env (gitignored) before any os.getenv below, so secrets like
# NOTION_TOKEN / ANTHROPIC_API_KEY work with a plain `uvicorn main:app`.
# Real environment variables still take precedence (override=False).
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env", override=False)
except ImportError:  # python-dotenv optional; absence just means no .env loading
    pass

# SQLite single-file database — portable, easy to ship to others.
DATABASE_URL = os.getenv("STOCKBOOK_DATABASE_URL", f"sqlite:///{BASE_DIR / 'stockbook.db'}")

# When true, the whole instance is read-only regardless of URL params
# (the per-request ?readonly=1 param can still force read-only on top of this).
READONLY = os.getenv("STOCKBOOK_READONLY", "").lower() in {"1", "true", "yes"}

# When true, monetary amounts are hidden globally (percentages only).
HIDE_AMOUNTS = os.getenv("STOCKBOOK_HIDE_AMOUNTS", "").lower() in {"1", "true", "yes"}

# Auto-refresh live quotes on page load (disable for offline use / packaging).
AUTO_REFRESH = os.getenv("STOCKBOOK_AUTO_REFRESH", "1").lower() in {"1", "true", "yes"}

# Ordered quote-source failover chain. First one that responds wins.
QUOTE_SOURCES = tuple(
    s.strip().lower()
    for s in os.getenv("STOCKBOOK_QUOTE_SOURCES", "tencent,sina,eastmoney").split(",")
    if s.strip()
)

TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# --------------------------------------------------------------------------- #
# RAG Q&A (Phase 2). Feature is OFF unless explicitly enabled. Keys come only
# from the environment — never stored in the DB or sent to the frontend.
# --------------------------------------------------------------------------- #
RAG_ENABLED = os.getenv("STOCKBOOK_RAG_ENABLED", "").lower() in {"1", "true", "yes"}

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Daily cap on /api/rag/ask calls (cost protection). Editable via env.
RAG_DAILY_LIMIT = int(os.getenv("STOCKBOOK_RAG_DAILY_LIMIT", "50"))

# Answer model — default to the cheap/fast Haiku; switchable via env.
RAG_MODEL = os.getenv("STOCKBOOK_RAG_MODEL", "claude-haiku-4-5-20251001")

# Retrieval / context trimming (cost protection).
RAG_TOP_K = int(os.getenv("STOCKBOOK_RAG_TOP_K", "5"))
RAG_CHUNK_CHARS = int(os.getenv("STOCKBOOK_RAG_CHUNK_CHARS", "1200"))      # chunk size when splitting
RAG_EXCERPT_CHARS = int(os.getenv("STOCKBOOK_RAG_EXCERPT_CHARS", "800"))    # per-chunk cap in prompt
RAG_EMBED_MODEL = os.getenv("STOCKBOOK_RAG_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")

# Backups (data-safety hardening). BACKUP_DIR is an offsite/synced-folder path
# (e.g. inside iCloud/坚果云); empty = local primary only. INTERVAL 0 disables
# the in-process auto-backup scheduler.
BACKUP_DIR = os.getenv("STOCKBOOK_BACKUP_DIR", "")
BACKUP_INTERVAL_HOURS = int(os.getenv("STOCKBOOK_BACKUP_INTERVAL_HOURS", "12"))
BACKUP_KEEP = int(os.getenv("STOCKBOOK_BACKUP_KEEP", "30"))

# Backup encryption (offsite only). Set this to encrypt the offsite/synced-folder
# copy (Fernet + scrypt). Empty = offsite stays plaintext (a warning is logged).
# Secret — .env only, never committed/logged.
BACKUP_PASSPHRASE = os.getenv("STOCKBOOK_BACKUP_PASSPHRASE", "")

# History + performance (daily NAV snapshots). BENCHMARK_CODE carries an explicit
# market prefix (sh000300 = 沪深300) because indices are not stocks and the
# per-stock code→market heuristic would mis-route them; empty = skip benchmark.
# INTERVAL 0 disables the in-process daily-snapshot scheduler.
BENCHMARK_CODE = os.getenv("STOCKBOOK_BENCHMARK_CODE", "sh000300")
SNAPSHOT_INTERVAL_HOURS = int(os.getenv("STOCKBOOK_SNAPSHOT_INTERVAL_HOURS", "24"))
