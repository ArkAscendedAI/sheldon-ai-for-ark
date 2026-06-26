"""WebSocket server — accepts connections from the game mod and mock clients.

This is the main entry point for the bridge. It:
1. Accepts WebSocket connections
2. Authenticates via shared token
3. Creates a session per player
4. Routes messages to the agent
5. Sends responses back
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
import re
import signal
import time
from collections import deque

import websockets
from websockets.asyncio.server import serve, ServerConnection

from sheldon_bridge.agent import Agent, AgentResult
from sheldon_bridge.auth import PlayerContext, RateLimiter, TokenAuthenticator
from sheldon_bridge.config import BridgeConfig
from sheldon_bridge.providers.llm import LLMProvider
from sheldon_bridge.session import SessionManager
from sheldon_bridge.tools.registry import ToolRegistry
from sheldon_bridge.db.manager import DBManager
from sheldon_bridge.db.assembler import ContextAssembler
from sheldon_bridge.db.campaign_store import CampaignStore
from sheldon_bridge.db.telemetry_store import TelemetryStore
from sheldon_bridge import census, gps
from sheldon_bridge.names import canonical_id
from sheldon_bridge.tls import build_ssl_context

# Import tool modules to trigger @tool registration
import sheldon_bridge.tools.knowledge  # noqa: F401
import sheldon_bridge.tools.actions  # noqa: F401
import sheldon_bridge.tools.memory  # noqa: F401

logger = logging.getLogger(__name__)

def _wire(payload) -> str:
    """Wire-format JSON: compact separators. The in-game mod detects message
    types by exact substring match (e.g. '"type":"reply"') — a space after
    the colon breaks every match silently."""
    return json.dumps(payload, separators=(",", ":"))


def _clean_reply(text: str) -> str:
    """Strip markdown/emoji the in-game font can't render (safety net behind the
    plain-text system-prompt instruction), then label it as Sheldon's."""
    import re
    t = text
    t = re.sub(r"```.*?```", "", t, flags=re.S)        # code fences
    t = re.sub(r"`([^`]*)`", r"\1", t)                  # inline code
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)         # headers
    t = re.sub(r"(?m)^\s*[-*_]{3,}\s*$", "", t)         # horizontal rules
    t = t.replace("**", "").replace("__", "")            # bold
    t = re.sub(r"(?m)^\s*[-*+]\s+", "", t)              # bullet markers
    t = "".join(ch for ch in t if ch == "\n" or 32 <= ord(ch) < 127)  # drop emoji/non-ASCII
    t = re.sub(r"\n{2,}", "  ", t).replace("\n", " ")   # collapse to a flowing line
    t = re.sub(r"\s{2,}", " ", t).strip()
    return f"Sheldon: {t}" if t else "Sheldon: ..."


def _reply_wire(body: str, target_eos: str = "") -> str:
    """Build the reply wire string for the cooked v6 mod.

    The mod (a) routes replies by Contains() against a literal substring that —
    due to an April-era double-escaping bug baked into the Blueprint — is
    `\\"type\\":\\"reply\\"` (backslashes are real characters), and (b) extracts
    the body by splitting on the COMPACT delimiters `"message":"` and `","type"`
    (added in v6). Neither is changeable without a cook, so the bridge emits a
    string that satisfies both: a compact body section for the splitter plus a
    backslash-escaped type marker as a tail for the matcher. Double-quotes in the
    body are mapped to single-quotes ('): the cooked mod has NO JSON unescape (it
    raw-Splits the body out), so a backslash-escaped `\\"` would DISPLAY literally
    (the `\\"Main Base\\"` bug, 2026-06-24) AND a literal `"` could form the
    `","type"` split delimiter early. A single-quote renders cleanly and can never
    form that delimiter — strictly safer than the escape it replaces. (The real fix
    — mod-side JSON unescape on reply render so true `"` survives — is a V20 item.)

    V18 Phase 2 (C3): the target survivor's EOS rides in a trailing `,"target":"<eos>"`
    field placed AFTER the `","type"` body-split delimiter, so the current cook's body
    extraction is unaffected (it ignores the tail) while the new OnRep_ReplyText filter
    Splits on `"target":"` to render the reply only on the requester's client — no more
    broadcasting every reply to everyone. Omitted when no target (back-compat)."""
    esc = body.replace('"', "'")  # cooked mod raw-Splits the body (no JSON unescape); '\\"' would show as backslashes — single-quote is delimiter-safe + clean
    tail = (',"target":"%s"' % target_eos) if target_eos else ''
    return '{"message":"' + esc + '","type":"reply",\\"type\\":\\"reply\\"' + tail + '}'


def _command_wire(command: str) -> str:
    """Frame a console command for the cooked mod's command branch (B-graft, 2026-06-14).

    Mirrors _reply_wire's hybrid scheme: the mod's EventGraph detects commands via
    Contains() against the backslash-escaped marker `\\"type\\":\\"command\\"` and extracts
    the command string by Split()ing on the compact delimiters `"message":"` and `","type"`.
    Fire-and-forget — the mod runs it through Execute Console Command (void) on the
    requesting player, and ARK enforces admin server-side, so there is no reply to await.

    Unlike _reply_wire, commands are NOT quote-escaped: ARK console commands use literal
    quotes for args (e.g. GiveItemToPlayer "Bob" "/Game/..." 1 0 false), and the mod's
    Split() keys on the exact `","type"` sequence — which a console command never contains —
    so quotes pass through intact and reach ExecuteConsoleCommand correctly.
    """
    return '{"message":"' + command + '","type":"command",\\"type\\":\\"command\\"}'


def _exec_command_wire(command: str, target_eos: str) -> str:
    """V21-1: frame an admin console command for the cooked `exec_command` branch (the redesign of
    the crash-prone server-side path, Gotcha #34). The mod's WS branch sets the replicated
    `ExecCommand` var = `<target_eos>|<command>`; ONLY the requester's client (whose local EOS
    matches target_eos) runs it via a CLIENT-side `ExecuteConsoleCommand` — where a real cheat
    manager exists, so it elevates + executes WITHOUT the dedicated-server crash the server-side
    path causes. Like `_command_wire`, the command is NOT quote-escaped (ARK console args use literal
    quotes; the mod Splits on `|` / the `","type"` delimiter, which a command never contains).
    `target_eos` is REQUIRED — an empty target would make the mod's `Contains(localEOS,"")` match
    EVERY client (broadcast footgun); the handler refuses empty. Hybrid marker scheme like the rest."""
    return ('{"message":"%s","type":"exec_command",\\"type\\":\\"exec_command\\","target":"%s"}'
            % (command, target_eos))


def _query_wire(request_type: str, eos: str = "", req_id: int | None = None) -> str:
    """Frame a getter query for a cooked getter branch (V9). Each getter's Contains() matches
    the backslash marker `\\"type\\":\\"<request_type>\\"`; the mod reads the requesting player's
    data and replies with clean JSON `{"type":"<reply_type>",...}`, which the bridge correlates
    (single-flight per reply type, per server link) in _handle_message. Generic over getters
    (get_vitals→vitals, get_position→position, ...).

    V18 Phase 2 (C2): the requesting survivor's EOS rides in the `message` field. The mod's
    resolve extracts it (the same Split(`"message":"` … `","type"`) convention as the pin/scan
    wires) to set CurrentRequesterPC to the ACTUAL requester rather than the last-seen PC —
    correct under concurrency. Harmless to a pre-Phase-2 cook: the message field is ignored by
    the type-matched getter, and the Phase-1 getter lock already serializes getters.

    V19-2 (LSA-04 / R-S1): an optional per-request `id` rides in a trailing `,"id":<n>` field
    placed AFTER the backslash `\\"type\\"` marker — exactly like _reply_wire's `target` tail, so
    the CURRENTLY-COOKED V18 mod (which Splits the message out of `"message":"` … `","type"` and
    Contains-matches the type marker) ignores it entirely. A future cook that echoes this id back
    in its reply lets the bridge correlate replies by (reply_type, id) and drop the global getter
    lock for throughput. Omitted when no id (back-compat / census)."""
    tail = (',"id":%d' % req_id) if req_id is not None else ''
    return ('{"message":"%s","type":"%s",\\"type\\":\\"%s\\"%s}'
            % (eos, request_type, request_type, tail))


def _pin_wire(op: str, pin_id: str = "", label: str = "",
              x: float = 0.0, y: float = 0.0, z: float = 0.0, target_eos: str = "") -> str:
    """Frame a map-pin actuator command for the cooked mod (V15 map-pin routing graft).

    Same hybrid Contains()/Split() scheme as _command_wire — the mod has no JSON parser. The
    server WS-receive graft detects pins via Contains(`\\"type\\":\\"map_pin\\"`) and extracts
    the pipe-delimited payload `op|id|label|x|y|z` by Split()ing on `"message":"` … `","type"`
    (the same pipe convention the getters use for list rows). It then routes to the requester's
    client (ServerSendExecCommandToPlayer → BPClientHandleNetExecCommand), which calls
    new_minimap_mark / remove_minimap_mark with CustomTag=id. Fire-and-forget — no reply.

    op ∈ {add, remove}. Coords are world cm (rounded — minimap marks don't need sub-cm). The
    label is sanitized to protect the Split() delimiters. `clear` is driven bridge-side as a
    per-pin remove (keeps the mod a dumb add/remove-by-tag primitive — no mod-side pin list)."""
    label = label.replace("|", "/").replace('"', "").replace("\\", "").replace("\n", " ")[:40]
    payload = "%s|%s|%s|%d|%d|%d" % (op, pin_id, label, round(x), round(y), round(z))
    # V19-5 (MAPPIN-01): a per-player RENDER target rides in a trailing `,"target":"<eos>"` AFTER the
    # type marker — outside the `"message"` payload, so the cooked V18 mod (which Splits only
    # `"message":"`…`","type"` into PinCommand) ignores it (live-safe). The V19 cook's WS MAP graft
    # Split()s `"target":"`…`"` out of the raw frame and APPENDS it to PinCommand as a 7th pipe field
    # (`op|id|label|x|y|z|target`); OnRep_PinCommand then Split()s the target back off and renders only
    # when Contains(localEOS, target) — same client-EOS gate as B4 OnRep_ReplyText. ALWAYS emit the
    # field (even empty) so the server-side Split is deterministic; empty target → Contains(eos,"")==true
    # in the cooked mod (see C2-05) → broadcast (every client renders, matching V18 behavior).
    tail = ',"target":"%s"' % target_eos
    return '{"message":"' + payload + '","type":"map_pin",\\"type\\":\\"map_pin\\"' + tail + '}'


