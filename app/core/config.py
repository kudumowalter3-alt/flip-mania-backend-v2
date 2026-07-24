"""
Central configuration for the Flip Mania backend.

Nothing here reads from a real .env yet — sane dev defaults only.
Swap SECRET_KEY / DATABASE_URL via environment variables in production.
"""
import os


class Settings:
    PROJECT_NAME: str = "Flip Mania Backend"

    # --- Auth ---
    SECRET_KEY: str = os.getenv("FLIPMANIA_SECRET_KEY", "dev-secret-change-me")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24h

    # --- Database ---
    DATABASE_URL: str = os.getenv("FLIPMANIA_DATABASE_URL", "sqlite:///./flipmania.db")

    # --- Rooms / Multiplayer ---
    MAX_PLAYERS_PER_ROOM: int = 4
    ROOM_CODE_LENGTH: int = 5
    RECONNECT_GRACE_SECONDS: int = int(os.getenv("FLIPMANIA_RECONNECT_GRACE_SECONDS", "45"))
    HEARTBEAT_INTERVAL_SECONDS: float = 8.0    # server -> client ping cadence
    HEARTBEAT_TIMEOUT_SECONDS: float = 25.0    # no pong in this long => treat as dropped
    REMATCH_VOTE_TIMEOUT_SECONDS: float = 30.0


settings = Settings()
