"""
Single WebSocket entrypoint for real-time multiplayer.

Connect to:  ws://host/ws/rooms/connect?token=<session_token>

`session_token` comes from POST /rooms or POST /rooms/join (REST). Using
the same token to reconnect is exactly what makes reconnect-after-drop
and reconnect-with-the-same-seat work — the token IS the seat.
"""
import json

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.crud.room import get_player_by_session_token, get_room
from app.crud.user import get_user
from app.database import SessionLocal
from app.ws.events import ClientEvent
from app.ws.manager import global_chat_hub, room_manager

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/chat/global")
async def global_chat_connect(websocket: WebSocket, token: str = Query(...)):
    """Site-wide lobby chat. Authenticated with the normal JWT access
    token from /auth/login (not a room session_token — there's no room)."""
    db: Session = SessionLocal()
    try:
        user_id_str = decode_access_token(token)
        if not user_id_str:
            await websocket.close(code=4001, reason="Invalid or expired token")
            return
        user = get_user(db, int(user_id_str))
        if not user:
            await websocket.close(code=4001, reason="User not found")
            return
        user_id, username = user.id, user.username
    finally:
        db.close()

    await global_chat_hub.connect(user_id, username, websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == ClientEvent.CHAT_SEND:
                await global_chat_hub.handle_chat_send(user_id, (msg.get("data") or {}).get("text", ""))
    except WebSocketDisconnect:
        global_chat_hub.disconnect(user_id)


@router.websocket("/ws/rooms/connect")
async def rooms_connect(websocket: WebSocket, token: str = Query(...)):
    db: Session = SessionLocal()
    try:
        player = get_player_by_session_token(db, token)
        if not player:
            await websocket.close(code=4001, reason="Invalid session token")
            return
        room = get_room(db, player.room_id)
        if not room:
            await websocket.close(code=4004, reason="Room no longer exists")
            return

        user_id = player.user_id
        username = player.user.username
        seat_index = player.seat_index
        is_spectator = player.is_spectator
        room_id = room.id
        host_id = room.host_id
    finally:
        db.close()

    hub = room_manager.get_or_create(room_id, host_id)
    await hub.connect(user_id, username, seat_index, is_spectator, websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")
            data = msg.get("data", {}) or {}

            if msg_type == ClientEvent.READY:
                await hub.handle_ready(user_id, bool(data.get("is_ready", False)))
            elif msg_type == ClientEvent.START_GAME:
                await hub.handle_start_game(user_id)
            elif msg_type == ClientEvent.GAME_ACTION:
                await hub.handle_game_action(user_id, data.get("action", ""), data.get("payload", {}))
            elif msg_type == ClientEvent.PING:
                await hub.handle_ping(user_id, float(data.get("client_ts", 0)))
            elif msg_type == ClientEvent.LATENCY_REPORT:
                await hub.handle_latency_report(user_id, float(data.get("rtt_ms", 0)))
            elif msg_type == ClientEvent.REMATCH_VOTE:
                await hub.handle_rematch_vote(user_id, bool(data.get("vote", False)))
            elif msg_type == ClientEvent.TRANSFER_HOST:
                await hub.handle_transfer_host(user_id, int(data.get("target_user_id")))
            elif msg_type == ClientEvent.LEAVE:
                await hub.leave(user_id)
                room_manager.maybe_teardown(room_id)
                break
            elif msg_type == ClientEvent.CHAT_SEND:
                await hub.handle_chat_send(user_id, data.get("text", ""))
            elif msg_type == ClientEvent.TYPING:
                await hub.handle_typing(user_id, bool(data.get("is_typing", False)))
            elif msg_type == ClientEvent.REACTION:
                await hub.handle_reaction(user_id, data.get("emoji", ""))
            elif msg_type == ClientEvent.HOST_STATE:
                await hub.handle_host_state(user_id, data.get("snapshot", {}))
            elif msg_type == ClientEvent.REMOTE_ACTION:
                await hub.handle_remote_action(user_id, data)
            # unknown message types are silently ignored rather than
            # dropping the connection — forward compatible with future
            # client versions sending event types this server predates

    except WebSocketDisconnect:
        await hub.disconnect(user_id)
        # Note: we do NOT tear down the hub here — the grace-period task
        # (`_grace_evict`) is responsible for final removal + teardown
        # check, since the player might reconnect before it fires.
