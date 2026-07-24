import secrets
import string

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.room import Room, RoomPlayer, RoomStatus
from app.schemas.room import RoomCreate


def _generate_code(db: Session) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(settings.ROOM_CODE_LENGTH))
        if not db.query(Room).filter(Room.code == code).first():
            return code


def _generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def create_room(db: Session, host_id: int, room_in: RoomCreate) -> Room:
    room = Room(
        code=_generate_code(db),
        host_id=host_id,
        max_players=room_in.max_players,
        is_private=room_in.is_private,
        allow_spectators=room_in.allow_spectators,
        status=RoomStatus.LOBBY,
    )
    db.add(room)
    db.commit()
    db.refresh(room)
    return room


def get_room_by_code(db: Session, code: str) -> Room | None:
    return db.query(Room).filter(Room.code == code.upper()).first()


def get_room(db: Session, room_id: int) -> Room | None:
    return db.query(Room).filter(Room.id == room_id).first()


def get_player_in_room(db: Session, room_id: int, user_id: int) -> RoomPlayer | None:
    return (
        db.query(RoomPlayer)
        .filter(RoomPlayer.room_id == room_id, RoomPlayer.user_id == user_id)
        .first()
    )


def get_player_by_session_token(db: Session, token: str) -> RoomPlayer | None:
    return db.query(RoomPlayer).filter(RoomPlayer.session_token == token).first()


def next_free_seat(db: Session, room: Room) -> int | None:
    taken = {
        rp.seat_index
        for rp in db.query(RoomPlayer).filter(
            RoomPlayer.room_id == room.id, RoomPlayer.is_spectator == False  # noqa: E712
        )
        if rp.seat_index is not None
    }
    for i in range(room.max_players):
        if i not in taken:
            return i
    return None


def add_player_to_room(
    db: Session, room: Room, user_id: int, as_spectator: bool = False
) -> RoomPlayer:
    existing = get_player_in_room(db, room.id, user_id)
    if existing:
        return existing

    seat_index = None
    is_spectator = as_spectator
    if not as_spectator:
        seat_index = next_free_seat(db, room)
        if seat_index is None:
            # Room full as a player -> fall back to spectator if allowed
            is_spectator = True

    player = RoomPlayer(
        room_id=room.id,
        user_id=user_id,
        seat_index=seat_index,
        is_spectator=is_spectator,
        session_token=_generate_session_token(),
    )
    db.add(player)
    db.commit()
    db.refresh(player)
    return player


def get_room_players(db: Session, room_id: int) -> list[RoomPlayer]:
    return db.query(RoomPlayer).filter(RoomPlayer.room_id == room_id).all()


def remove_player(db: Session, player: RoomPlayer) -> None:
    db.delete(player)
    db.commit()


def set_ready(db: Session, player: RoomPlayer, is_ready: bool) -> RoomPlayer:
    player.is_ready = is_ready
    db.commit()
    db.refresh(player)
    return player


def set_room_status(db: Session, room: Room, status: RoomStatus) -> Room:
    room.status = status
    db.commit()
    db.refresh(room)
    return room


def set_room_host(db: Session, room: Room, new_host_id: int) -> Room:
    room.host_id = new_host_id
    db.commit()
    db.refresh(room)
    return room
