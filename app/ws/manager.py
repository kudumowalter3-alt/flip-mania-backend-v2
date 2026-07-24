"""
Live multiplayer runtime. One RoomHub exists per *active* room (created on
first WS connection, torn down when empty) and owns:

- the actual WebSocket connections for that room
- reconnect grace periods (a dropped connection keeps its seat for
  RECONNECT_GRACE_SECONDS before being evicted)
- heartbeat/ping-pong + rolling latency per player
- ready state, host authority, rematch voting
- the authoritative GameSession for that room

This is the piece Priority 1 is actually about, so it's fully wired end
to end and exercised by tests/test_multiplayer.py against a live server
— not just unit-tested in isolation.
"""
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import WebSocket
from sqlalchemy.orm import Session

from app.core.config import settings
from app.crud.room import set_room_host, set_room_status
from app.database import SessionLocal
from app.models.room import RoomStatus
from app.ws.events import ClientEvent, ServerEvent, envelope
from app.ws.game_session import GamePhase, GameSession


@dataclass
class PlayerConn:
    user_id: int
    username: str
    seat_index: int | None
    is_spectator: bool
    websocket: WebSocket
    connected: bool = True
    latency_ms: float | None = None
    last_pong_at: float = field(default_factory=time.time)
    heartbeat_task: asyncio.Task | None = None
    disconnect_task: asyncio.Task | None = None


