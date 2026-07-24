import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, DateTime, Boolean, ForeignKey, Enum as SAEnum
)
from sqlalchemy.orm import relationship

from app.database import Base


class RoomStatus(str, enum.Enum):
    LOBBY = "lobby"
    IN_PROGRESS = "in_progress"
    FINISHED = "finished"


class Room(Base):
    """
    A Flip Mania room/match. Live, moment-to-moment game state (board
    positions, turn order, dice results) is NOT stored here — that lives
    in the in-memory GameSession (app/ws/game_session.py) while the room
    is active, because it changes many times a second and doesn't need
    to survive a server restart. This row tracks the durable stuff:
    who owns the room, its code, and its lobby/finished status.
    """
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(8), unique=True, index=True, nullable=False)
    host_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(SAEnum(RoomStatus), default=RoomStatus.LOBBY, nullable=False)
    max_players = Column(Integer, default=4)
    is_private = Column(Boolean, default=False)
    allow_spectators = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    host = relationship("User", back_populates="hosted_rooms", foreign_keys=[host_id])
    players = relationship("RoomPlayer", back_populates="room", cascade="all, delete-orphan")


class RoomPlayer(Base):
    """
    A user's membership row in a room. Ready state and seat assignment
    are durable (survive reconnects); live socket/latency state is kept
    in the in-memory ConnectionManager, not here.
    """
    __tablename__ = "room_players"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    seat_index = Column(Integer, nullable=True)  # None while spectating
    is_spectator = Column(Boolean, default=False)
    is_ready = Column(Boolean, default=False)
    session_token = Column(String(64), unique=True, nullable=False)
    joined_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    room = relationship("Room", back_populates="players")
    user = relationship("User")
