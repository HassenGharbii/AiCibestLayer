from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import FRONTEND_DIR
from .routers import instances, sources, train

app = FastAPI(title="Axivis RT (TX2)", version="0.1.0")

app.add_event_handler("startup", db.init_db)

app.include_router(sources.router)
app.include_router(instances.router)
app.include_router(train.router)

app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")