class RoomHub:
    def __init__(self, room_id: int, host_id: int):
        self.room_id = room_id
        self.host_id = host_id
        self.conns: dict[int, PlayerConn] = {}
        self.game = GameSession(room_id=room_id)
        self.lock = asyncio.Lock()
        self.chat_history: deque = deque(maxlen=50)  # small in-memory ring buffer; not persisted
        self.typing: dict[int, bool] = {}
        self.last_host_snapshot: dict | None = None  # so a late-joining/reconnecting player can catch up

    # ---------- membership ----------

    async def connect(
        self, user_id: int, username: str, seat_index: int | None,
        is_spectator: bool, websocket: WebSocket,
    ):
        await websocket.accept()
        async with self.lock:
            existing = self.conns.get(user_id)
            reconnecting = existing is not None and not existing.connected

            if existing and existing.disconnect_task and not existing.disconnect_task.done():
                existing.disconnect_task.cancel()

            conn = PlayerConn(
                user_id=user_id, username=username, seat_index=seat_index,
                is_spectator=is_spectator, websocket=websocket,
            )
            self.conns[user_id] = conn
            conn.heartbeat_task = asyncio.create_task(self._heartbeat_loop(user_id))

            if not is_spectator and user_id not in self.game.players:
                self.game.add_player(user_id, username, seat_index)
            elif user_id in self.game.players:
                self.game.players[user_id].is_connected = True

        # Always give the newly (re)connected socket a full snapshot first.
        await self.send_to(user_id, ServerEvent.ROOM_SNAPSHOT, self._room_snapshot())
        if self.chat_history:
            await self.send_to(user_id, ServerEvent.CHAT_HISTORY, {"messages": list(self.chat_history)})
        if self.last_host_snapshot is not None:
            await self.send_to(user_id, ServerEvent.HOST_STATE_UPDATE, {"snapshot": self.last_host_snapshot})

        if reconnecting:
            await self.broadcast(
                ServerEvent.PLAYER_RECONNECTED, {"user_id": user_id, "username": username},
            )
        else:
            await self.broadcast(
                ServerEvent.PLAYER_JOINED,
                {
                    "user_id": user_id, "username": username,
                    "seat_index": seat_index, "is_spectator": is_spectator,
                },
                exclude={user_id},
            )

    async def disconnect(self, user_id: int):
        """Called when the socket drops (network loss, tab close, etc).
        The seat is held for a grace period in case they reconnect."""
        async with self.lock:
            conn = self.conns.get(user_id)
            if not conn:
                return
            conn.connected = False
            if conn.heartbeat_task:
                conn.heartbeat_task.cancel()
            if user_id in self.game.players:
                self.game.players[user_id].is_connected = False
            conn.disconnect_task = asyncio.create_task(self._grace_evict(user_id))

        await self.broadcast(
            ServerEvent.PLAYER_DISCONNECTED,
            {"user_id": user_id, "grace_seconds": settings.RECONNECT_GRACE_SECONDS},
            exclude={user_id},
        )

    async def _grace_evict(self, user_id: int):
        try:
            await asyncio.sleep(settings.RECONNECT_GRACE_SECONDS)
        except asyncio.CancelledError:
            return  # they reconnected in time
        await self._remove_player(user_id, notify_type=ServerEvent.PLAYER_LEFT)

    async def leave(self, user_id: int):
        """Voluntary leave — no grace period."""
        conn = self.conns.get(user_id)
        if conn and conn.disconnect_task and not conn.disconnect_task.done():
            conn.disconnect_task.cancel()
        await self._remove_player(user_id, notify_type=ServerEvent.PLAYER_LEFT)

    async def _remove_player(self, user_id: int, notify_type: str):
        async with self.lock:
            conn = self.conns.pop(user_id, None)
            if conn and conn.heartbeat_task:
                conn.heartbeat_task.cancel()
            was_host = user_id == self.host_id
            self.game.remove_player(user_id)

            new_host_id = None
            if was_host and self.conns:
                new_host_id = next(iter(self.conns.keys()))
                self.host_id = new_host_id
                self._persist_host_transfer(new_host_id)

        await self.broadcast(notify_type, {"user_id": user_id})
        if new_host_id is not None:
            await self.broadcast(ServerEvent.HOST_CHANGED, {"new_host_id": new_host_id})
        if not self.conns:
            room_manager.maybe_teardown(self.room_id)

    # ---------- heartbeat / latency ----------

    async def _heartbeat_loop(self, user_id: int):
        try:
            while True:
                await asyncio.sleep(settings.HEARTBEAT_INTERVAL_SECONDS)
                conn = self.conns.get(user_id)
                if not conn or not conn.connected:
                    return
                if time.time() - conn.last_pong_at > settings.HEARTBEAT_TIMEOUT_SECONDS:
                    # No response for too long — treat like a network drop.
                    await self.disconnect(user_id)
                    return
                await self.send_to(user_id, ServerEvent.SERVER_PING, {})
        except asyncio.CancelledError:
            return

    async def handle_ping(self, user_id: int, client_ts: float):
        """Client-initiated latency probe. Also counts as proof-of-life,
        same as a server_ping response would, so it resets the timeout."""
        conn = self.conns.get(user_id)
        if not conn:
            return
        now = time.time()
        conn.last_pong_at = now
        await self.send_to(user_id, ServerEvent.PONG, {"client_ts": client_ts, "server_ts": now})

    async def handle_latency_report(self, user_id: int, rtt_ms: float):
        """Client reports the round-trip time it measured from a ping/pong
        pair, so other players (and the server) know this player's latency."""
        conn = self.conns.get(user_id)
        if not conn:
            return
        conn.last_pong_at = time.time()
        conn.latency_ms = rtt_ms
        await self.broadcast(
            ServerEvent.LATENCY_UPDATE, {"user_id": user_id, "latency_ms": round(rtt_ms, 1)}
        )

    # ---------- ready / start / rematch / host ----------

    async def handle_ready(self, user_id: int, is_ready: bool):
        if user_id not in self.game.players:
            return  # spectators don't have ready state
        self._ready_state = getattr(self, "_ready_state", {})
        self._ready_state[user_id] = is_ready
        await self.broadcast(ServerEvent.READY_STATE, {"user_id": user_id, "is_ready": is_ready})

    def _all_ready(self) -> bool:
        ready_state = getattr(self, "_ready_state", {})
        active_players = [uid for uid, p in self.game.players.items()]
        if len(active_players) < 2:
            return False
        return all(ready_state.get(uid, False) for uid in active_players)

    async def handle_start_game(self, user_id: int):
        if user_id != self.host_id:
            await self.send_to(user_id, ServerEvent.ERROR, {"message": "Only the host can start the game"})
            return
        if not self._all_ready():
            await self.send_to(user_id, ServerEvent.ERROR, {"message": "Not all players are ready"})
            return
        self.game.start()
        self._persist_room_status(RoomStatus.IN_PROGRESS)
        await self.broadcast(ServerEvent.GAME_STARTED, self.game.snapshot())

    async def handle_game_action(self, user_id: int, action: str, payload: dict):
        try:
            if action == "roll":
                roll = self.game.roll(user_id)
                event = ServerEvent.GAME_OVER if self.game.phase == GamePhase.FINISHED else ServerEvent.GAME_STATE
                data = self.game.snapshot()
                data["last_roll_by"] = user_id
                await self.broadcast(event, data)
                if self.game.phase == GamePhase.FINISHED:
                    self._persist_room_status(RoomStatus.FINISHED)
            elif action == "end_turn":
                self.game.end_turn(user_id)
                await self.broadcast(ServerEvent.GAME_STATE, self.game.snapshot())
            else:
                await self.send_to(user_id, ServerEvent.ERROR, {"message": f"Unknown action '{action}'"})
        except ValueError as e:
            await self.send_to(user_id, ServerEvent.ERROR, {"message": str(e)})

    async def handle_rematch_vote(self, user_id: int, vote: bool):
        self.game.rematch_votes[user_id] = vote
        active_players = list(self.game.players.keys())
        votes = self.game.rematch_votes
        await self.broadcast(
            ServerEvent.REMATCH_STATUS,
            {"votes": {str(k): v for k, v in votes.items()}, "needed": len(active_players)},
        )
        if active_players and all(votes.get(uid) for uid in active_players):
            self.game.reset_for_rematch()
            self._persist_room_status(RoomStatus.LOBBY)
            self._ready_state = {uid: False for uid in active_players}
            await self.broadcast(ServerEvent.ROOM_SNAPSHOT, self._room_snapshot())

    async def handle_transfer_host(self, user_id: int, target_user_id: int):
        if user_id != self.host_id:
            await self.send_to(user_id, ServerEvent.ERROR, {"message": "Only the host can transfer host"})
            return
        if target_user_id not in self.conns:
            await self.send_to(user_id, ServerEvent.ERROR, {"message": "Target player is not in the room"})
            return
        self.host_id = target_user_id
        self._persist_host_transfer(target_user_id)
        await self.broadcast(ServerEvent.HOST_CHANGED, {"new_host_id": target_user_id})

    # ---------- chat / reactions ----------

    async def handle_chat_send(self, user_id: int, text: str):
        text = (text or "").strip()
        if not text:
            return
        text = text[:500]  # basic length cap, no other moderation yet
        conn = self.conns.get(user_id)
        username = conn.username if conn else str(user_id)
        message = {
            "user_id": user_id,
            "username": username,
            "text": text,
            "ts": time.time(),
        }
        self.chat_history.append(message)
        await self.broadcast(ServerEvent.CHAT_MESSAGE, message)

    async def handle_typing(self, user_id: int, is_typing: bool):
        self.typing[user_id] = is_typing
        await self.broadcast(
            ServerEvent.TYPING_UPDATE, {"user_id": user_id, "is_typing": is_typing}, exclude={user_id}
        )

    async def handle_reaction(self, user_id: int, emoji: str):
        # Ephemeral — no persistence, no rate limiting yet (fine for a
        # handful of players in one room; would need throttling at scale).
        if not emoji or len(emoji) > 8:
            return
        await self.broadcast(ServerEvent.REACTION_BROADCAST, {"user_id": user_id, "emoji": emoji})

    # ---------- real Flip Mania engine relay (host-authoritative) ----------
    # The generic dice-stub GameSession above stays as the Priority-1 proof
    # of authoritative networking. The real game (script.js) runs unmodified
    # on the host's browser; the host pushes snapshots here, which we relay
    # to everyone else and cache so a reconnecting/late-joining player can
    # catch up immediately without waiting for the next host tick.

    async def handle_host_state(self, user_id: int, snapshot: dict):
        if user_id != self.host_id:
            await self.send_to(user_id, ServerEvent.ERROR, {"message": "Only the host can push game state"})
            return
        self.last_host_snapshot = snapshot
        await self.broadcast(ServerEvent.HOST_STATE_UPDATE, {"snapshot": snapshot}, exclude={user_id})

    async def handle_remote_action(self, user_id: int, action_data: dict):
        # Any player (including spectators, for now — the host UI is
        # expected to validate whose turn it actually is) can send this;
        # it's only ever forwarded to whoever the current host is.
        host_conn = self.conns.get(self.host_id)
        if not host_conn:
            return
        payload = {"user_id": user_id, **action_data}
        await self.send_to(self.host_id, ServerEvent.REMOTE_ACTION_FORWARD, payload)

    # ---------- persistence helpers (small, synchronous, sqlite is fast) ----------

    def _persist_host_transfer(self, new_host_id: int):
        db: Session = SessionLocal()
        try:
            from app.crud.room import get_room
            room = get_room(db, self.room_id)
            if room:
                set_room_host(db, room, new_host_id)
        finally:
            db.close()

    def _persist_room_status(self, status: RoomStatus):
        db: Session = SessionLocal()
        try:
            from app.crud.room import get_room
            room = get_room(db, self.room_id)
            if room:
                set_room_status(db, room, status)
        finally:
            db.close()

    # ---------- messaging ----------

    async def send_to(self, user_id: int, event_type: str, data: dict):
        conn = self.conns.get(user_id)
        if conn and conn.connected:
            await conn.websocket.send_json(envelope(event_type, data))

    async def broadcast(self, event_type: str, data: dict, exclude: set[int] | None = None):
        exclude = exclude or set()
        msg = envelope(event_type, data)
        for uid, conn in list(self.conns.items()):
            if uid in exclude or not conn.connected:
                continue
            try:
                await conn.websocket.send_json(msg)
            except Exception:
                pass  # a dead socket here will be cleaned up by its own receive loop

    def _room_snapshot(self) -> dict:
        return {
            "room_id": self.room_id,
            "host_id": self.host_id,
            "players": [
                {
                    "user_id": c.user_id, "username": c.username,
                    "seat_index": c.seat_index, "is_spectator": c.is_spectator,
                    "connected": c.connected, "latency_ms": c.latency_ms,
                    "is_ready": getattr(self, "_ready_state", {}).get(c.user_id, False),
                }
                for c in self.conns.values()
            ],
            "game": self.game.snapshot(),
        }


