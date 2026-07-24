import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.database import Base, engine
from app.models import user, room  # noqa: F401 — register models with Base before create_all
from app.routers import auth, users, rooms, websocket

Base.metadata.create_all(bind=engine)

app = FastAPI(title=settings.PROJECT_NAME)

_origins_env = os.getenv("FLIPMANIA_ALLOWED_ORIGINS", "*")
_allow_origins = ["*"] if _origins_env.strip() == "*" else [o.strip() for o in _origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=False,  # we use bearer tokens, not cookies, so this isn't needed
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(rooms.router)
app.include_router(websocket.router)


@app.get("/health")
def health_check():
    return {"status": "ok"}


# --- Not-yet-built routers (Priorities 2-8), left as explicit TODOs so ---
# --- nothing here silently pretends to exist before it's actually built ---
# app.include_router(chat.router)        # Priority 3 — not built
# app.include_router(social.router)      # Priority 2 — not built
# app.include_router(leaderboard.router) # Priority 8 — not built
