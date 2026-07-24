from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.crud.room import (
    add_player_to_room, create_room, get_room_by_code
)
from app.database import get_db
from app.models.room import Room, RoomStatus
from app.models.user import User
from app.routers.deps import get_current_user
from app.schemas.room import RoomCreate, RoomJoin, RoomJoinResult, RoomOut
from app.ws.manager import room_manager

router = APIRouter(prefix="/rooms", tags=["rooms"])


@router.get("")
def list_public_rooms(db: Session = Depends(get_db)):
    """Lobby listing: open (non-private) rooms still in the lobby phase,
    with a live player count pulled from the in-memory hub where available."""
    rooms = (
        db.query(Room)
        .filter(Room.is_private == False, Room.status == RoomStatus.LOBBY)  # noqa: E712
        .order_by(Room.created_at.desc())
        .limit(50)
        .all()
    )
    result = []
    for room in rooms:
        hub = room_manager.get(room.id)
        player_count = len(hub.conns) if hub else None
        result.append({
            "code": room.code,
            "host_id": room.host_id,
            "max_players": room.max_players,
            "allow_spectators": room.allow_spectators,
            "player_count": player_count,
        })
    return result


@router.post("", response_model=RoomJoinResult, status_code=status.HTTP_201_CREATED)
def create_new_room(
    room_in: RoomCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = create_room(db, host_id=current_user.id, room_in=room_in)
    player = add_player_to_room(db, room, current_user.id, as_spectator=False)
    return RoomJoinResult(
        room=RoomOut.model_validate(room),
        session_token=player.session_token,
        seat_index=player.seat_index,
        is_spectator=player.is_spectator,
    )


@router.post("/join", response_model=RoomJoinResult)
def join_room(
    join_in: RoomJoin,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = get_room_by_code(db, join_in.code)
    if not room:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")
    if join_in.as_spectator and not room.allow_spectators:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Spectating disabled for this room")

    player = add_player_to_room(db, room, current_user.id, as_spectator=join_in.as_spectator)
    return RoomJoinResult(
        room=RoomOut.model_validate(room),
        session_token=player.session_token,
        seat_index=player.seat_index,
        is_spectator=player.is_spectator,
    )


@router.get("/{code}", response_model=RoomOut)
def get_room_info(code: str, db: Session = Depends(get_db)):
    room = get_room_by_code(db, code)
    if not room:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")
    return room