class RoomManager:
    """Registry of RoomHub instances, one per currently-active room."""

    def __init__(self):
        self.hubs: dict[int, RoomHub] = {}

    def get_or_create(self, room_id: int, host_id: int) -> RoomHub:
        if room_id not in self.hubs:
            self.hubs[room_id] = RoomHub(room_id, host_id)
        return self.hubs[room_id]

    def get(self, room_id: int) -> RoomHub | None:
        return self.hubs.get(room_id)

    def maybe_teardown(self, room_id: int):
        hub = self.hubs.get(room_id)
        if hub and not hub.conns:
            del self.hubs[room_id]


room_manager = RoomManager()


class GlobalChatHub:
    """One shared chat channel for the whole lobby (not tied to any room).
    Same ring-buffer-history pattern as room chat, just global scope."""

    def __init__(self):
        self.conns: dict[int, PlayerConn] = {}
        self.history: deque = deque(maxlen=100)

    async def connect(self, user_id: int, username: str, websocket: WebSocket):
        await websocket.accept()
        conn = PlayerConn(
            user_id=user_id, username=username, seat_index=None,
            is_spectator=True, websocket=websocket,
        )
        self.conns[user_id] = conn
        if self.history:
            await self.send_to(user_id, ServerEvent.CHAT_HISTORY, {"messages": list(self.history)})

    def disconnect(self, user_id: int):
        self.conns.pop(user_id, None)

    async def handle_chat_send(self, user_id: int, text: str):
        text = (text or "").strip()[:500]
        if not text:
            return
        conn = self.conns.get(user_id)
        username = conn.username if conn else str(user_id)
        message = {"user_id": user_id, "username": username, "text": text, "ts": time.time()}
        self.history.append(message)
        await self.broadcast(ServerEvent.CHAT_MESSAGE, message)

    async def send_to(self, user_id: int, event_type: str, data: dict):
        conn = self.conns.get(user_id)
        if conn and conn.connected:
            await conn.websocket.send_json(envelope(event_type, data))

    async def broadcast(self, event_type: str, data: dict, exclude: set[int] | None = None):
        exclude = exclude or set()
        msg = envelope(event_type, data)
        for uid, conn in list(self.conns.items()):
            if uid in exclude:
                continue
            try:
                await conn.websocket.send_json(msg)
            except Exception:
                pass


global_chat_hub = GlobalChatHub()