def _scan_wire(x: float, y: float, z: float, radius: float, group: int) -> str:
    """Frame a dino-census octree scan for the cooked get_dino_scan branch (V15).

    Hybrid Contains()/Split() scheme like _pin_wire: the census Contains() matches
    `\\"type\\":\\"get_dino_scan\\"` and Split()s the payload out of "message" (between
    `"message":"` and `","type"`). Payload is `x,y,z,radius,group` — the octree center +
    radius + EServerOctreeGroup int (the bridge SWEEPS groups for stasis coverage). The
    mod replies `{"type":"dino_scan","data":"<rec>;<rec>;…"}`, correlated as reply_type
    'dino_scan'. Coords rounded to cm — the octree doesn't need sub-cm precision."""
    payload = "%d,%d,%d,%d,%d" % (round(x), round(y), round(z), round(radius), int(group))
    return '{"message":"' + payload + '","type":"get_dino_scan",\\"type\\":\\"get_dino_scan\\"}'


# reply msg_types the cooked getters send back (correlated by _handle_message)
_QUERY_REPLY_TYPES = {"vitals", "position", "inventory", "equipped", "tribe", "progression",
                      "players", "dinos", "look", "buffs", "engrams", "dino_scan", "world_time",
                      "command_result"}

# Unsolicited event frames from the mod's universal event buff → canonical kind. The V13
# buff sent "death_event"/"damage_event"; the V15 rebuild sends "death"/"damage". Accept
# BOTH spellings so a mod-side type-string choice can't silently drop the frame (the same
# class of bug that dropped the buffs/vitals getter replies). Canonical kind is kept stable
# (*_event) so get_recent_events consumers don't change.
_GAME_EVENT_TYPES = {"death": "death_event", "death_event": "death_event",
                     "damage": "damage_event", "damage_event": "damage_event",
                     # V18 A1 dino events (BP_SheldonEventBuff.BPOnTamedWildDino /
                     # BP_SheldonDinoEventBuff.BPInstigatorDied) — team/tribe-scoped, no EOS.
                     "dino_tamed": "dino_tamed", "dino_death": "dino_death"}

# Out-of-band state-push frames: NOT a per-call getter reply (no waiter), NOT an event. The
# mod answers a get_server_state request with {"type":"server_state", gps_lat_origin:…, …} —
# the live PrimalWorldSettings GPS constants for this map (V17 cook item 4). _handle_server_state
# writes them to telemetry.server_state, where gps.from_server_state + the assembler/find_dinos
# pick them up (inert until then — we never guess a GPS). Like getter replies these ride the
# SendPlayerMessage wrapper, so _extract_embedded_reply must admit them too.
_STATE_PUSH_TYPES = {"server_state"}

# get_server_state request retry on the server link: the mod's GPS branch may not be ready the
# instant the link comes up, and a PRE-V17 mod has no such branch at all. Ask a few times,
# stopping as soon as the constants land — or quietly give up (pre-cook mod never answers; the
# unmatched request is a no-op for the mod, exactly like the dormant census scans were).
_SERVER_STATE_TRIES = 5
_SERVER_STATE_RETRY_S = 6.0

# Bridge-side damage flood control (tunable, NO cook). The mod's baked per-survivor floor +
# per-buff rate-limit is the real LINK protection (bytes already sent can't be un-sent); these
# guard the BRIDGE itself so a raid-scale damage burst (e.g. 50 players taking boss AOE) can
# neither overwhelm the turn pipeline nor evict rare death events from the bounded ring.
_DAMAGE_RATE_CAP = 8             # max damage frames INGESTED per window; excess dropped + counted
_DAMAGE_RATE_WINDOW_S = 1.0
_DAMAGE_COALESCE_WINDOW_S = 5.0  # damage within this of the last stored damage → merged, not appended
_DAMAGE_MIN_AMOUNT = 25.0        # report floor: ignore trivial hits (post-mitigation). The MOD has no
#                                  amount threshold (only a per-survivor rate-limit) — it lives HERE so it
#                                  is tunable with NO cook. Raise it if low-stakes chip damage is noisy.

# Admission cap (M3): max player turns in flight (queued + running) at once. The semaphore
# caps how many turns hit the LLM simultaneously (8); this caps the BACKLOG so a flood can't
# spawn unbounded turn tasks. 8× the LLM concurrency = generous burst headroom before shedding.
_MAX_INFLIGHT_TURNS = 64

# Getter reply correlation (XDEL-01/02 fix). Replies carry no id, so a getter that TIMES OUT must
# not let its late reply be delivered to the NEXT player's waiter. A timed-out getter leaves a
# TOMBSTONE in its reply-type FIFO; the late reply is matched to the tombstone and dropped. The
# tombstone is purged after _GETTER_REPLY_LATENESS so a never-arriving (lost) reply can't eat the
# next getter's reply indefinitely. _GETTER_SEND_TIMEOUT caps the ws.send INSIDE the getter lock so
# a back-pressured write can't freeze every player's getters + the census (T6/SERVER-06).
_GETTER_REPLY_LATENESS = 12.0
_GETTER_SEND_TIMEOUT = 5.0
# LSA-02: a getter reply is silently dropped while the mod's IsAuthenticated gate is false (the
# reconnect→reauth window after any link blip / redeploy). On a getter TIMEOUT, retry exactly ONCE
# after this backoff (long enough for a typical reauth, short enough not to wedge the 8s getter) —
# bounded so it can never loop.
_GETTER_REAUTH_BACKOFF_S = 1.5
# Getter-contention mitigation (no-cook, 2026-06-24): the dino census shares _getter_lock with player
# getters (vitals/position/…), so a slow census scan can starve an interactive getter. If a player
# queried within _CENSUS_YIELD_WINDOW seconds, the census briefly defers (_CENSUS_YIELD_DEFER) before
# its next scan — it still scans (no tile skipped, no false removals), it just yields lock time during
# active chat. The real fix is the V20 req-id protocol (drop the lock for concurrent getters).
_CENSUS_YIELD_WINDOW = 8.0
_CENSUS_YIELD_DEFER = 3.0

# V21-3 reply pacing: min gap between successive reply-var sets on the single server link so each
# replicates before the next overwrites the shared ReplyText. 0.25s comfortably exceeds an always-
# relevant actor's net-update interval; it only delays BUNCHED replies (rare — replies are seconds
# apart), so single/normal replies are unaffected.
_REPLY_MIN_GAP_S = 0.25

# Per-turn wall-clock ceiling (TS-AGENT01 fix). agent.run() can otherwise loop MAX_ITERATIONS x
# failover x backoff with no bound and hold one of the 8 turn-semaphore slots for many minutes,
# starving every other player. Cap the whole turn so a wedged/slow turn always frees its slot.
_TURN_WALLCLOCK_TIMEOUT = 150.0

# Inbound chat-body cap (ADD-01 prompt-injection / DoS): the player fully controls the message body,
# which flows into the LLM context, lore search, and persistence. Bound it before any of that.
_MAX_MESSAGE_CHARS = 4000

# _team_tribe is learned from dino_tamed frames; cap it so it can't grow unbounded over a server
# lifetime (T12/LEAK-05). FIFO-evict the oldest team mappings past the cap.
_TEAM_TRIBE_MAX = 4096


class _GetterWaiter:
    """One pending single-flight getter reply (held under _getter_lock). `abandoned` marks a getter
    that TIMED OUT: its tombstone stays in the per-reply-type FIFO so the (possibly) late reply is
    consumed against it and DROPPED, never cross-delivered to the next requester. Tombstones older
    than _GETTER_REPLY_LATENESS are purged. Reference: XDEL-01/02 (V18 audit).

    `req_id` is the per-request correlation id stamped into _query_wire (V19-2). The CURRENTLY-COOKED
    mod does NOT echo it, so it is only USED when an incoming reply carries a matching echoed id; the
    tombstone-FIFO path is unchanged for the id-less cooked mod (back-compat)."""
    __slots__ = ("fut", "sent_at", "abandoned", "req_id")

    def __init__(self, fut: asyncio.Future, sent_at: float, req_id: int | None = None):
        self.fut = fut
        self.sent_at = sent_at
        self.abandoned = False
        self.req_id = req_id


def _extract_embedded_reply(raw: str) -> dict | None:
    """Recover a getter reply that rode inside a SendPlayerMessage wrapper (V10 fix).

    Root cause of the V9 getter blocker: an inline VictoryCore WebSocketSendMessage in the
    mod's EventGraph (ubergraph) does NOT transmit, while the identical call inside the
    SendPlayerMessage UFunction does (proven live: chat + the __telemetry heartbeat). So V10
    routes each getter reply through SendPlayerMessage, which wraps the payload as
        {"type":"player_message","message":"<PAYLOAD>","position":{...},"facing_yaw":0.0}
    WITHOUT escaping the payload's quotes — so the frame is not valid JSON and json.loads
    fails. Recover the embedded reply by the same split the cooked mod uses for replies
    (split on `"message":"` then `","position":`); <PAYLOAD> is valid JSON on its own.
    Returns the reply dict iff it's a known getter reply type OR a game-event frame, else
    None. Death/damage events ALSO ride SendPlayerMessage (wrapped -> invalid JSON ->
    recovered here), so the guard MUST admit _GAME_EVENT_TYPES or they're silently dropped:
    the clean json.loads dispatch never sees them because the wrapper is structurally invalid
    JSON. (Caught by the V15 de-risk pass — the guard previously allowed only getters.)
    """
    if '"type":"player_message"' not in raw or '"message":"' not in raw:
        return None
    payload = raw.split('"message":"', 1)[1].split('","position":', 1)[0].strip()
    if not payload.startswith("{"):
        return None
    try:
        d = json.loads(payload)
    except (ValueError, TypeError):
        return None
    return d if isinstance(d, dict) and d.get("type") in (
        _QUERY_REPLY_TYPES | set(_GAME_EVENT_TYPES) | _STATE_PUSH_TYPES) else None


