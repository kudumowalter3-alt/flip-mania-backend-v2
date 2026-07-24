import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.database import Base, engine
from app.models import user, room  # noqa: F401
from app.routers import auth, users, rooms, websocket


# Create database tables
Base.metadata.create_all(bind=engine)


# Create FastAPI app
app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0"
)


# Allow frontend connection
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://flipmamia-v2.netlify.app",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# API routes
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(rooms.router)
app.include_router(websocket.router)


# Health check
@app.get("/health")
def health_check():
    return {"status": "ok"}
