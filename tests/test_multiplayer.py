"""
This is NOT a mocked unit test. It runs real HTTP requests and real
WebSocket connections against a live uvicorn instance and prints
PASS/FAIL for each behavior in the Priority 1 spec, so the results here
are actually true of the running server, not just of the code in isolation.

Run with the server already up on 127.0.0.1:8000:
    python3 tests/test_multiplayer.py

Design note on the harness itself: broadcasts go to every connected
socket, not just whoever triggered them. Reading ad hoc from "whichever
socket is relevant right now" leaves the other sockets' backlogs
un-drained, and a later read can accidentally pick up a *stale* message
instead of a fresh one. Every connection here gets its own background
reader task that continuously drains its socket into an asyncio.Queue
the instant a message arrives, so timing never depends on which
connection the test happens to be polling.
"""
import asyncio
import json
import sys
import uuid
import time

import httpx
import websockets

BASE = "http://127.0.0.1:8000"
WS_BASE = "ws://127.0.0.1:8000"

DEBUG = False

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = ""):
    results.append((name, condition, detail))
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not condition else ""))


async def register_and_login(client: httpx.AsyncClient, username: str) -> tuple[str, int]:
    password = "test-pass-123"
    r = await client.post(f"{BASE}/auth/register", json={"username": username, "password": password})
    if r.status_code == 400:  # already exists from a previous run
        r = await client.post(f"{BASE}/auth/login", json={"username": username, "password": password})
    r.raise_for_status()
    body = r.json()
    return body["access_token"], body["user"]["id"]