def _recover_player_message(raw: str) -> dict | None:
    """A REAL player chat message whose text contains an unescaped `"` (or `\\`) breaks json.loads —
    the cooked mod wraps the body into the player_message frame WITHOUT escaping it, so e.g. a player
    typing `call it "Main Base"` produces structurally-invalid JSON that the recv loop would otherwise
    drop as [recv-badjson]. Recover it: re-escape JUST the message field (json.dumps) and re-parse the
    frame — which also restores position/facing. Returns the player_message dict, or None if this isn't
    a recoverable player_message. (Distinct from _extract_embedded_reply, which only recovers wrapped
    getter-replies / events whose payload is a `{...}` object.) The clean fix is mod-side escaping → V20."""
    if '"type":"player_message"' not in raw or '"message":"' not in raw or '","position":' not in raw:
        return None
    try:
        head, rest = raw.split('"message":"', 1)
        text, tail = rest.rsplit('","position":', 1)   # rsplit: the REAL position delim is always last
        fixed = head + '"message":' + json.dumps(text) + ',"position":' + tail
        d = json.loads(fixed)
    except (ValueError, TypeError):
        return None
    return d if isinstance(d, dict) and d.get("type") == "player_message" else None


class BridgeServer:
    """The main Sheldon Bridge server."""

    def __init__(self, config: BridgeConfig):
        self.config = config

        # Auth
        self.authenticator = TokenAuthenticator(config.shared_secret)

        # LLM
        self.llm = LLMProvider(config.llm)

        # Tools
        self.registry = ToolRegistry(tier_config=config.tiers or None)
        self.registry.discover()

        # Sessions & rate limiting
        self.sessions = SessionManager()
        self.rate_limiter = RateLimiter()

        # Concurrency: each player turn runs as its own task so the recv loop never
        # blocks (a getter's reply arrives mid-turn on the SAME multiplexed link and
        # must be readable, or its future times out). The semaphore caps simultaneous
        # LLM turns so a burst of players can't overwhelm the provider; _turn_tasks
        # keeps strong refs so tasks aren't GC'd mid-flight.
        self._turn_semaphore = asyncio.Semaphore(8)
        self._turn_tasks: set[asyncio.Task] = set()
        # Multi-user safety (V18 Phase 1). Getter replies correlate by reply-TYPE FIFO and carry
        # no id/eos, so two concurrent getters would cross-deliver (A gets B's vitals). Serialize
        # ALL link getters — player queries AND census scans — through one lock so at most one is
        # in flight, making the FIFO match unambiguous. The mod already serializes on the game
        # thread + shares one QueryAccum slot, so this matches reality; the per-request-id protocol
        # (Phase 2 / the V18 cook) restores throughput. _pin_lock serializes the admin map-pin
        # registry (RMW across an await).
        self._getter_lock = asyncio.Lock()
        self._pin_lock = asyncio.Lock()
        self._last_player_getter_ts = 0.0   # stamped on each player getter; the census yields to it (contention mitigation)
        # V21-3 reply pacing: all replies set the mod's SINGLE replicated `ReplyText` var, which UE
        # coalesces last-writer-wins — two replies within one net-update window clobber, so the loser's
        # requester never sees their reply. Serialize reply sends over the single server link with a
        # min gap so each value persists >= one net-update and replicates to its target before the next
        # overwrites it. (No-cook mitigation; the robust fix is a cook-side replicated reply queue/seq-id
        # — NetExec per-player delivery is NOT available here, the SSECTP route failed ownership.)
        self._reply_lock = asyncio.Lock()
        self._last_reply_send = 0.0
        # The single game-server multiplexed link (set on its connect, identity-checked-cleared on
        # its drop). Tracked EXPLICITLY — not via the _connections dict key "server-link" — so a
        # second link or a reconnect-before-cleanup can't collide on the literal key and
        # de-register the survivor (SCC-01), and so command/getter routing never falls back to a
        # player socket (F7). One link only → its census/state tasks are singletons (cancelled when
        # the link drops OR a new link connects).
        self._server_link_ws: ServerConnection | None = None
        self._census_task: asyncio.Task | None = None
        self._state_task: asyncio.Task | None = None

        # Agent
        self.agent = Agent(
            llm=self.llm,
            registry=self.registry,
            rate_limiter=self.rate_limiter,
            game_command_handler=self._game_command_handler,
        )

        # Persistence (Phase 0): per-cluster/server SQLite + the context assembler
        self.db = DBManager(
            config.data_root,
            default_cluster_id=config.default_cluster_id,
            default_server_id=config.default_server_id,
            server_ip_map=config.server_ip_map,
        )
        self.assembler = ContextAssembler(config.context_token_budget)

        # Recent game-events (death/damage/etc.) the mod's event buff pushes over the link.
        # Bounded ring; exposed to turns via extra_context so the LLM can answer "what killed
        # me?" / react to a recent death. Bridge-wide for now (the v1 death frame carries no
        # eos id; multi-player filtering arrives when the frame includes the survivor).
        self._recent_game_events: deque = deque(maxlen=20)
        # BUFFS-09: dino tame/death events get their OWN bounded ring (pull-only) so a death/damage
        # burst in the shared ring can't evict them before the owning tribe polls get_recent_dino_events.
        self._recent_dino_game_events: deque = deque(maxlen=20)
        # cluster_id -> {team id -> tribe name}, learned from dino_tamed frames (which carry both).
        # Lets a dino_death (which carries only the team id) be attributed to the owning tribe so the
        # get_recent_dino_events tool can filter by the asker's tribe. BUFFS-07: keyed PER CLUSTER so
        # a same-named tribe (or recycled team id) on a different cluster can't collide; the matcher
        # prefers the asker's NUMERIC team id over tribe-name string equality. Bounded per cluster.
        self._team_tribe: dict = {}
        self._damage_ingest_times: deque = deque(maxlen=64)  # sliding window for the damage rate cap
        self._damage_dropped: int = 0                        # damage frames dropped by the cap (stat)
        self._map_pins: dict = {}                            # bridge-tracked Sheldon map pins (id → meta)
        self._pin_counter: int = 0                           # monotonic id source for map pins
        self._getter_req_counter: int = 0                    # V19-2: monotonic getter request-id source

        # Track connected clients
        self._connections: dict[str, ServerConnection] = {}
        # Per-reply-type FIFO of pending getter waiters (_GetterWaiter). Cooked getters send id-less
        # {"type":"<reply>",...} back over the server link; correlate single-flight under
        # _getter_lock. A timed-out getter leaves a TOMBSTONE so its late reply is matched to IT and
        # dropped, never handed to the next player's waiter (XDEL-01 cross-delivery fix).
        self._query_waiters: dict[str, list[_GetterWaiter]] = {}
        # Continuous dino-census config (off by default; cook-gated — the get_dino_scan
        # branch must be live in the mod first). Drives a per-server background loop.
        self._census_cfg = census.CensusConfig(enabled=getattr(config, "census_enabled", False))

        logger.info(
            f"Bridge initialized: {len(self.registry.all_tools)} tools, "
            f"tiers={self.registry.tier_names}, "
            f"model={config.llm.litellm_model}"
        )

    def _cancel_link_tasks(self) -> None:
        """Cancel the server-link's singleton census + state tasks — called when the link drops or a
        new link supersedes it, so at most ONE census loop ever runs on the shared telemetry.db
        (SCC-03)."""
        for attr in ("_census_task", "_state_task"):
            t = getattr(self, attr, None)
            if t is not None and not t.done():
                t.cancel()
            setattr(self, attr, None)

    async def handle_connection(self, websocket: ServerConnection) -> None:
        """Handle a single WebSocket connection from the game mod or mock client."""
        player_id = None
        is_server_link = False
        try:
            # Step 1: Authenticate
            raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            auth_msg = json.loads(raw)

            if auth_msg.get("type") != "auth":
                await websocket.close(4001, "First message must be auth")
                return

            token = auth_msg.get("token", "")
            if not self.authenticator.validate_token(token):
                logger.warning(f"Auth failed from {websocket.remote_address}")
                await websocket.close(4001, "Authentication failed")
                return

            # Step 2: Create session
            player_data = auth_msg.get("player", {})
            # The game SERVER's mod component connects with empty identity vars
            # (server-multiplexed link; players ride on top of it). `or` catches
            # empty strings, not just missing keys — empty tier would otherwise
            # resolve to zero tools.
            raw_id = (player_data.get("player_id") or "").strip()
            is_server_link = not raw_id
            player = PlayerContext(
                player_id=raw_id or "server-link",
                display_name=(player_data.get("display_name") or "").strip() or "Survivor",
                # ADD-03: only the server-link's auth tier is honored. Any OTHER connection is forced
                # to 'player' — a client must not self-declare admin/superadmin at connect time.
                tier=((player_data.get("tier") or "").strip() or "player") if is_server_link else "player",
                tribe_id=player_data.get("tribe_id", ""),
                position=player_data.get("position", {}),
                facing_yaw=player_data.get("facing_yaw", 0.0),
            )
            player_id = player.player_id

            session = self.sessions.create(player)
            # Resolve cluster/server scope: explicit ids from auth once the mod sends
            # them (V9), else the source-IP map, else config defaults.
            remote_ip = websocket.remote_address[0] if websocket.remote_address else None
            session.scope = self.db.resolve_scope(
                remote_ip=remote_ip,
                server_id=(player_data.get("server_id") or "").strip() or None,
                cluster_id=(player_data.get("cluster_id") or "").strip() or None,
            )
            # Make sure this survivor exists in the cluster's campaign db.
            campaign = CampaignStore(await self.db.campaign(session.scope.cluster_id))
            await campaign.upsert_survivor(player_id, player.display_name, player.tier)
            # Track the link EXPLICITLY (not under the dict key "server-link" a 2nd link could
            # collide on, SCC-01). Players — which don't open their own WS today — go in _connections.
            if is_server_link:
                if self._server_link_ws is not None and self._server_link_ws is not websocket:
                    logger.warning("[server-link] a second game-server link connected — cancelling the "
                                   "previous link's census/state tasks so only one runs (SCC-03)")
                    self._cancel_link_tasks()
                self._server_link_ws = websocket
            else:
                self._connections[player_id] = websocket

            # Send auth success
            await websocket.send(_wire({
                "type": "auth_success",
                "player_id": player_id,
                "tier": player.tier,
                "tools_available": len(self.registry.get_tools_for_tier(player.tier)),
            }))

            if is_server_link:
                logger.info(
                    f"Game server connected (multiplexed link, anonymous session, "
                    f"tier={player.tier}, tools={len(self.registry.get_tools_for_tier(player.tier))})"
                )
                # Attach the continuous dino census to this server link (cook-gated; off
                # unless census_enabled). Cancelled in the finally when the link drops.
                if self._census_cfg.enabled:
                    self._census_task = asyncio.create_task(self._run_census(session.scope, websocket))
                    logger.info("Census enabled — dino-scan background loop attached")
                # Ask the mod for this map's GPS constants (bounded retry, self-terminating).
                # Inert on a pre-V17 mod (no get_server_state branch); fills server_state on V17+.
                self._state_task = asyncio.create_task(self._request_server_state(session.scope, websocket))
            else:
                logger.info(
                    f"Player connected: {player.display_name} ({player_id[:8]}...) "
                    f"tier={player.tier}"
                )

            # Step 3: Message loop
            async for raw_message in websocket:
                try:
                    msg = json.loads(raw_message)
                    await self._handle_message(msg, session, websocket)
                except json.JSONDecodeError:
                    # V10 getter replies ride inside a SendPlayerMessage wrapper (unescaped
                    # payload → not valid JSON). Recover + route them before treating as bad.
                    rep = _extract_embedded_reply(str(raw_message))
                    if rep is not None:
                        logger.info(f"[reply-embedded] {rep.get('type')}")  # V10 getter reply
                        await self._handle_message(rep, session, websocket)
                        continue
                    pm = _recover_player_message(str(raw_message))
                    if pm is not None:
                        # a real chat message whose text had an unescaped " broke json.loads; recovered
                        logger.info("[recv-recovered] player_message with unescaped quotes")
                        await self._handle_message(pm, session, websocket)
                        continue
                    logger.warning(f"[recv-badjson] {str(raw_message)[:400]}")  # TEMP DIAG
                    await websocket.send(_wire({
                        "type": "error",
                        "message": "Invalid JSON",
                    }))
                except Exception as e:
                    logger.error(f"Error handling message for {player_id}: {e}", exc_info=True)
                    await websocket.send(_wire({
                        "type": "error",
                        "message": "Internal error processing your request",
                    }))

        except asyncio.TimeoutError:
            logger.warning(f"Auth timeout from {websocket.remote_address}")
            await websocket.close(4002, "Auth timeout")
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"Connection error: {e}", exc_info=True)
        finally:
            # Only THIS link clears the slot + cancels its singleton tasks; a newer link that
            # superseded it must survive (identity-checked teardown — SCC-01).
            if is_server_link and self._server_link_ws is websocket:
                self._server_link_ws = None
                self._cancel_link_tasks()
            if player_id:
                if not is_server_link:
                    self._connections.pop(player_id, None)
                self.sessions.remove(player_id)

    async def _handle_message(
        self, msg: dict, session, websocket: ServerConnection
    ) -> None:
        """Route an incoming message to the appropriate handler."""
        msg_type = msg.get("type")

        if msg_type == "player_message":
            # Run the turn as its own task so this recv loop keeps reading the
            # multiplexed link. CRITICAL: a getter's reply arrives mid-turn on THIS
            # same connection — awaiting the turn inline parked the loop, so the reply
            # was unread until the turn ended and every getter's 8s future timed out
            # (the late reply was then dropped). Tasking also overlaps concurrent
            # players' turns instead of serializing them.
            # Admission cap (M3): shed load instead of spawning unbounded turn tasks under a
            # flood. The semaphore throttles LLM concurrency, but tasks would otherwise queue
            # without bound. Past the cap, tell the player to retry rather than pile on.
            if len(self._turn_tasks) >= _MAX_INFLIGHT_TURNS:
                logger.warning(f"[admission] turn fan-out cap hit ({len(self._turn_tasks)}); shedding")
                # V19-6 (SERVER-10): target the shed-notice at the asker (parsed from the frame's
                # |||-prefix) so it doesn't broadcast under the cooked B4 empty-target rule; empty
                # target (unidentified frame) is the accepted before-identity broadcast.
                await websocket.send(_wire({
                    "type": "error",
                    "message": "Sheldon is swamped right now — give me a moment and try again.",
                    "target": self._requester_eos_of(msg, websocket),
                }))
                return
            task = asyncio.create_task(
                self._handle_player_message_safe(msg, session, websocket)
            )
            self._turn_tasks.add(task)
            task.add_done_callback(self._turn_tasks.discard)

        elif msg_type == "position_update":
            # Update player position (sent periodically by the mod)
            pos = msg.get("position", {})
            session.player.update_position(pos, msg.get("facing_yaw", 0.0))

        elif msg_type in _QUERY_REPLY_TYPES:
            # Cooked getter reply → deliver to the oldest LIVE waiter of this type. A late reply for a
            # getter that already timed out is consumed against its tombstone and DROPPED, never handed
            # to the next player's waiter (XDEL-01 cross-delivery fix). No payload logged (privacy).
            logger.debug(f"[reply] {msg_type} waiters={len(self._query_waiters.get(msg_type, []))}")
            self._deliver_typed_reply(msg_type, msg)

        elif msg_type in _GAME_EVENT_TYPES:
            # Unsolicited typed frame from the mod's universal event buff
            # (bp_instigator_received_killing_damage / bp_adjust_damage_ex → the WS
            # component's SendPlayerMessage UFunction). Record it so the next turn can
            # react / answer "what killed me?". No reply expected. The cluster scope (from the
            # server-link session) keys the per-cluster team->tribe map (BUFFS-07).
            cid = getattr(getattr(session, "scope", None), "cluster_id", None)
            self._handle_game_event(_GAME_EVENT_TYPES[msg_type], msg, cluster_id=cid)

        elif msg_type == "server_state":
            # Out-of-band state push — the mod's answer to a get_server_state request
            # (the map's live GPS constants). Persist to telemetry.server_state; no reply.
            await self._handle_server_state(msg, session)

        elif msg_type == "tool_response":
            # Game mod responding to a tool request (future use)
            pass

        elif msg_type == "ping":
            await websocket.send(_wire({"type": "pong"}))

        elif msg_type == "auth":
            # The mod sends auth from two paths (connect callback + timer
            # fallback); the first is consumed by the handshake. Re-ack the
            # duplicate so the mod's IsAuthenticated flag sets either way.
            await websocket.send(_wire({
                "type": "auth_success",
                "player_id": session.player.player_id,
                "tier": session.player.tier,
                "tools_available": len(self.registry.get_tools_for_tier(session.player.tier)),
            }))

        else:
            logger.warning(f"Unknown message type: {msg_type} :: {json.dumps(msg)[:300]}")

    def _handle_game_event(self, kind: str, msg: dict, cluster_id: str | None = None) -> None:
        """Record a mod-pushed game event (death/damage). Stored in a bounded ring and
        surfaced to turns via extra_context; logged so a cook can verify the buff→bridge
        path end-to-end. The 'type' marker is dropped — the rest is the event payload
        (death: victim/killer/cause; damage: amount/cause). A field-tolerant 'summary' is
        derived so a turn can answer "what killed me?" without re-parsing raw fields.

        `cluster_id` keys the per-cluster team->tribe map a dino_tamed learns into (BUFFS-07).

        Damage frames are flood-controlled (rate cap + coalesce); death is never throttled —
        it's rare and critical, and must always reach the ring."""
        from sheldon_bridge.names import parse_actor_ref
        now = time.time()
        data = {k: v for k, v in msg.items() if k != "type"}
        for f in ("killer", "attacker"):   # ACTOR names (GetObjectName) -> clean species + class_key + uid
            if data.get(f):
                ref = parse_actor_ref(data[f])
                if not ref.get("name"):
                    data.pop(f)                       # null-ish (e.g. environmental death -> "None") -> drop
                    continue
                data[f] = ref["name"]                 # clean species (display)
                data[f + "_class"] = ref["class_key"] # census dino_species join key
                if ref.get("uid"):
                    data[f + "_uid"] = ref["uid"]     # the individual-marry key (matches census dino_uid scan)
        if kind == "damage_event" and data.get("cause"):  # damage cause = the DamageCauser ACTOR
            ref = parse_actor_ref(data["cause"])          # -> clean species (death's 'cause' is a damage
            if ref.get("name"):                           #    TYPE string, NOT an actor, so leave it alone)
                data["cause"] = ref["name"]
                if ref.get("class_key"):
                    data["cause_class"] = ref["class_key"]
        # NOTE: data["victim"] is the dying PLAYER's id (the V17 buff stamps the EOS) — NOT an
        # actor name. It must line up with the requester id so the asker's turn can pick out
        # THEIR own death (the per-player recent_events filter) and concurrent deaths can't
        # cross-attribute. Canonicalize it through the SAME alias the requester id uses, so a
        # mixed-id cook (EOS victim vs numeric requester) still matches.
        victim = str(data.get("victim", "")).strip()
        if victim in ("", "0", "None"):
            data.pop("victim", None)                  # no usable victim id (cast-failed / env death)
        else:
            data["victim"] = canonical_id(victim)
        if kind == "damage_event":
            try:
                amt = float(data.get("amount", 0) or 0)
            except (TypeError, ValueError):
                amt = 0.0
            if amt < _DAMAGE_MIN_AMOUNT:
                return  # trivial/parseless hit — below the (no-cook tunable) report floor
            if not self._admit_damage_event(now):
                return  # raid-scale burst tripped the bridge's ingest cap → dropped + counted
        event = {"event": kind, "at": now, "data": data,
                 "summary": self._summarize_game_event(kind, data)}
        if kind == "damage_event" and self._coalesce_into_recent_damage(event, now):
            return  # merged into the rolling damage entry (keeps deaths in the ring)
        # BUFFS-09: dino tame/death events ride their OWN ring (pull-only via get_recent_dino_events),
        # so a combat death/damage burst in the shared ring can't evict them before the tribe polls.
        if kind in ("dino_tamed", "dino_death"):
            self._recent_dino_game_events.append(event)
        else:
            self._recent_game_events.append(event)
        if kind == "dino_tamed":   # learn team->tribe so a later dino_death (team only) can be attributed
            team, tribe = str(data.get("team", "")).strip(), str(data.get("tribe", "")).strip()
            if team and tribe:
                # BUFFS-07: store under the originating cluster so a same-named tribe on another
                # cluster can't overwrite this mapping (last-writer-wins was bridge-wide before).
                cmap = self._team_tribe.setdefault(cluster_id or "", {})
                if team not in cmap and len(cmap) >= _TEAM_TRIBE_MAX:
                    cmap.pop(next(iter(cmap)), None)  # bound (T12): FIFO-evict oldest within the cluster
                cmap[team] = tribe
        logger.info(f"[game-event] {kind} :: {json.dumps(msg)[:300]}")

    def _recent_events_for(self, requester_id: str) -> list:
        """The recent game-events visible to ONE survivor's turn. Death AND damage events are
        scoped to THIS player (victim == their canonical id) so concurrent events can't cross-
        attribute when several players ask "what killed/hit me?" at once — the V18 damage buff
        stamps the damaged player's EOS as victim, exactly like the death buff; victimless events
        (environmental / cast-failed) stay as a timing fallback. Both sides are already
        canonicalized (requester_id at parse, victim at
        ingest), but canonicalize defensively here too so this predicate is correct in isolation
        regardless of what was stored — that mixed-id robustness is exactly the V17 partial-EOS fix."""
        rid = canonical_id(requester_id)
        # Deep-copy each event (H3): the ring entries are mutated in place by
        # _coalesce_into_recent_damage on the recv loop, so handing a turn the live dicts
        # risks a torn read while it formats them. The ring is tiny (maxlen 20) → cheap.
        return [copy.deepcopy(e) for e in self._recent_game_events
                if e.get("event") not in ("dino_tamed", "dino_death")   # dino: pull-only, tribe-filtered
                and (e.get("event") not in ("death_event", "damage_event")
                     or canonical_id(e.get("data", {}).get("victim")) in (rid, None))]

    def _recent_dino_events(self) -> list:
        """The dino tame/death ring (deep-copied), UNFILTERED — kept OUT of the proactive prompt;
        get_recent_dino_events filters by the asker's tribe before returning, so no cross-tribe leak."""
        return [copy.deepcopy(e) for e in self._recent_dino_game_events]

    def _admit_damage_event(self, now: float) -> bool:
        """Sliding-window rate cap on damage-frame INGESTION (the bridge's own protection;
        the mod's baked floor is what protects the link). Returns False — and counts the
        drop — when more than _DAMAGE_RATE_CAP frames arrived within _DAMAGE_RATE_WINDOW_S."""
        win = self._damage_ingest_times
        cutoff = now - _DAMAGE_RATE_WINDOW_S
        while win and win[0] < cutoff:
            win.popleft()
        if len(win) >= _DAMAGE_RATE_CAP:
            self._damage_dropped += 1
            if self._damage_dropped % 50 == 1:
                logger.warning(f"[game-event] damage flood cap hit; dropped {self._damage_dropped} frame(s)")
            return False
        win.append(now)
        return True

    def _coalesce_into_recent_damage(self, event: dict, now: float) -> bool:
        """If the newest ring entry is a damage_event within _DAMAGE_COALESCE_WINDOW_S, merge
        this one into it (keep the latest fields, bump 'count', refresh time/summary) instead
        of appending — so sustained combat stays a single rolling entry and can't evict rare
        death events. Returns True if it coalesced."""
        if not self._recent_game_events:
            return False
        last = self._recent_game_events[-1]
        if last.get("event") != "damage_event" or (now - last.get("at", 0.0)) > _DAMAGE_COALESCE_WINDOW_S:
            return False
        count = last["data"].get("count", 1) + 1
        last["data"] = dict(event["data"], count=count)
        last["at"] = now
        last["summary"] = f"{event['summary']} (x{count})"
        return True

    @staticmethod
    def _summarize_game_event(kind: str, data: dict) -> str:
        """One-line, field-tolerant gloss of a death/damage frame (any field may be absent
        on older/partial frames — never KeyError)."""
        if kind == "death_event":
            killer, cause = data.get("killer"), data.get("cause")
            if killer and cause:
                return f"Killed by {killer} ({cause})"
            if killer:
                return f"Killed by {killer}"
            if cause:
                return f"Died: {cause}"
            return "Died (cause unknown)"
        if kind == "damage_event":
            amount, cause = data.get("amount"), data.get("cause")
            base = "Took damage" if amount is None else f"Took {amount} damage"
            return f"{base} from {cause}" if cause else base
        if kind == "dino_tamed":
            d, t = data.get("dino", "dino"), data.get("tribe")
            return f"Tamed a {d}" + (f" (tribe {t})" if t else "")
        if kind == "dino_death":
            d, k = data.get("dino", "dino"), data.get("killer")
            return f"Your tribe's {d} was killed by {k}" if k else f"Your tribe's {d} died"
        return kind

    async def _handle_server_state(self, msg: dict, session) -> None:
        """Persist an out-of-band server_state push from the mod — the map's live GPS constants
        (gps_lat_origin/gps_lon_origin/gps_lat_scale/gps_lon_scale), answering get_server_state.

        Each non-'type' key is upserted into THIS server's telemetry.server_state; from there
        gps.from_server_state + the assembler/find_dinos read them (they fall back to raw coords
        until present, so a mod that never publishes can never produce a wrong GPS). The gps_
        prefix keeps these out of the LLM context (the assembler filters it). set_server_state is
        an upsert, so a re-publish (e.g. after a corrected recook) self-corrects. Persisting any
        key (not just gps_) keeps this forward-compatible if the mod later pushes map_name/etc."""
        keys = {k: v for k, v in msg.items() if k != "type" and v is not None}
        if not keys:
            return
        try:
            telemetry = TelemetryStore(await self.db.telemetry(
                session.scope.cluster_id, session.scope.server_id))
            for k, v in keys.items():
                await telemetry.set_server_state(k, v)
        except Exception as e:
            logger.warning(f"[server_state] failed to persist {sorted(keys)}: {e}")
            return
        logger.info(f"[server_state] stored {', '.join(sorted(keys))}")

    async def _handle_player_message_safe(
        self, msg: dict, session, websocket: ServerConnection
    ) -> None:
        """Run a turn task defensively: one turn's failure must not kill the
        connection's recv loop, and the player still gets an error frame."""
        try:
            await self._handle_player_message(msg, session, websocket)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"player_message task failed: {e}", exc_info=True)
            try:
                # V19-6 (SERVER-10): target the error at the asker so the cooked B4 filter doesn't
                # broadcast it to every client (empty target = before-identity broadcast fallback).
                await websocket.send(_wire({
                    "type": "error",
                    "message": "Internal error processing your request",
                    "target": self._requester_eos_of(msg, websocket),
                }))
            except Exception:
                pass

    async def _handle_player_message(
        self, msg: dict, session, websocket: ServerConnection
    ) -> None:
        """Handle a player chat message — run the agentic loop."""
        text = msg.get("message", "").strip()
        if not text:
            return

        # In-game telemetry pings (mod lifecycle events) — log, never LLM
        if text.startswith("__telemetry:"):
            logger.info(f"[telemetry] {text[len('__telemetry:'):]}")
            return

        # The cooked mod (C-identity graft, 2026-06-14) prepends per-survivor
        # identity to the chat text:
        #     <player_id>|||<admin true|false>|||<character_name>|||<message>
        # Parse it so each survivor gets their OWN campaign identity/history instead
        # of riding the anonymous server-multiplexed link. Backward-compatible: an
        # un-prefixed message (older mod, or telemetry above) falls through to the
        # connection's session identity unchanged.
        requester_id = session.player.player_id
        requester_name = session.player.display_name
        requester_admin = session.player.tier in ("admin", "superadmin")
        had_identity = False
        # SECURITY (SEC-01/ADD-03): ONLY honor the per-survivor identity + admin prefix on the
        # authenticated server-link connection. A |||-prefixed player_message from any OTHER socket
        # has its identity/admin claim IGNORED (→ unidentified → dropped below) — a client cannot
        # impersonate a survivor or self-promote to admin by riding a second connection.
        is_link = websocket is self._server_link_ws
        if is_link and text.count("|||") >= 3:
            pid, admin_s, cname, text = (p.strip() for p in text.split("|||", 3))
            if pid:
                requester_id = pid
                requester_name = cname or requester_name
                requester_admin = admin_s.lower() in ("true", "1")
                had_identity = True

        # Normalize the parsed id to its canonical form (numeric LinkedPlayerID -> EOS).
        # The V17 cook is in a partial-EOS state: the death buff stamps the EOS id but this
        # chat prefix still carries the legacy numeric. Canonicalizing HERE — before the id
        # keys the session, the campaign history, telemetry, AND the per-player death filter —
        # makes death attribution match again and stops chat history from forking off the
        # EOS-keyed campaign. No-op once the mod emits EOS everywhere. See names.canonical_id.
        requester_id = canonical_id(requester_id)

        # A player_message WITHOUT the mod's identity prefix is NOT real player chat — it's
        # a cooked getter reply leaking back over the server link. The current getters reply
        # via SendPlayerMessage as a raw, untyped player_message (e.g. "X=0.000 Y=0.000
        # Z=0.000", "0.0") with no `type` (so the reply router can't catch it) and no `|||`
        # identity. Running the LLM on those spams phantom "Survivor" turns + timeout replies
        # to whoever's online. Drop them here. (Proper getter routing needs the typed-reply
        # cook — V12; this only stops the spam.)
        if not had_identity:
            logger.info(f"[drop-leak] unidentified server-link msg: {text[:80]!r}")
            return

        # Empty body AFTER stripping the identity prefix: the mod periodically sends
        # identity-only frames (e.g. `pid|||false|||name|||` with no message). The early
        # `if not text` guard above can't catch these (the prefix makes the raw non-empty),
        # so re-check here — otherwise we burn a turn answering nothing ("Still here, send
        # your message...") and persist an empty user row, which pollutes later context.
        if not text:
            return
        if len(text) > _MAX_MESSAGE_CHARS:
            # ADD-01: bound the player-controlled body before it enters the LLM context, lore
            # search, and persistence (prompt-injection / oversized-payload guard).
            text = text[:_MAX_MESSAGE_CHARS]

        # Per-requester session. The connection `session` is the anonymous multiplexed
        # link shared by ALL players, so running turns on it races the conversation
        # scratch buffer under concurrency and lumps every player into one rate-limit
        # bucket. Key an isolated session (own scratch + lock + rate-limit bucket) by the
        # resolved requester id. The DB stays the durable per-survivor source of truth.
        req_player = dataclasses.replace(
            session.player,
            player_id=requester_id,
            display_name=requester_name,
            tier="admin" if requester_admin else "player",
        )
        rsession = self.sessions.get_or_create(req_player, scope=session.scope)
        rsession.player.display_name = requester_name
        rsession.player.tier = req_player.tier

        # ADMISSION rate-limit (SERVER-11): reject a spammer HERE — before the expensive assemble()
        # and before taking a turn-task slot — so one player's flood can't shed everyone else via the
        # admission cap. Targeted (per-player) reply, NOT persisted (no history pollution).
        allowed, reason = self.rate_limiter.check(requester_id, req_player.tier, "requests")
        if not allowed:
            await self._send_reply_paced(websocket, _reply_wire(
                _clean_reply(f"You're sending messages too quickly — give me a moment. ({reason})"),
                requester_id))
            return

        # Update position if included in the message
        pos = msg.get("position")
        if pos:
            rsession.player.update_position(pos, msg.get("facing_yaw", 0.0))

        # Echo request_id from client (for server-side request tracking)
        request_id = msg.get("request_id")

        # Send "thinking" indicator. V19-6 (SERVER-10): TARGET it at the resolved requester so the
        # cooked B4 OnRep filter renders the indicator only on the asker's client — an untargeted
        # frame's empty target matches every client (Contains(localEOS,"")==true) and would flicker
        # "thinking" on everyone whenever ANY player asks. requester_id is always set here
        # (unidentified frames were dropped above).
        thinking_msg = {"type": "thinking", "target": requester_id}
        if request_id is not None:
            thinking_msg["request_id"] = request_id
        await websocket.send(_wire(thinking_msg))

        # Context stores wrap the shared per-cluster/server connections (cheap).
        scope = session.scope
        campaign = CampaignStore(await self.db.campaign(scope.cluster_id))
        telemetry = TelemetryStore(await self.db.telemetry(scope.cluster_id, scope.server_id))

        # Per-requester lock serializes ONE player's rapid-fire turns (ordering); the semaphore
        # caps how many DIFFERENT players do heavy work at once. Context assembly (DB-heavy) AND
        # the agentic loop BOTH run inside the semaphore so a burst of queued turns can't pile
        # their assemble() DB load onto the loop before admission (M3). Assembling under the
        # per-player lock also means a player's back-to-back turns assemble serially (after the
        # prior turn's LLM run) instead of concurrently as before. (Persistence still lands just
        # after the lock — a fully serialized assemble→run→persist is a Phase-2 refinement.)
        async with rsession.lock:
            async with self._turn_semaphore:
                # Ensure the (possibly newly-seen) requesting survivor exists in the campaign.
                await campaign.upsert_survivor(
                    requester_id, requester_name, "admin" if requester_admin else "player"
                )
                # This player's recent game events (per-player death scoping + numeric/EOS
                # aliasing). PUSHED into the prompt (so "what killed me?" works without the LLM
                # calling get_recent_events) AND handed to the tool ctx for explicit recall.
                recent_events = self._recent_events_for(requester_id)
                assembled = await self.assembler.assemble(
                    base_system=self.config.build_base_system(),
                    campaign=campaign,
                    telemetry=telemetry,
                    eos_id=requester_id,
                    character_name=requester_name,
                    is_admin=requester_admin,
                    user_message=text,
                    recent_events=recent_events,
                )
                rsession.set_context(assembled.system_prompt, assembled.messages)
                try:
                    result = await asyncio.wait_for(self.agent.run(
                        rsession, text,
                        extra_context={"campaign": campaign, "telemetry": telemetry,
                                       "eos_id": requester_id, "character_name": requester_name,
                                       "recent_events": recent_events,
                                       "dino_events": self._recent_dino_events(),
                                       # BUFFS-07: hand the tool ONLY the asker's own-cluster team->tribe
                                       # map, so a same-named tribe on another cluster can't bleed in.
                                       "team_tribe": dict(self._team_tribe.get(scope.cluster_id, {}))},
                    ), timeout=_TURN_WALLCLOCK_TIMEOUT)
                except asyncio.TimeoutError:
                    # TS-AGENT01: a wedged/slow turn must not hold one of the 8 semaphore slots
                    # indefinitely. Cancelling agent.run unwinds it cleanly and frees the slot.
                    logger.warning(f"[turn-timeout] {requester_id[:8]}... exceeded "
                                   f"{_TURN_WALLCLOCK_TIMEOUT:.0f}s — cancelled to free the slot")
                    result = AgentResult(
                        response_text="That one took too long and I had to stop — try a simpler request.",
                        tool_calls_made=0, iterations=0, total_input_tokens=0, total_output_tokens=0,
                        total_cost=0.0, duration_ms=_TURN_WALLCLOCK_TIMEOUT * 1000.0,
                        error="turn_wallclock_timeout",
                    )

        # Persist the turn to durable conversation history (keyed per survivor). Skip the assistant
        # row on an error OR a transient canned rejection (max-iter / turn-timeout) so boilerplate
        # never re-enters the survivor's context on later turns (LEAK-02).
        await campaign.add_message(requester_id, "user", text)
        if not result.error and not result.transient:
            await campaign.add_message(requester_id, "assistant", result.response_text)

        # Send response — _clean_reply() strips markdown/emoji and labels it
        # "Sheldon: ..."; _reply_wire() then frames it for the cooked v6 mod
        # (matches the reply type with literal backslashes, splits the body on
        # compact delimiters), so the wire string must satisfy both.
        # C3-08: the per-player reply MUST carry a non-empty target or the cooked B4 filter
        # (Contains(localEOS, "")==true) renders it on EVERY client. requester_id is always set here
        # (unidentified messages are dropped earlier), but guard the broadcast footgun explicitly.
        if not (requester_id or "").strip():
            logger.warning("[reply] empty requester_id on the per-player path — dropping (would broadcast)")
            return
        await self._send_reply_paced(websocket, _reply_wire(_clean_reply(result.response_text), requester_id))

        logger.info(
            f"[{rsession.player.display_name}] "
            f"'{text[:50]}...' → "
            f"{result.iterations} iters, "
            f"{result.tool_calls_made} tools, "
            f"${result.total_cost:.4f}, "
            f"{result.duration_ms:.0f}ms"
        )

    def _next_getter_req_id(self) -> int:
        """Monotonic per-process getter request id (V19-2). Stamped into _query_wire; a future cook
        echoes it back so _deliver_typed_reply can correlate the reply by (reply_type, id) and the
        global getter lock can eventually be dropped for throughput. Harmless on the cooked V18 mod,
        which ignores the trailing id and never echoes it."""
        self._getter_req_counter = getattr(self, "_getter_req_counter", 0) + 1
        return self._getter_req_counter

    def _register_getter_waiter(self, reply_type: str, req_id: int | None = None) -> "_GetterWaiter":
        """Register a single-flight getter waiter for reply_type and return it. Caller MUST hold
        _getter_lock and send the query immediately after. Purges expired tombstones first so the
        per-type FIFO stays bounded (XDEL-01). `req_id` (V19-2) lets an id-echoing reply correlate
        directly; it is recorded but does NOT change FIFO behaviour for the id-less cooked mod."""
        loop = asyncio.get_event_loop()
        waiters = self._query_waiters.setdefault(reply_type, [])
        if waiters:
            cutoff = loop.time() - _GETTER_REPLY_LATENESS
            waiters[:] = [w for w in waiters if not (w.abandoned and w.sent_at < cutoff)]
        w = _GetterWaiter(loop.create_future(), loop.time(), req_id)
        waiters.append(w)
        return w

    def _deliver_typed_reply(self, reply_type: str, msg: dict) -> None:
        """Route a cooked getter reply to the right outstanding waiter of its type.

        V19-2 (req-id correlation, ADDITIVE): if the reply carries an echoed `id` (a FUTURE cook),
        resolve the waiter whose `req_id` matches it — correct even if it arrives after a
        different-id waiter is already pending. A timed-out (tombstoned) id-match is consumed +
        dropped (anti-cross-delivery); an id with no matching waiter is a late/unsolicited frame,
        dropped.

        Back-compat: when the reply carries NO id (the CURRENTLY-COOKED V18 mod), fall back to the
        existing tombstone-FIFO exactly as before — deliver to the OLDEST live waiter, consume a
        late reply against a leading tombstone, purge expired tombstones off the front. Same-type
        id-less replies arrive in send-order (the mod serializes getters on one QueryAccum slot over
        one TCP stream), so oldest-match is correct."""
        waiters = self._query_waiters.get(reply_type)
        if not waiters:
            logger.info(f"[reply-drop] {reply_type}: no waiter (late/unsolicited)")
            return
        # V19-2 id-correlated path (only when the reply actually echoes an id).
        rid = self._reply_req_id(msg)
        # Getter correlation visibility (V20-1 verify + ongoing multi-user debugging): id=<n> means the
        # cooked mod echoed the request id (id-correlation active); id=None means FIFO fallback.
        logger.info(f"[reply-corr] {reply_type} id={rid}")
        if rid is not None:
            for i, w in enumerate(waiters):
                if w.req_id == rid:
                    waiters.pop(i)
                    if w.abandoned:
                        logger.info(f"[reply-drop] {reply_type} id={rid}: late reply for a timed-out getter")
                    elif not w.fut.done():
                        w.fut.set_result(msg)
                    return
            logger.info(f"[reply-drop] {reply_type} id={rid}: no matching waiter (late/unsolicited)")
            return
        # Back-compat tombstone-FIFO path (id-less cooked mod).
        cutoff = asyncio.get_event_loop().time() - _GETTER_REPLY_LATENESS
        while waiters and waiters[0].abandoned and waiters[0].sent_at < cutoff:
            waiters.pop(0)
        if not waiters:
            logger.info(f"[reply-drop] {reply_type}: only expired tombstones")
            return
        head = waiters.pop(0)
        if head.abandoned:
            logger.info(f"[reply-drop] {reply_type}: late reply for a timed-out getter (anti-cross-delivery)")
            return
        if not head.fut.done():
            head.fut.set_result(msg)

    @staticmethod
    def _reply_req_id(msg: dict) -> int | None:
        """Pull an echoed getter-request id out of a reply frame (V19-2), tolerant of int or
        numeric-string. Returns None when absent (the cooked V18 mod) — the caller then uses the
        tombstone-FIFO fallback. A non-numeric id is treated as absent (never crashes the router)."""
        if not isinstance(msg, dict) or "id" not in msg:
            return None
        try:
            return int(msg["id"])
        except (TypeError, ValueError):
            return None

    def _requester_eos_of(self, msg: dict, websocket: ServerConnection) -> str:
        """Resolve the requester EOS of a player_message frame for TARGETING a per-player non-reply
        frame (V19-6 / SERVER-10) — thinking/error frames sent BEFORE _handle_player_message has fully
        parsed identity. Mirrors that parse exactly: ONLY the server-link's |||-prefix is honored
        (SEC-01/ADD-03 — a non-link socket cannot self-identify), canonicalized (V17 partial-EOS).
        Returns "" when no identity can be resolved → the caller leaves the target empty (the
        accepted broadcast-before-identity case)."""
        if not isinstance(msg, dict):
            return ""
        text = (msg.get("message") or "")
        if websocket is getattr(self, "_server_link_ws", None) and text.count("|||") >= 3:
            pid = text.split("|||", 3)[0].strip()
            if pid:
                return canonical_id(pid) or ""
        return ""

    async def _send_reply_paced(self, ws, wire: str) -> None:
        """Send a reply (a `_reply_wire` frame that sets the mod's replicated `ReplyText`) with a
        min gap since the last reply on this link (V21-3). All replies share ONE replicated var,
        which UE coalesces last-writer-wins; without spacing, two replies in one net-update window
        clobber and the loser's requester never sees their reply. Serializing here makes each value
        persist >= a net-update so it replicates to its target client first. Only delays bunched
        replies (rare); a lone reply pays ~0 (last_send is far in the past)."""
        async with self._reply_lock:
            now = asyncio.get_event_loop().time()
            wait = self._last_reply_send + _REPLY_MIN_GAP_S - now
            if wait > 0:
                await asyncio.sleep(wait)
            await ws.send(wire)
            self._last_reply_send = asyncio.get_event_loop().time()

    async def _game_command_handler(self, command: dict) -> dict:
        """Tool-context game handler: relay an action to the connected mod.

        v1 supports `console_command` — the cooked B-graft console pipe. It's
        fire-and-forget: the mod runs the command via Execute Console Command (void)
        on the requesting player, and ARK enforces admin server-side, so there's no
        reply payload to await. Structured actions (e.g. spawn_dino) aren't handled by
        the void pipe yet — the LLM should fall back to execute_console_command with a
        raw ARK command for those.
        """
        # The mod connects as the server-multiplexed link; commands ride back over it. Use the
        # explicitly-tracked link (NOT a _connections fallback that could target a player socket, F7).
        ws = self._server_link_ws
        if ws is None:
            return {"success": False, "error": "No game server is connected to the bridge."}
        action = command.get("action")
        if action == "console_command":
            cmd = (command.get("command") or "").strip()
            if not cmd:
                return {"success": False, "error": "Empty console command."}
            # V21-1: route the command to the requesting admin's CLIENT (real cheat manager → no
            # server crash, Gotcha #34) via the cooked `exec_command` RepNotify branch. The requester
            # EOS is the per-player target — REQUIRED, else the mod's Contains(localEOS,"") matches
            # EVERY client (broadcast footgun). The tool is admin-tier; ARK's own per-player admin
            # check still gates the client-side run.
            eos = (command.get("eos") or "").strip()
            if not eos:
                return {"success": False,
                        "error": "Refusing console command: no requester id (would broadcast to every client)."}
            # Elevate so the client actually executes it (admincheat = the ARK admin path). Tunable
            # bridge-side — no recook to change the prefix. Don't double-prefix.
            run = cmd if cmd.lower().startswith(("admincheat ", "cheat ")) else "admincheat " + cmd
            # Fire-and-forget: the client runs it; stdout READBACK stays BLOCKED (no placeable node
            # captures console output), so `command_result`/the await stay unused for now (exec-only).
            try:
                await ws.send(_exec_command_wire(run, eos))
            except Exception as e:
                return {"success": False, "error": f"Failed to send command to the server: {e}"}
            return {"success": True, "message": f"Sent to your client to run: {run}"}
        if action == "query":
            # Request/await a cooked getter. command: {request:"get_vitals", reply:"vitals"}.
            request_type = command.get("request"); reply_type = command.get("reply")
            if reply_type not in _QUERY_REPLY_TYPES:
                return {"success": False, "error": f"Unknown query reply type '{reply_type}'."}
            # C2-05: a player-data getter MUST carry the requester EOS — the cooked mod resolves the
            # pawn by it, and an empty eos makes the mod's Contains(eos,"") match the FIRST PC (wrong
            # player). dino_scan is census (no eos). Refuse an empty-eos player getter outright.
            if reply_type != "dino_scan" and not (command.get("eos") or "").strip():
                return {"success": False,
                        "error": f"Refusing {reply_type}: no requester id (would read the wrong player)."}
            eos = command.get("eos") or ""
            # LSA-02: a getter reply is silently DROPPED when the mod's IsAuthenticated gate is false
            # (the SendPlayerMessage else-branch is a cooked-stripped PrintString) — which happens in
            # the brief reconnect→reauth window after any link blip / redeploy / NPM restart. The most
            # common cause of a getter timeout is therefore transient: retry ONCE after a short backoff
            # before surfacing the error. Bounded to exactly one extra attempt so it can never loop.
            r, w1 = await self._query_once(ws, request_type, reply_type, eos)
            if (not r.get("success")) and r.get("retryable"):
                # LSA-02: drop attempt-1's tombstone BEFORE retrying — else the retry's reply (id-less
                # on the cooked V18 mod) is consumed against that stale tombstone by the FIFO router and
                # the retry waiter times out, so the retry could NEVER succeed. The dropped reauth reply
                # for attempt-1 never arrives; same player+getter under the lock → safe to discard.
                self._discard_getter_waiter(reply_type, w1)
                await asyncio.sleep(_GETTER_REAUTH_BACKOFF_S)
                r, _ = await self._query_once(ws, request_type, reply_type, eos)
            r.pop("retryable", None)
            return r
        if action == "map_pin":
            # Admin map-pin actuator (add/remove/clear). Serialized + monotonic id — see
            # _handle_map_pin (M4).
            return await self._handle_map_pin(ws, command)
        return {
            "success": False,
            "error": (f"Action '{action}' isn't supported by the live console pipe yet — "
                      f"use execute_console_command with a raw ARK command instead."),
        }

    async def _query_once(self, ws, request_type: str, reply_type: str, eos: str) -> dict:
        """One getter round-trip. Returns the reply on success, else an error dict with `retryable`
        True on a timeout (the LSA-02 reauth-drop case) so the caller can retry exactly once. Stamps a
        monotonic req-id into the wire + the waiter; the cooked V20 mod echoes it back so
        _deliver_typed_reply correlates the reply by (reply_type, id).

        V20-1 (2026-06-25): the getter lock is DROPPED — getters run CONCURRENTLY, correlated by id
        (verified in-game). Previously this held _getter_lock across send→await so only one getter was
        in flight (required while the cooked mod sent id-less replies that would FIFO-cross-deliver)."""
        self._last_player_getter_ts = time.time()   # signal the census to yield to this interactive getter
        # V20-1 (2026-06-25): GETTER LOCK DROPPED. The cooked V20 mod echoes the request id —
        # verified in-game ([reply-corr] vitals id=6/7, position id=13/18) — so _deliver_typed_reply
        # correlates concurrent same-type getters by id (no cross-delivery). Player getters now run
        # CONCURRENTLY (the 20–30-player throughput unlock); the census still self-serializes (singleton).
        # SAFETY NET: the [reply-corr] log flags any getter that ever replies id=None — if one appears,
        # that getter's id-echo regressed and the lock should be restored (re-wrap this body).
        req_id = self._next_getter_req_id()
        w = self._register_getter_waiter(reply_type, req_id)
        try:
            # T6: cap the send so a back-pressured write can't block this getter's task indefinitely.
            await asyncio.wait_for(
                ws.send(_query_wire(request_type, eos, req_id)),
                timeout=_GETTER_SEND_TIMEOUT,
            )
            reply = await asyncio.wait_for(w.fut, timeout=8.0)
            return {"success": True, "data": reply}, w  # router already removed our waiter
        except asyncio.TimeoutError:
            # Leave a tombstone so a LATE reply for this getter is dropped against it, never
            # cross-delivered (XDEL-01). Flag retryable — a timeout is most often the transient
            # reconnect→reauth drop (LSA-02). Return the waiter so the caller can discard this
            # tombstone before a retry (else the retry reply is eaten by it).
            w.abandoned = True
            return ({"success": False, "retryable": True,
                     "error": f"The server didn't return {reply_type} in time."}, w)
        except Exception as e:
            w.abandoned = True
            return {"success": False, "error": f"Failed to query {reply_type}: {e}"}, w

    def _discard_getter_waiter(self, reply_type: str, w) -> None:
        """Remove a specific (timed-out) getter waiter from its per-type FIFO so a retry's id-less
        reply isn't consumed against this stale tombstone (LSA-02). Identity match; safe because the
        retry is the same player+getter under the getter lock and attempt-1's reply was dropped."""
        lst = self._query_waiters.get(reply_type)
        if lst:
            lst[:] = [x for x in lst if x is not w]

    async def _handle_map_pin(self, ws, command: dict) -> dict:
        """Per-requester map-pin actuator (add/remove/clear). Serialized under _pin_lock (counter++ /
        registry RMW stays atomic across the send await); the id counter is MONOTONIC (never rolls
        back on a send failure — M4). MAPPIN-02/CDP-12: the registry is keyed PER REQUESTER (by eos)
        so a player can only remove/clear THEIR OWN pins, never clobber another survivor's. NOTE the
        cooked mod still RENDERS pins to every client (a singleton replicated var) — per-player
        RENDER is the V19 cook (MAPPIN-01); this closes the bridge-side OWNERSHIP hole."""
        op = command.get("op")
        owner = (command.get("eos") or "").strip() or "?"
        tgt = (command.get("eos") or "").strip()  # V19-5 per-player render target (empty -> broadcast)
        async with self._pin_lock:
            if op == "add":
                self._pin_counter += 1
                pin_id = "sheldon_p%d" % self._pin_counter
                label = command.get("label") or "Sheldon pin"
                x, y, z = command.get("x", 0.0), command.get("y", 0.0), command.get("z", 0.0)
                try:
                    await ws.send(_pin_wire("add", pin_id, label, x, y, z, target_eos=tgt))
                except Exception as e:
                    return {"success": False, "error": f"Failed to send pin to the server: {e}"}
                self._map_pins.setdefault(owner, {})[pin_id] = {"label": label, "x": x, "y": y, "z": z}
                return {"success": True, "pin_id": pin_id, "label": label}
            if op == "remove":
                pin_id = command.get("id") or ""
                owned = self._map_pins.get(owner, {})
                if pin_id not in owned:  # only the creator's own pins — no cross-player removal
                    return {"success": False, "error": f"No Sheldon pin '{pin_id}' of yours to remove."}
                try:
                    await ws.send(_pin_wire("remove", pin_id, target_eos=tgt))
                except Exception as e:
                    return {"success": False, "error": f"Failed to remove pin: {e}"}
                owned.pop(pin_id, None)
                return {"success": True, "removed": pin_id}
            if op == "clear":
                owned = self._map_pins.get(owner, {})
                cleared = 0
                try:
                    for pid in list(owned):
                        await ws.send(_pin_wire("remove", pid, target_eos=tgt))
                        owned.pop(pid, None)   # MAPPIN-06: pop as each send succeeds (no registry desync on partial failure)
                        cleared += 1
                except Exception as e:
                    return {"success": False, "error": f"Failed to clear pins after {cleared}: {e}", "cleared": cleared}
                return {"success": True, "cleared": cleared}
            return {"success": False, "error": f"Unknown map_pin op '{op}'."}

    async def _request_server_state(self, scope, ws) -> None:
        """On the server link, ask the mod for this map's GPS constants and stop once they land.

        The reply is out-of-band (it rides SendPlayerMessage → recovered → _handle_server_state
        persists it), so we just poll telemetry to decide when to stop rather than awaiting a
        future. Check-first: if a VALID set is already cached (this map's constants are static,
        so a prior session's capture is still correct) we send nothing. Otherwise ask, wait,
        re-check — up to _SERVER_STATE_TRIES. A pre-V17 mod has no get_server_state branch and
        never answers; the unmatched request is a harmless no-op (like the dormant census scans
        were) and we quietly exhaust the retries. (If a corrected recook ever needs to overwrite
        a stale-but-valid capture, clear the gps_ server_state keys so this re-asks.)"""
        try:
            telemetry = TelemetryStore(await self.db.telemetry(scope.cluster_id, scope.server_id))
        except Exception as e:
            logger.warning(f"[server_state] requester could not open telemetry: {e}")
            return
        for _ in range(_SERVER_STATE_TRIES):
            if gps.from_server_state(await telemetry.get_server_state()) is not None:
                return  # constants present (this session or cached) — done, never guess
            try:
                await ws.send(_query_wire("get_server_state"))
            except Exception:
                return  # link dropped
            await asyncio.sleep(_SERVER_STATE_RETRY_S)

    async def _census_scan(self, ws, cx: float, cy: float, cz: float,
                           radius: float, group: int) -> list[dict] | None:
        """Census scan_fn: send one get_dino_scan octree tile + await the reply.

        Returns parsed dino dicts on a real reply (possibly []), or **None on a timeout / send
        failure** (V21-2). None ≠ empty: the cell carries NO departure information, so the caller
        (_scan_cell) must NOT run it through the departure-sweep upsert (an empty sweep would
        false-remove every known dino in the square). A timeout almost always means the cell is
        too dense for the mod to serialize within scan_timeout, so _scan_cell SPLITS on None and
        the smaller quadrants resolve — instead of the old behaviour where the near-player active
        scan timed out every cycle and wiped the index around the player.

        Correlated single-flight on reply_type 'dino_scan'; the census is sequential (singleton
        per server) so two scans never contend for the one waiter slot."""
        # The census still self-serializes on _getter_lock (it is the ONLY remaining user of that
        # lock — V20-1 dropped it from the player getter path, so player queries no longer wait on
        # it). Getter-contention mitigation (no-cook, 2026-06-24): if a player queried within
        # _CENSUS_YIELD_WINDOW s, briefly defer so a census scan doesn't hog the mod's single game
        # thread while their interactive getter is in flight. (Post-V20-1 this is a soft game-thread
        # spacer, not a bridge-lock fix — the locks are decoupled; the V21-2 subdivision now keeps
        # each scan small too, so the contention it guards against is far smaller than it was.)
        if time.time() - self._last_player_getter_ts < _CENSUS_YIELD_WINDOW:
            await asyncio.sleep(_CENSUS_YIELD_DEFER)
        async with self._getter_lock:
            # Register with a monotonic req-id (the scan wire has no id field today — census
            # correlation stays FIFO under the lock; the id keeps the waiter uniform for a future
            # id-echoing cook). The scan_wire is unchanged → cooked-mod-compatible.
            w = self._register_getter_waiter("dino_scan", self._next_getter_req_id())
            try:
                await asyncio.wait_for(ws.send(_scan_wire(cx, cy, cz, radius, group)),
                                       timeout=_GETTER_SEND_TIMEOUT)
                reply = await asyncio.wait_for(w.fut, timeout=self._census_cfg.scan_timeout)
            except Exception:
                w.abandoned = True  # tombstone: a late dino_scan reply must not land in the next scan
                return None         # V21-2: None (not []) → _scan_cell splits / skips, never false-sweeps
        data = reply.get("data", "") if isinstance(reply, dict) else (reply or "")
        return census.parse_scan_reply(data)

    async def _run_census(self, scope, ws) -> None:
        """Continuous per-server dino census: keep this server's telemetry.db dino_index fresh
        via bounded DINOPAWNS octree tiles (bounds learned from each pass, reactive subdivision
        for dense regions, latency-feedback throttle), a cheap player-centric active layer
        between full passes, and a captured_at TTL reaper for staleness. Self-partitioning (the
        per-server telemetry.db IS the per-map DB). Runs until the link drops or is cancelled."""
        try:
            telemetry = TelemetryStore(await self.db.telemetry(scope.cluster_id, scope.server_id))
            cfg = self._census_cfg

            async def scan_fn(cx, cy, cz, radius, group):
                return await self._census_scan(ws, cx, cy, cz, radius, group)

            async def upsert_fn(bounds, dinos):
                return await telemetry.upsert_region(bounds, dinos)

            async def reap_fn(older_than):
                return await telemetry.reap_stale(older_than)

            async def players_fn():
                rows = await telemetry.recent_players(time.time() - cfg.active_player_ttl)
                return [{"x": r["x"], "y": r["y"], "z": r["z"]} for r in rows]

            def should_run():
                return ws is self._server_link_ws  # singleton: a superseded link's census exits (SCC-03)

            logger.info("Census loop started for server %s", getattr(scope, "server_id", "?"))
            await census.run_census(
                scan_fn=scan_fn, upsert_fn=upsert_fn, reap_fn=reap_fn,
                get_state=telemetry.get_server_state, set_state=telemetry.set_server_state,
                players_fn=players_fn, cfg=cfg, stats=census.CensusStats(), should_run=should_run,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Census loop error (server %s): %s",
                         getattr(scope, "server_id", "?"), e, exc_info=True)

    async def _cleanup_loop(self) -> None:
        """Periodically clean up expired sessions and rate limiter data."""
        while True:
            await asyncio.sleep(60)
            expired = self.sessions.cleanup_expired()
            self.rate_limiter.cleanup()
            if expired:
                logger.info(f"Cleaned up {expired} expired sessions")


async def run_server(config: BridgeConfig) -> None:
    """Start the bridge server."""
    server = BridgeServer(config)

    # Set up graceful shutdown
    stop = asyncio.get_event_loop().create_future()

    def handle_signal():
        if not stop.done():
            stop.set_result(None)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    # Start cleanup task
    cleanup_task = asyncio.create_task(server._cleanup_loop())

    ssl_ctx = build_ssl_context(config.ssl_cert, config.ssl_key)
    scheme = "wss" if ssl_ctx else "ws"
    logger.info(
        f"Sheldon Bridge starting on "
        f"{scheme}://{config.websocket_host}:{config.websocket_port}"
    )

    async with serve(
        server.handle_connection,
        host=config.websocket_host,
        port=config.websocket_port,
        ssl=ssl_ctx,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=2**20,  # 1MB max message
    ) as ws_server:
        logger.info("Sheldon Bridge is running. Press Ctrl+C to stop.")
        await stop

    cleanup_task.cancel()
    await server.db.close()
    logger.info("Sheldon Bridge stopped.")
