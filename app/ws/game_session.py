"""
Authoritative, in-memory game state for one active room.

Scope note (being honest about what this is): this is a real, working
turn-based state machine — turn order, server-generated dice rolls,
server-side rejection of out-of-turn actions, and a win condition — but
it is a generic stand-in board, NOT a full port of Flip Mania's actual
rules (Zammer deck, dungeon bottle-flip, keys, portals). Porting the
real ruleset from script.js into a server-authoritative form is real
follow-up work, tracked as its own TODO item, not something to fake here.
Everything network/lifecycle-related (turns, rolls, win check, rematch,
host authority over start) is genuinely authoritative and anti-cheat in
the sense that only the server ever decides the roll or whose turn it is.
"""
import random
import time
from dataclasses import dataclass, field
from enum import Enum


BOARD_SIZE = 38  # matches the real Flip Mania board's 38 spaces
LAPS_TO_WIN = 2


class GamePhase(str, Enum):
    LOBBY = "lobby"
    IN_PROGRESS = "in_progress"
    FINISHED = "finished"


@dataclass
class PlayerState:
    user_id: int
    username: str
    seat_index: int
    position: int = 0
    laps: int = 0
    is_connected: bool = True


@dataclass
class GameSession:
    room_id: int
    phase: GamePhase = GamePhase.LOBBY
    players: dict[int, PlayerState] = field(default_factory=dict)   # user_id -> state
    turn_order: list[int] = field(default_factory=list)             # user_ids, seat order
    turn_index: int = 0
    last_roll: int | None = None
    winner_user_id: int | None = None
    started_at: float | None = None
    rematch_votes: dict[int, bool] = field(default_factory=dict)

    # --- setup ---
    def add_player(self, user_id: int, username: str, seat_index: int) -> None:
        self.players[user_id] = PlayerState(user_id, username, seat_index)

    def remove_player(self, user_id: int) -> None:
        self.players.pop(user_id, None)
        self.turn_order = [uid for uid in self.turn_order if uid != user_id]
        if self.turn_index >= len(self.turn_order):
            self.turn_index = 0

    def current_turn_user_id(self) -> int | None:
        if not self.turn_order:
            return None
        return self.turn_order[self.turn_index % len(self.turn_order)]

    def start(self) -> None:
        self.turn_order = sorted(self.players.keys(), key=lambda uid: self.players[uid].seat_index)
        self.turn_index = 0
        self.phase = GamePhase.IN_PROGRESS
        self.started_at = time.time()
        self.winner_user_id = None
        self.last_roll = None

    # --- turn actions (server-authoritative) ---
    def roll(self, user_id: int) -> int:
        if self.phase != GamePhase.IN_PROGRESS:
            raise ValueError("Game is not in progress")
        if self.current_turn_user_id() != user_id:
            raise ValueError("Not your turn")

        roll = random.randint(1, 6)  # server rolls — client can never supply this
        self.last_roll = roll
        p = self.players[user_id]
        p.position += roll
        if p.position >= BOARD_SIZE:
            p.position -= BOARD_SIZE
            p.laps += 1
            if p.laps >= LAPS_TO_WIN:
                self.phase = GamePhase.FINISHED
                self.winner_user_id = user_id
        return roll

    def end_turn(self, user_id: int) -> None:
        if self.phase != GamePhase.IN_PROGRESS:
            raise ValueError("Game is not in progress")
        if self.current_turn_user_id() != user_id:
            raise ValueError("Not your turn")
        if not self.turn_order:
            return
        self.turn_index = (self.turn_index + 1) % len(self.turn_order)

    # --- rematch ---
    def reset_for_rematch(self) -> None:
        for p in self.players.values():
            p.position = 0
            p.laps = 0
        self.phase = GamePhase.LOBBY
        self.winner_user_id = None
        self.last_roll = None
        self.rematch_votes = {}

    # --- serialization ---
    def snapshot(self) -> dict:
        return {
            "phase": self.phase.value,
            "players": {
                str(uid): {
                    "user_id": p.user_id,
                    "username": p.username,
                    "seat_index": p.seat_index,
                    "position": p.position,
                    "laps": p.laps,
                    "is_connected": p.is_connected,
                }
                for uid, p in self.players.items()
            },
            "turn_order": self.turn_order,
            "current_turn_user_id": self.current_turn_user_id(),
            "last_roll": self.last_roll,
            "winner_user_id": self.winner_user_id,
        }
