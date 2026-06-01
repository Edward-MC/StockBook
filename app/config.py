"""Application configuration.

Local-first, single-user. Settings come from environment variables with
sensible defaults so the app runs with zero configuration.
"""
import os
from pathlib import Path

# Project root (one level above the app/ package).
BASE_DIR = Path(__file__).resolve().parent.parent

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
