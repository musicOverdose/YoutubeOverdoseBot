import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from src.core.config import settings
from src.core.database import Base, engine
from src.core.logger import setup_logger
from src.core.security import hash_password
from src.web.routes.ai_routes import router as ai_router
from src.web.routes.audit_routes import router as audit_router
from src.web.routes.auth_routes import router as auth_router
from src.web.routes.cache_routes import router as cache_router
from src.web.routes.cookies_routes import router as cookies_router
from src.web.routes.dashboard_routes import router as dashboard_router
from src.web.routes.job_routes import router as job_router
from src.web.routes.logs_routes import router as logs_router
from src.web.routes.must_join_routes import router as must_join_router
from src.web.routes.queue_routes import router as queue_router
from src.web.routes.settings_routes import router as settings_router
from src.web.routes.system_routes import router as system_router
from src.web.routes.telegram_routes import router as telegram_router
from src.web.routes.user_routes import router as user_router

logger = setup_logger("web_app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database schema if needed...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        try:
            await conn.execute(text("ALTER TABLE job_requests ADD COLUMN IF NOT EXISTS menu_message_id BIGINT;"))
        except Exception as e:
            logger.debug("Column menu_message_id addition check: %s", e)

    # Initialize or preserve admin credentials in PostgreSQL
    from src.services.auth_service import AuthService
    await AuthService.init_admin_credentials()

    # Startup state reconciliation (authoritative ACTIVE DB -> runtime files & READY gating)
    from src.services.setting_service import SettingService
    try:
        await SettingService.load_all_settings_to_runtime()
        await SettingService.reconcile_startup_state()
    except Exception as e:
        logger.error("Failed startup state reconciliation: %s", e)

    yield
    logger.info("Shutting down web application...")


app = FastAPI(
    title="Telegram YouTube Downloader Web Admin",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(system_router)
app.include_router(telegram_router)
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(queue_router)
app.include_router(job_router)
app.include_router(cache_router)
app.include_router(user_router)
app.include_router(must_join_router)
app.include_router(cookies_router)
app.include_router(ai_router)
app.include_router(settings_router)
app.include_router(logs_router)
app.include_router(audit_router)

# Mount static files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def serve_index():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        response = FileResponse(index_file)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    return {"status": "ok", "message": "Admin API is running. Access /docs for Swagger UI."}
