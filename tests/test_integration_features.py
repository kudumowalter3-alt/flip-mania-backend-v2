import asyncio
import json
import uuid

import httpx
import websockets

BASE = "http://127.0.0.1:8000"
WS_BASE = "ws://127.0.0.1:8000"

results = []


def check(name, cond):
    results.append((name, cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")


async def register(client, username):
    r = await client.post(f"{BASE}/auth/register", json={"username": username, "password": "test-pass-123"})
    r.raise_for_status()
    b = r.json()
    return b["access_token"], b["user"]["id"]


async def main():
    run_id = uuid.uuid4().hex[:6]
    async with httpx.AsyncClient() as client:
        token_a, uid_a = await register(client, f"host_{run_id}")
        token_b, uid_b = await register(client, f"guest_{run_id}")
        ha = {"Authorization": f"Bearer {token_a}"}
        hb = {"Authorization": f"Bearer {token_b}"}

        r = await client.post(f"{BASE}/rooms", json={"max_players": 4}, headers=ha)
        room = r.json()
        code = room["room"]["code"]
        seat_a = room["session_token"]

        r = await client.post(f"{BASE}/rooms/join", json={"code": code}, headers=hb)
        seat_b = r.json()["session_token"]

        # public listing shows it (still in lobby)
        r = await client.get(f"{BASE}/rooms")
        listing = r.json()
        check("room appears in public listing", any(x["code"] == code for x in listing))

        ws_a = await websockets.connect(f"{WS_BASE}/ws/rooms/connect?token={seat_a}")
        await ws_a.recv()
        ws_b = await websockets.connect(f"{WS_BASE}/ws/rooms/connect?token={seat_b}")
        await ws_b.recv()
        await ws_a.recv()  # player_joined

        # --- chat ---
        await ws_a.send(json.dumps({"type": "chat_send", "data": {"text": "hi from host"}}))
        msg_a = json.loads(await ws_a.recv())
        msg_b = json.loads(await ws_b.recv())
        check("chat message broadcast to sender and others", msg_a["type"] == "chat_message" and msg_b["type"] == "chat_message")
        check("chat message text preserved", msg_b["data"]["text"] == "hi from host")

        # --- typing ---
        await ws_a.send(json.dumps({"type": "typing", "data": {"is_typing": True}}))
        typing_evt = json.loads(await ws_b.recv())
        check("typing indicator relayed (not to sender)", typing_evt["type"] == "typing_update" and typing_evt["data"]["is_typing"])

        # --- reaction ---
        await ws_b.send(json.dumps({"type": "reaction", "data": {"emoji": "🔥"}}))
        reaction_a = json.loads(await ws_a.recv())
        reaction_b = json.loads(await ws_b.recv())
        check("reaction broadcast to everyone including sender", reaction_a["data"]["emoji"] == "🔥" and reaction_b["data"]["emoji"] == "🔥")

        # --- host state relay ---
        fake_snapshot = {"players": [{"id": 0, "position": 5}], "current": 0}
        await ws_a.send(json.dumps({"type": "host_state", "data": {"snapshot": fake_snapshot}}))
        state_update = json.loads(await ws_b.recv())
        check("host_state relayed to non-host", state_update["type"] == "host_state_update" and state_update["data"]["snapshot"] == fake_snapshot)

        # non-host cannot push host_state
        await ws_b.send(json.dumps({"type": "host_state", "data": {"snapshot": {}}}))
        err = json.loads(await ws_b.recv())
        check("non-host blocked from pushing host_state", err["type"] == "error")

        # --- remote_action forwarded only to host ---
        await ws_b.send(json.dumps({"type": "remote_action", "data": {"action": "roll"}}))
        fwd = json.loads(await ws_a.recv())
        check("remote_action forwarded to host", fwd["type"] == "remote_action_forward" and fwd["data"]["action"] == "roll" and fwd["data"]["user_id"] == uid_b)

        # --- late-joining player gets chat history + last host snapshot ---
        r = await client.post(f"{BASE}/rooms/join", json={"code": code, "as_spectator": True}, headers=ha)
        # (host re-joining as another seat isn't realistic; instead register a third user)
        token_c, uid_c = await register(client, f"late_{run_id}")
        hc = {"Authorization": f"Bearer {token_c}"}
        r = await client.post(f"{BASE}/rooms/join", json={"code": code, "as_spectator": True}, headers=hc)
        seat_c = r.json()["session_token"]
        ws_c = await websockets.connect(f"{WS_BASE}/ws/rooms/connect?token={seat_c}")
        snap = json.loads(await ws_c.recv())
        history = json.loads(await ws_c.recv())
        host_state_catchup = json.loads(await ws_c.recv())
        check("late joiner receives chat history", history["type"] == "chat_history" and len(history["data"]["messages"]) >= 1)
        check("late joiner receives cached host snapshot", host_state_catchup["type"] == "host_state_update" and host_state_catchup["data"]["snapshot"] == fake_snapshot)

        await ws_a.close(); await ws_b.close(); await ws_c.close()

        # --- global chat ---
        gws_a = await websockets.connect(f"{WS_BASE}/ws/chat/global?token={token_a}")
        gws_b = await websockets.connect(f"{WS_BASE}/ws/chat/global?token={token_b}")
        # drain any chat_history sent immediately on connect (this server may
        # have accumulated global chat history from earlier runs/tests)
        for ws in (gws_a, gws_b):
            try:
                await asyncio.wait_for(ws.recv(), timeout=0.3)
            except asyncio.TimeoutError:
                pass
        await gws_a.send(json.dumps({"type": "chat_send", "data": {"text": "hello world"}}))
        g_msg_b = json.loads(await gws_b.recv())
        check("global chat message received by other connected user", g_msg_b["data"]["text"] == "hello world")
        await gws_a.close(); await gws_b.close()

    print()
    passed = sum(1 for _, ok in results if ok)
    print(f"{passed}/{len(results)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