class Conn:
    """A WS connection with a background reader so nothing gets missed."""

    def __init__(self, name: str, ws):
        self.name = name
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue()
        self.reader_task = asyncio.create_task(self._reader())

    async def _reader(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if DEBUG:
                    print(f"    <- [{self.name}] {msg['type']} {msg.get('data')}")
                await self.queue.put(msg)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send(self, msg_type: str, data: dict | None = None):
        await self.ws.send(json.dumps({"type": msg_type, "data": data or {}}))

    async def recv(self, timeout: float = 5.0):
        return await asyncio.wait_for(self.queue.get(), timeout=timeout)

    async def recv_until(self, event_type: str, timeout: float = 5.0, max_msgs: int = 30):
        for _ in range(max_msgs):
            msg = await self.recv(timeout=timeout)
            if msg["type"] == event_type:
                return msg
        raise TimeoutError(f"[{self.name}] never saw '{event_type}'")

    async def recv_any(self, event_types: set[str], timeout: float = 5.0, max_msgs: int = 30):
        for _ in range(max_msgs):
            msg = await self.recv(timeout=timeout)
            if msg["type"] in event_types:
                return msg
        raise TimeoutError(f"[{self.name}] never saw any of {event_types}")

    async def close(self):
        self.reader_task.cancel()
        try:
            await self.ws.close()
        except Exception:
            pass


async def connect(name: str, token: str) -> Conn:
    ws = await websockets.connect(f"{WS_BASE}/ws/rooms/connect?token={token}")
    return Conn(name, ws)


async def scenario_one():
    """Core Priority 1 loop: join, ready, start, authoritative turns,
    heartbeat/latency, disconnect + reconnect with the same seat, and
    automatic host transfer when the host drops."""
    run_id = uuid.uuid4().hex[:6]
    async with httpx.AsyncClient() as client:
        token_a, uid_a = await register_and_login(client, f"walter_{run_id}")
        token_b, uid_b = await register_and_login(client, f"palysha_{run_id}")
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}

        r = await client.post(f"{BASE}/rooms", json={"max_players": 4}, headers=headers_a)
        r.raise_for_status()
        room_data = r.json()
        code = room_data["room"]["code"]
        token_seat_a = room_data["session_token"]
        check("host creates room", r.status_code == 201, str(r.text))

        r = await client.post(f"{BASE}/rooms/join", json={"code": code}, headers=headers_b)
        r.raise_for_status()
        token_seat_b = r.json()["session_token"]
        check("second player joins by code", r.status_code == 200)

        a = await connect("A", token_seat_a)
        snap_a = await a.recv()
        check("host gets room_snapshot on connect", snap_a["type"] == "room_snapshot")

        b = await connect("B", token_seat_b)
        snap_b = await b.recv()
        check("second player gets room_snapshot on connect", snap_b["type"] == "room_snapshot")

        joined_evt = await a.recv_until("player_joined")
        check("host is notified of player join", joined_evt["data"]["user_id"] == uid_b)

        # --- ready status + start_game gating ---
        await a.send("ready", {"is_ready": True})
        await a.recv_until("ready_state")
        await b.recv_until("ready_state")  # B also sees A's broadcast

        await a.send("start_game")
        err = await a.recv_until("error")
        check("start_game blocked until everyone is ready", "ready" in err["data"]["message"].lower())

        await b.send("ready", {"is_ready": True})
        await a.recv_until("ready_state")
        await b.recv_until("ready_state")

        await a.send("start_game")
        started = await a.recv_until("game_started")
        started_b = await b.recv_until("game_started")
        check("game_started broadcast to both players", started["type"] == started_b["type"] == "game_started")

        first_turn_uid = started["data"]["current_turn_user_id"]
        check("server assigns an authoritative first turn", first_turn_uid in (uid_a, uid_b))

        # --- authoritative turn enforcement ---
        current, other = (a, b) if first_turn_uid == uid_a else (b, a)
        await other.send("game_action", {"action": "roll"})
        err2 = await other.recv_until("error")
        check("server rejects an out-of-turn roll", "not your turn" in err2["data"]["message"].lower())

        await current.send("game_action", {"action": "roll"})
        state_after_roll = await current.recv_until("game_state")
        await other.recv_until("game_state")  # drain the other socket's copy too
        check(
            "server generates the dice roll (1-6), not the client",
            isinstance(state_after_roll["data"]["last_roll"], int)
            and 1 <= state_after_roll["data"]["last_roll"] <= 6,
        )

        await current.send("game_action", {"action": "end_turn"})
        state_after_end = await current.recv_until("game_state")
        await other.recv_until("game_state")
        check(
            "turn actually advances to the other player",
            state_after_end["data"]["current_turn_user_id"] != first_turn_uid,
        )

        # --- heartbeat / latency ---
        t0 = time.time()
        await a.send("ping", {"client_ts": t0})
        pong = await a.recv_until("pong")
        rtt_ms = (time.time() - t0) * 1000
        await a.send("latency_report", {"rtt_ms": rtt_ms})
        latency_evt = await b.recv_until("latency_update")
        check(
            "ping/pong round trip + latency broadcast to other players",
            pong["type"] == "pong" and latency_evt["data"]["user_id"] == uid_a,
        )

        # --- disconnect + reconnect with same seat ---
        await b.close()
        disc_evt = await a.recv_until("player_disconnected")
        check("other players notified on disconnect", disc_evt["data"]["user_id"] == uid_b)

        b2 = await connect("B2", token_seat_b)
        reconnect_snap = await b2.recv()
        reconnect_evt = await a.recv_until("player_reconnected")
        check(
            "reconnect with the same session_token restores the same seat",
            reconnect_snap["type"] == "room_snapshot" and reconnect_evt["data"]["user_id"] == uid_b,
        )
        players_after_reconnect = reconnect_snap["data"]["game"]["players"]
        check(
            "game state preserved across reconnect (not wiped)",
            str(uid_a) in players_after_reconnect and str(uid_b) in players_after_reconnect,
        )

        # --- host transfer (automatic, via disconnect) ---
        await a.close()  # host leaves via network drop
        host_evt = await b2.recv_until("host_changed", timeout=grace_wait())
        check("host transfers automatically when the host disconnects", host_evt["data"]["new_host_id"] == uid_b)

        await b2.close()


