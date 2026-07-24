from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import relationship

from app.database import Base


class User(Base):
    """
    Minimal account record needed to identify a player across sessions.
    Profile fields (avatar/country/XP/etc — Priority 5) intentionally
    live in a separate PlayerProfile table so this table stays about
    identity/auth only. PlayerProfile is not built yet; see TODO.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(32), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, default=True)

    hosted_rooms = relationship("Room", back_populates="host", foreign_keys="Room.host_id")
