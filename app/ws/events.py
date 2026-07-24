"""
The WS wire protocol. Every message, either direction, is JSON:

    {"type": "<event_type>", "data": {...}}

Server -> client messages additionally carry "server_ts" (epoch seconds,
float) so the client can do its own clock-skew-aware latency math if it
wants to, on top of the ping/pong round trip the server already tracks.
"""
import time
from typing import Any


# --- client -> server ---
class ClientEvent:
    READY = "ready"                  # {is_ready: bool}
    START_GAME = "start_game"        # {} — host only, all non-spectators ready
    GAME_ACTION = "game_action"      # {action: "roll" | "end_turn", payload?: {...}}
    PING = "ping"                    # {client_ts: float} — client-initiated latency probe
    LATENCY_REPORT = "latency_report"  # {rtt_ms: float} — client reports the RTT it measured
    REMATCH_VOTE = "rematch_vote"    # {vote: bool}
    TRANSFER_HOST = "transfer_host"  # {target_user_id: int} — host only
    LEAVE = "leave"                  # {} — voluntary leave, distinct from a network drop

    # --- chat / reactions ---
    CHAT_SEND = "chat_send"          # {text: str}
    TYPING = "typing"                # {is_typing: bool}
    REACTION = "reaction"            # {emoji: str} — ephemeral floating reaction, gameplay or chat

    # --- real Flip Mania engine relay (host-authoritative) ---
    # The generic GAME_ACTION/GAME_STATE pair above belongs to the
    # Priority-1 proof-of-concept dice stub. The actual Flip Mania
    # engine (script.js) is far too stateful/interactive to reimplement
    # server-side in this pass, so instead the room's host runs the real
    # unmodified engine and relays snapshots; everyone else mirrors it.
    HOST_STATE = "host_state"        # {snapshot: {...}} — host only, full engine snapshot
    REMOTE_ACTION = "remote_action"  # {action: "roll", ...} — any player, forwarded to host only


# --- server -> client ---
class ServerEvent:
    ROOM_SNAPSHOT = "room_snapshot"
    PLAYER_JOINED = "player_joined"
    PLAYER_LEFT = "player_left"
    PLAYER_DISCONNECTED = "player_disconnected"
    PLAYER_RECONNECTED = "player_reconnected"
    READY_STATE = "ready_state"
    GAME_STARTED = "game_started"
    GAME_STATE = "game_state"
    GAME_OVER = "game_over"
    PONG = "pong"
    LATENCY_UPDATE = "latency_update"
    HOST_CHANGED = "host_changed"
    REMATCH_STATUS = "rematch_status"
    ERROR = "error"
    SERVER_PING = "server_ping"

    # --- chat / reactions ---
    CHAT_MESSAGE = "chat_message"
    CHAT_HISTORY = "chat_history"
    TYPING_UPDATE = "typing_update"
    REACTION_BROADCAST = "reaction_broadcast"

    # --- real engine relay ---
    HOST_STATE_UPDATE = "host_state_update"
    REMOTE_ACTION_FORWARD = "remote_action_forward"


def envelope(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"type": event_type, "data": data, "server_ts": time.time()}