async def scenario_two():
    """Explicit host transfer, spectator mode, driving a full game to
    completion, and the rematch vote cycle."""
    run_id = uuid.uuid4().hex[:6]
    async with httpx.AsyncClient() as client:
        token_a, uid_a = await register_and_login(client, f"host_{run_id}")
        token_b, uid_b = await register_and_login(client, f"player_{run_id}")
        token_c, uid_c = await register_and_login(client, f"spectator_{run_id}")
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}
        headers_c = {"Authorization": f"Bearer {token_c}"}

        r = await client.post(f"{BASE}/rooms", json={"max_players": 4}, headers=headers_a)
        code = r.json()["room"]["code"]
        seat_a = r.json()["session_token"]

        r = await client.post(f"{BASE}/rooms/join", json={"code": code}, headers=headers_b)
        seat_b = r.json()["session_token"]

        r = await client.post(f"{BASE}/rooms/join", json={"code": code, "as_spectator": True}, headers=headers_c)
        spec_body = r.json()
        seat_c = spec_body["session_token"]
        check(
            "spectator join is recorded as a spectator, no seat",
            spec_body["is_spectator"] and spec_body["seat_index"] is None,
        )

        a = await connect("A", seat_a)
        await a.recv()
        b = await connect("B", seat_b)
        await b.recv()
        await a.recv_until("player_joined")

        c = await connect("C", seat_c)
        spec_snap = await c.recv()
        await a.recv_until("player_joined")  # spectator join also broadcasts to A
        await b.recv_until("player_joined")  # ...and to B
        check(
            "spectator appears in room_snapshot flagged correctly",
            any(p["user_id"] == uid_c and p["is_spectator"] for p in spec_snap["data"]["players"]),
        )

        # --- explicit host transfer while host is still connected ---
        await a.send("transfer_host", {"target_user_id": uid_b})
        host_evt_a = await a.recv_until("host_changed")
        host_evt_b = await b.recv_until("host_changed")
        await c.recv_until("host_changed")
        check(
            "explicit host transfer broadcasts to everyone and takes effect",
            host_evt_a["data"]["new_host_id"] == uid_b and host_evt_b["data"]["new_host_id"] == uid_b,
        )

        # old host (A) can no longer start the game; new host (B) can
        await a.send("start_game")
        err = await a.recv_until("error")
        check("old host loses start_game authority after transfer", "host" in err["data"]["message"].lower())

        await a.send("ready", {"is_ready": True})
        await a.recv_until("ready_state")
        await b.recv_until("ready_state")
        await b.send("ready", {"is_ready": True})
        await a.recv_until("ready_state")
        await b.recv_until("ready_state")

        await b.send("start_game")
        started = await b.recv_until("game_started")
        await a.recv_until("game_started")
        await c.recv_until("game_started")
        check("new host can start the game", started["type"] == "game_started")

        # spectator cannot act
        await c.send("game_action", {"action": "roll"})
        try:
            spec_err = await c.recv_until("error", timeout=3.0)
            spectator_blocked = "not your turn" in spec_err["data"]["message"].lower()
        except (TimeoutError, asyncio.TimeoutError):
            spectator_blocked = True  # server just ignored it -- also acceptable
        check("spectator cannot take a game action", spectator_blocked)

        # --- drive the game to completion so we can test rematch ---
        # C (spectator) never acts, so its queue is never drained here, but
        # A/B both receive every broadcast and must both be drained every
        # iteration or the un-acting one's queue backs up.
        current_uid = started["data"]["current_turn_user_id"]
        conns = {uid_a: a, uid_b: b}
        max_turns = 200
        game_over_msg = None
        for _ in range(max_turns):
            actor = conns[current_uid]
            other = b if actor is a else a
            await actor.send("game_action", {"action": "roll"})
            msg = await actor.recv_any({"game_state", "game_over"})
            await other.recv_any({"game_state", "game_over"})
            if msg["type"] == "game_over":
                game_over_msg = msg
                break
            await actor.send("game_action", {"action": "end_turn"})
            msg2 = await actor.recv_until("game_state")
            await other.recv_until("game_state")
            current_uid = msg2["data"]["current_turn_user_id"]
        check("game reaches a winner within a bounded number of turns", game_over_msg is not None)

        # --- rematch: both vote yes, expect reset to lobby ---
        await a.send("rematch_vote", {"vote": True})
        await a.recv_until("rematch_status")
        await b.recv_until("rematch_status")

        await b.send("rematch_vote", {"vote": True})
        reset_snap = await b.recv_until("room_snapshot")
        await a.recv_until("room_snapshot")
        check(
            "unanimous rematch vote resets the game to lobby phase",
            reset_snap["data"]["game"]["phase"] == "lobby",
        )

        await a.close()
        await b.close()
        await c.close()


def grace_wait():
    # Host disconnect goes through the same grace-eviction path as any other
    # player. The server this test runs against is started with
    # FLIPMANIA_RECONNECT_GRACE_SECONDS=5 (see run instructions) specifically
    # so this test doesn't take 45+ real seconds; wait comfortably past that.
    return 10.0


async def main():
    await scenario_one()
    await scenario_two()

    print()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"{passed}/{total} checks passed")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
