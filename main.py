"""FastAPI application entrypoint — «衡» StockBook strategy tracker.

Run with:  uvicorn main:app --reload
The `app` object is defined here at module top level so IDE run-configs
(PyCharm's FastAPI detector) and uvicorn both find it directly.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import backup, config, snapshot_service
from app.routers import api, pages, rag
from app.seed import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    backup_task = backup.start_scheduler()
    snapshot_task = snapshot_service.start_scheduler()
    try:
        yield
    finally:
        await backup.stop_scheduler(backup_task)
        await snapshot_service.stop_scheduler(snapshot_task)


app = FastAPI(title="StockBook · 衡", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
app.include_router(api.router)
app.include_router(pages.router)
app.include_router(rag.router)
