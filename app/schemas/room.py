from pydantic import BaseModel

from app.models.room import RoomStatus


class RoomCreate(BaseModel):
    max_players: int = 4
    is_private: bool = False
    allow_spectators: bool = True


class RoomJoin(BaseModel):
    code: str
    as_spectator: bool = False


class RoomPlayerOut(BaseModel):
    user_id: int
    username: str
    seat_index: int | None
    is_spectator: bool
    is_ready: bool

    class Config:
        from_attributes = True


class RoomOut(BaseModel):
    id: int
    code: str
    host_id: int
    status: RoomStatus
    max_players: int
    is_private: bool
    allow_spectators: bool

    class Config:
        from_attributes = True


class RoomJoinResult(BaseModel):
    room: RoomOut
    session_token: str
    seat_index: int | None
    is_spectator: bool
