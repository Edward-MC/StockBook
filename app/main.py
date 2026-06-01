"""Compatibility shim.

The application is defined in the project-root ``main.py`` (so IDE FastAPI
run-configs detect it). This module re-exports it for ``uvicorn app.main:app``
and existing imports.
"""
from main import app

__all__ = ["app"]
