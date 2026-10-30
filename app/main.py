"""FastAPI entrypoint. Serves the API and the single-page CSM console."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import scheduler
from .api.routes import router
from .config import WEB_DIR
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("chp")

app = FastAPI(title="Renewal Risk Workbench", version="1.0.0")
app.include_router(router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    scheduler.start()
    log.info("startup complete; scheduler=%s", scheduler.status())


@app.get("/api/health")
def health():
    return {"ok": True, "scheduler": scheduler.status(), "service": "renewal-risk-workbench"}


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(WEB_DIR / "index.html"))
