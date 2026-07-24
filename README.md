# Flip Mania Backend

FastAPI backend for Flip Mania's online multiplayer, chat, and lobby.
Built fresh for this project (the game itself has no server). This
covers **Priority 1 (multiplayer)** plus **chat, emoji reactions, and
lobby listing**, built and verified against a live server — not just
written and assumed to work.

## Run it

```bash
pip install -r requirements.txt --break-system-packages
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then, with the server running:

```bash
python3 tests/test_multiplayer.py           # Priority 1: 24/24
python3 tests/test_integration_features.py  # chat/reactions/relay: 11/11
```

Both are real integration tests — real HTTP + WebSocket traffic against
your actually-running server, not mocks. See the root `DEPLOYMENT.md`
for deploying this to Render.

## Structure

```
app/
  main.py          FastAPI app, router wiring, env-based CORS, table creation
  core/              config.py (tunables), security.py (bcrypt + JWT)
  database.py         SQLAlchemy engine/session (SQLite by default, Postgres via env)
  models/              User, Room, RoomPlayer
  schemas/              Pydantic request/response shapes
  crud/                 DB access functions
  routers/
    auth.py               POST /auth/register, /auth/login
    users.py               GET /users/me
    rooms.py                 POST /rooms, POST /rooms/join, GET /rooms (lobby list), GET /rooms/{code}
    websocket.py              Room WS endpoint + global chat WS endpoint
  ws/
    events.py               Wire protocol: message envelope + event type constants
    game_session.py           Priority-1 proof-of-concept authoritative dice/turn state machine
    manager.py                 RoomHub (connections, heartbeat, reconnect, ready/start/rematch/
                                host-transfer, chat, reactions, host-state relay) + RoomManager +
                                GlobalChatHub
tests/
  test_multiplayer.py         Priority 1 end-to-end (24 checks)
  test_integration_features.py  Chat/reactions/relay/lobby-listing (11 checks)
render.yaml / Procfile          Render deployment
```

## What's genuinely done

**Priority 1 (multiplayer):**
- Auth: register/login, JWT bearer tokens
- Room create/join over REST; each seat gets its own `session_token`
- A single WS endpoint (`/ws/rooms/connect?token=...`) that's actually
  server-authoritative: join/leave/**reconnect with the same seat**,
  heartbeat + latency, ready/start gating, **server-rolled dice that
  rejects out-of-turn actions**, spectator mode, rematch voting, host
  transfer (explicit and automatic-on-disconnect)

**Chat, reactions, lobby (this pass):**
- Room chat + global (site-wide) chat, each with its own WS channel and
  a small in-memory history buffer (last 50/100 messages — not
  persisted to DB; a restart clears it)
- Typing indicators
- Emoji reactions — ephemeral, broadcast-only, no persistence (by design,
  these are meant to float and disappear)
- `GET /rooms` — public lobby listing (open, non-private, still-in-lobby
  rooms with a live player count)
- **Host-authoritative game-state relay** (`host_state` / `remote_action`
  events) — this is the bridge used by the actual Flip Mania frontend
  integration: the room host runs the real, unmodified game engine and
  broadcasts snapshots; other players' clients relay their actions back
  through the host. This exists *alongside*, not instead of, the
  Priority-1 dice-stub above — see the frontend's docs for why.

## What's NOT done

- The generic dice-stub `game_session.py` remains a proof-of-concept for
  authoritative networking, not the real Flip Mania ruleset — the real
  game runs client-side on the host's browser (see frontend README).
- Chat has no persistence, moderation, or rate limiting beyond a length
  cap — fine for a small player base, would need real work before public
  launch.
- Ranked matchmaking, leaderboards, achievements, friends/social graph,
  country-channel chat, and the rest of the longer-term priority list are
  not started.
- Bearer tokens are long-lived (24h) with no refresh flow.
- Single-instance in-memory state (`RoomManager`, `GlobalChatHub`) — this
  will not survive a multi-instance/autoscaled deployment without adding
  a shared backing store (e.g. Redis pub/sub) for cross-instance
  broadcast. Fine for one Render instance; a real scaling pass would need
  to address this explicitly.
