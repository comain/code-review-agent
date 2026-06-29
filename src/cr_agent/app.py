from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from cr_agent.api.routes import router
from cr_agent.config import Settings, get_settings
from cr_agent.dependencies import get_task_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="cr-agent", version="0.1.0")
    app.include_router(router)
    app.mount(settings.report_public_root, StaticFiles(directory=settings.report_dir), name="reports")
    return app


app = create_app()
