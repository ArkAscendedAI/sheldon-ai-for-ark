"""Map-agnostic dino census orchestrator (no-cook safe rewrite, 2026-06-21).

Drives the cooked `get_dino_scan` octree primitive per connected server (world-key =
scope.server_id) to keep each server's telemetry.db `dino_index` as fresh as practical
WITHOUT stalling the game thread or flooding the WS link. Sheldon ALWAYS answers dino
questions from the DB — it never scans on request.

Design (empirically validated — a full large-map pass = ~23,500 dinos in ~65s back-to-back,
throttled ~2-4min):
  - group `DINOPAWNS` (2) ONLY — it already includes far STASISED wild dinos; no group sweep.
  - NO 177k-radius discover probe (that was V16's crash). Bounds are LEARNED from the dino
    bbox of each pass; a periodic re-probe of a generous bounded box catches new regions.
  - reactive SUBDIVISION: a cell that returns too many dinos splits into quadrants and
    recurses (down to a floor) — caves auto-get fine cells, ocean stays coarse, nothing
    capped/missed (density-agnostic, even if spawn density doubles).
  - batch time-slice (one cell per tick) + latency-FEEDBACK throttle (scan latency = a live
    server-load proxy: back off when busy, speed up when idle).
  - FRESHNESS layer: a full pass runs only once per cadence/wipe-cycle; between passes a cheap
    player-centric ACTIVE layer keeps near-player dinos fresh (they're woken -> full stats
    valid, and that's where dinos actually change). Cost scales with players + wipe frequency,
    NOT the 24k map total.
  - STALENESS: upsert_region departure-sweeps each re-scanned tile (uid-keyed); a captured_at
    TTL reaper backstops regions that drop out of coverage; a population crater = a wipe.

Pure logic is dependency-injected (scan_fn / upsert_fn / reap_fn / state get-set / players_fn /
should_run) so it unit-tests without the live WS or DB. The bridge wires the real send/await +
telemetry store in server.py.
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

logger = logging.getLogger("sheldon.census")

# order MUST match the cooked census record (build_dino_scan.py ORDER)
FIELDS = ["uid", "species", "level", "gender", "team", "tamedname", "loc",
          "hp_c", "hp_m", "stam_c", "stam_m", "oxy_c", "oxy_m", "food_c", "food_m",
          "weight_c", "weight_m", "torp_c", "torp_m", "melee", "speed"]
_STAT_KEYS = FIELDS[7:]          # hp_c .. speed
_TAIL = len(FIELDS) - 2          # rigid fields after uid+species: level..speed (19)

_SQRT2 = 1.4142135623730951
_LVL_RE = re.compile(r"(?:lvl|level)\.?\s*(\d+)", re.IGNORECASE)


# --- parsing ---------------------------------------------------------------------

def _parse_vec(s: str) -> tuple[float, float, float]:
    """`Conv_VectorToString` form: 'X=123.4 Y=-56.7 Z=8.9' -> (x,y,z). Tolerant."""
    out = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    for tok in s.replace(",", " ").split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            k = k.strip().upper()
            if k in out:
                try: out[k] = float(v)
                except ValueError: pass
    return out["X"], out["Y"], out["Z"]


def _f(v):
    try: return float(v)
    except (TypeError, ValueError): return None


def _extract_level(species_raw: str, level_field) -> tuple[int | None, str]:
    """Return (level, clean_species). Prefer the integer level field when it's a real (>0)
    value; otherwise parse 'Lvl N' / 'Level N' out of the descriptive name (Primal Nemesis
    and other mods bake it in: 'Megalodon - Lvl 300'). Always strip that suffix so the stored
    species is clean ('Megalodon'). `AbsoluteBaseLevel` reads 0 on a stasised dino whose
    status component hasn't initialized — the descriptive name is the reliable source there."""
    lv = None
    try:
        f = int(float(level_field))
        if f > 0:
            lv = f
    except (TypeError, ValueError):
        pass
    species = (species_raw or "").strip()
    if lv is None:
        m = _LVL_RE.search(species)
        if m:
            lv = int(m.group(1))
    if _LVL_RE.search(species):
        species = _LVL_RE.sub("", species).strip().rstrip("-–—").strip()
    return lv, (species or (species_raw or "").strip())


def parse_record(rec: str) -> dict | None:
    """One pipe-delimited census record -> a dino_index dict. None if malformed.

    Hardened (2026-06-21): GetDescriptiveName can itself contain a '|' (modded names) ->
    surplus parts. Wild dinos (group DINOPAWNS) have an empty TamedName, so any surplus is in
    the species field: re-join the surplus into species and keep the rigid 19-field tail
    (level..speed). Guard on the loc field being a real vector ('=' present) so a record
    fragmented by a ';' inside a name is DROPPED rather than silently misparsed (the
    field-shifted Coelacanth bug)."""
    parts = rec.split("|")
    if len(parts) < len(FIELDS):
        return None                                   # fragment — drop, don't misparse
    if len(parts) > len(FIELDS):                       # surplus '|' -> attribute to species
        parts = [parts[0], "|".join(parts[1:len(parts) - _TAIL])] + parts[len(parts) - _TAIL:]
    d = dict(zip(FIELDS, parts))
    if "=" not in d["loc"]:                            # loc misaligned -> drop
        return None
    x, y, z = _parse_vec(d["loc"])
    level, species = _extract_level(d["species"], d["level"])
    gender = "F" if str(d["gender"]).strip().lower() == "true" else "M"
    base_stats = {k: _f(d[k]) for k in _STAT_KEYS}
    return {
        "dino_uid": d["uid"].strip() or None,
        "species": species or None,
        "name": (d["tamedname"].strip() or None),
        "level": level,
        "gender": gender,
        "tribe": d["team"].strip() or None,           # team int; bridge classifies wild/tamed
        "imprint": None,
        "base_stats": base_stats,
        "x": x, "y": y, "z": z,
    }


def parse_scan_reply(text: str) -> list[dict]:
    """Full census reply -> dino dicts. Accepts the mod's `dino_scan|<rec>;<rec>;…` form
    and a JSON envelope `{"type":"dino_scan","data":"<rec>;<rec>;"}`."""
    if not text:
        return []
    data = text
    s = text.strip()
    if s.startswith("{"):
        try: data = (json.loads(s).get("data") or "")
        except Exception: data = ""
    elif s.startswith("dino_scan|"):
        data = s[len("dino_scan|"):]
    out = []
    for rec in data.split(";"):
        rec = rec.strip()
        if not rec:
            continue
        d = parse_record(rec)
        if d and d["dino_uid"]:
            out.append(d)
    return out


# --- geometry --------------------------------------------------------------------

def tiles(minx: float, miny: float, maxx: float, maxy: float, size: float):
    """Yield (center_x, center_y, radius) covering the bbox with square tiles of `size`.
    radius = half-diagonal so the circular octree query fully covers each square tile."""
    if maxx <= minx or maxy <= miny or size <= 0:
        return
    radius = (size * _SQRT2) / 2.0
    y = miny
    while y < maxy:
        x = minx
        while x < maxx:
            yield (x + size / 2.0, y + size / 2.0, radius)
            x += size
        y += size


def bbox_of(dinos: list[dict], pad: float = 0.0) -> tuple[float, float, float, float] | None:
    """Bounding box (minx,miny,maxx,maxy) of dino positions, optionally padded. None if empty."""
    pts = [(d["x"], d["y"]) for d in dinos if d.get("x") is not None and d.get("y") is not None]
    if not pts:
        return None
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


# --- pure control helpers (directly unit-tested) ---------------------------------

def next_interval(latency: float, cur: float, cfg: "CensusConfig") -> float:
    """Adapt the inter-scan sleep to live server load (scan latency = a load proxy). A slow
    scan (busy game thread) backs off; a fast scan (idle) speeds up. Bounded."""
    if latency > cfg.latency_high:
        cur *= cfg.backoff
    elif latency < cfg.latency_low:
        cur *= cfg.speedup
    return max(cfg.min_interval, min(cfg.max_interval, cur))


def is_wipe(total: int, prev: int, cfg: "CensusConfig") -> bool:
    """A population crater vs the previous pass = a dino wipe / mass despawn."""
    return prev >= cfg.wipe_min_prev and total < prev * cfg.wipe_ratio


def is_converged(total: int, prev: int, cfg: "CensusConfig") -> bool:
    """Counts stable across passes -> repopulation settled / steady state."""
    if prev <= 0:
        return False
    return abs(total - prev) <= max(cfg.converge_abs, prev * cfg.converge_ratio)


# --- config / stats --------------------------------------------------------------

@dataclass
class CensusConfig:
    enabled: bool = False              # opt-in; off until census_enabled post-cook
    group: int = 2                     # EServerOctreeGroup.DINOPAWNS (wild; includes stasised). NO sweep.
    tile_size: float = 60000.0         # measured ~113ms/tile, ~80 dinos avg on Ragnarok
    z_layers: tuple = (-10000.0,)      # single layer captured ~96% on Ragnarok; add layers for tall maps (no cook)
    map_extent: float = 1200000.0      # cold/re-probe box half-extent (covers any ASA map; shrinks to learned bounds)
    subdivide_threshold: int = 600     # dinos in a cell over this -> subdivide (density-agnostic completeness)
    min_tile_size: float = 15000.0     # subdivision floor
    active_radius: float = 60000.0     # player-centric layer radius
    active_player_ttl: float = 600.0   # treat a player seen within this many seconds as "online"
    active_interval: float = 45.0      # near-player layer cadence (was 20s; widened 2026-06-24 to cut
                                       # getter-lock pressure — near-player data just goes ≤45s stale)
    full_pass_interval: float = 1800.0 # full map refresh cadence (30 min) — the freshness layer
    reprobe_interval: float = 21600.0  # re-scan the full map_extent box (catch new/moved regions) every 6h
    staleness_ttl: float = 5400.0      # reap rows not seen in 90 min (> full_pass_interval so live rows survive)
    wipe_ratio: float = 0.4            # total < 0.4*prev -> wipe/crater
    wipe_min_prev: int = 50            # ignore "craters" when prev was tiny
    wipe_settle: float = 180.0         # wait after a wipe before the re-pass (let respawns populate)
    converge_abs: int = 25             # within this many of prev -> "converged"
    converge_ratio: float = 0.05
    tile_interval: float = 0.15        # base inter-scan sleep; feedback-throttled from here
    min_interval: float = 0.05
    max_interval: float = 1.0
    latency_high: float = 0.4          # scan slower than this (busy thread) -> back off
    latency_low: float = 0.15          # faster than this (idle) -> speed up
    backoff: float = 1.6
    speedup: float = 0.75
    scan_timeout: float = 5.0          # < the 8s player-getter await: a stalled census tile frees the
                                       # shared _getter_lock sooner (no-cook getter-contention mitigation, 2026-06-24)


@dataclass
class CensusStats:
    cycles: int = 0
    tiles_scanned: int = 0
    active_scans: int = 0
    last_cycle_secs: float = 0.0
    last_full_count: int = 0
    bounds: tuple | None = None
    wipe_pending: bool = False
    wipe_settle_until: float = 0.0


# --- state codecs (server_state stores strings) ----------------------------------

def _enc(v) -> str:
    return json.dumps(v, separators=(",", ":"))


def _dec_json(s, default):
    if not s:
        return default
    try: return json.loads(s)
    except Exception: return default


def _dec_float(s, default: float) -> float:
    try: return float(s)
    except (TypeError, ValueError): return default


# --- scanning --------------------------------------------------------------------

async def _scan_cell(scan_fn, upsert_fn, cx: float, cy: float, size: float,
                     cfg: CensusConfig, collected: list,
                     z_layers=None, should_run=None) -> int:
    """Scan one square cell (center cx,cy, side `size`) on DINOPAWNS. Subdivide into 4 quadrants
    and recurse — bounded by min_tile_size — when the cell is too dense to scan in one shot, so
    completeness is density-agnostic. Upserts at the granularity actually scanned (the per-region
    departure-sweep bounds match), clipping each dino to the square that contains it (the circle
    over-reaches; border dinos belong to the neighbour cell). Appends (uid,x,y) of every upserted
    dino to `collected` for the pass's count + bbox. Returns rows upserted for this cell subtree.

    `scan_fn` may return None for a tile (a TIMEOUT / link blip — the cooked mod couldn't answer
    in scan_timeout, almost always because the cell is too dense to serialize). `None` is NOT an
    empty cell: it carries no departure information, so it must NEVER reach upsert_region with an
    empty list (V21-2 fix — that empty departure-sweep would DELETE every known dino in the square,
    a false removal that wiped near-player dinos every cycle). Instead we SPLIT proactively (a
    count-based check can't see a timeout — it returned no rows), which a timeout-masking reactive
    threshold misses; quadrants are ~1/4 as dense and resolve. If we bottom out at min_tile_size
    still timing out, we skip the upsert entirely (next pass retries) rather than false-sweep.

    `z_layers` overrides cfg.z_layers (the active layer scans only the player's own z).
    `should_run` (optional) short-circuits deep subdivision when the link drops mid-cell."""
    radius = (size * _SQRT2) / 2.0
    zs = cfg.z_layers if z_layers is None else z_layers
    raw: list[dict] = []
    no_result = False
    for z in zs:
        r = await scan_fn(cx, cy, z, radius, cfg.group)
        if r is None:                     # timeout / link blip — no usable data for this z
            no_result = True
            break
        raw += r
    cell = {d["dino_uid"]: d for d in raw if d.get("dino_uid")}   # dedup z-layer / edge overlap

    # Split when the cell is too dense to scan in one shot. Density shows up two ways: a successful
    # scan returned >= subdivide_threshold dinos (reactive), OR the scan TIMED OUT (no_result) — so
    # dense/slow the mod couldn't answer, which a count check can't detect (a timeout returns 0 rows).
    too_dense = no_result or len(cell) >= cfg.subdivide_threshold
    if too_dense and size / 2.0 >= cfg.min_tile_size:
        q = size / 2.0
        off = size / 4.0
        n = 0
        for dx, dy in ((-off, -off), (off, -off), (-off, off), (off, off)):
            if should_run is not None and not should_run():
                break
            n += await _scan_cell(scan_fn, upsert_fn, cx + dx, cy + dy, q, cfg,
                                   collected, z_layers, should_run)
        return n

    if no_result:
        # Bottomed out at the floor still timing out (a pathologically dense min cell, or a link
        # blip). CRITICAL: do NOT upsert — an empty departure-sweep here false-removes every known
        # dino in this square. Skip it; the next active/full pass re-scans and re-establishes it.
        logger.warning("census: cell (%.0f,%.0f) size=%.0f returned no result at floor; "
                       "skipping upsert (no false sweep)", cx, cy, size)
        return 0

    half = size / 2.0
    bx0, by0, bx1, by1 = cx - half, cy - half, cx + half, cy + half
    inside = [d for d in cell.values()
              if bx0 <= (d.get("x") or 0.0) < bx1 and by0 <= (d.get("y") or 0.0) < by1]
    for d in inside:
        collected.append((d["dino_uid"], d.get("x"), d.get("y")))
    return await upsert_fn((bx0, by0, bx1, by1), inside)


async def _active_layer(scan_fn, upsert_fn, players_fn, cfg: CensusConfig, stats: CensusStats,
                        should_run=None):
    """Keep dinos near online players fresh — they're woken (full stats valid) and that's where
    dinos actually change. Cheap: scales with player count, not the map total.

    Routes each player's neighbourhood through the subdividing `_scan_cell` (V21-2 fix). A near-
    player region is exactly where density spikes — a base full of tames, a cave, a Primal-Nemesis
    spawn cluster — and the old single `active_radius` scan (2x a full-pass tile's area, with NO
    subdivision) timed out there EVERY cycle, returning nothing and false-sweeping the index. Going
    through _scan_cell splits a dense neighbourhood into cells the mod can actually answer, and
    never upserts on a timeout. Sparse neighbourhoods cost exactly one scan, unchanged: the cell
    square (px +/- size/2) is the same inscribed square the old path swept (size = active_radius*sqrt2
    -> radius = active_radius), so coverage is identical when no split is needed."""
    try:
        players = await players_fn()
    except Exception:
        logger.exception("census players_fn error")
        return
    if not players:
        return
    interval = cfg.tile_interval
    z0 = cfg.z_layers[0]
    size = cfg.active_radius * _SQRT2     # _scan_cell radius = size*sqrt2/2 = active_radius
    for p in players:
        px, py = p.get("x"), p.get("y")
        if px is None or py is None:
            continue
        cz = p.get("z") if p.get("z") is not None else z0
        t0 = time.time()
        scratch: list = []
        await _scan_cell(scan_fn, upsert_fn, px, py, size, cfg, scratch,
                         z_layers=[cz], should_run=should_run)
        stats.active_scans += 1
        await asyncio.sleep(next_interval(time.time() - t0, interval, cfg))
        if should_run is not None and not should_run():
            break


async def _full_pass(scan_fn, upsert_fn, get_state, set_state,
                     cfg: CensusConfig, stats: CensusStats, should_run) -> int:
    """One full map pass: cold/periodic re-probe sweeps a generous bounded box; otherwise the
    learned populated bounds (+ a one-tile ring for expansion). Learns bounds from the dino
    bbox, persists last count, and flags a wipe on a population crater. Returns unique total."""
    now = time.time()
    bounds = _dec_json(await get_state("census_bounds"), None)
    if not (isinstance(bounds, list) and len(bounds) == 4):   # ignore unset/legacy formats -> re-probe
        bounds = None
    last_reprobe = _dec_float(await get_state("census_lastreprobe"), 0.0)
    reprobe = (bounds is None) or (now - last_reprobe >= cfg.reprobe_interval)
    if reprobe:
        e = cfg.map_extent
        area = (-e, -e, e, e)
    else:
        p = cfg.tile_size
        area = (bounds[0] - p, bounds[1] - p, bounds[2] + p, bounds[3] + p)

    t0 = time.time()
    interval = cfg.tile_interval
    collected: list = []
    for cx, cy, _r in tiles(area[0], area[1], area[2], area[3], cfg.tile_size):
        if not should_run():
            break
        c0 = time.time()
        await _scan_cell(scan_fn, upsert_fn, cx, cy, cfg.tile_size, cfg, collected,
                         should_run=should_run)
        stats.tiles_scanned += 1
        interval = next_interval(time.time() - c0, interval, cfg)
        await asyncio.sleep(interval)

    uids = {u for u, _, _ in collected}
    total = len(uids)
    elapsed = time.time() - t0
    prev = int(_dec_float(await get_state("census_lastcount"), 0))

    xs = [x for _, x, _ in collected if x is not None]
    ys = [y for _, _, y in collected if y is not None]
    bb = None
    if xs and ys:
        pad = cfg.tile_size
        bb = [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]
        await set_state("census_bounds", _enc(bb))
    await set_state("census_lastfull", repr(now))
    await set_state("census_lastcount", str(total))
    if reprobe:
        await set_state("census_lastreprobe", repr(now))

    stats.cycles += 1
    stats.last_cycle_secs = elapsed
    stats.last_full_count = total
    stats.bounds = tuple(bb) if bb else (tuple(bounds) if bounds else None)

    if is_wipe(total, prev, cfg):
        stats.wipe_pending = True
        stats.wipe_settle_until = time.time() + cfg.wipe_settle
        logger.info("census: WIPE/crater detected (%d -> %d); re-pass in ~%.0fs after respawn-settle",
                    prev, total, cfg.wipe_settle)
    else:
        stats.wipe_pending = False
        logger.info("census full pass: %d dinos, %d tiles, %.0fs, %s%s",
                    total, stats.tiles_scanned, elapsed,
                    "converged" if is_converged(total, prev, cfg) else "changing",
                    " (re-probe)" if reprobe else "")
    return total


async def run_census(*, scan_fn, upsert_fn, reap_fn, get_state, set_state,
                     players_fn, cfg: CensusConfig, stats: CensusStats, should_run) -> None:
    """Continuous per-world census loop. Injected:
      scan_fn(cx,cy,cz,radius,group) -> list[dino dict]
      upsert_fn(bounds, dinos) -> int            (per-region departure-sweep + insert)
      reap_fn(older_than) -> int                 (captured_at TTL staleness backstop)
      get_state(key)/set_state(key,val)          (server_state KV; strings)
      players_fn() -> list[{"x","y","z"}]        (online players for the active layer)
      should_run() -> bool                       (stop signal / connection alive)
    Active layer every active_interval; a full pass on cold-start / interval / post-wipe-settle."""
    logger.info("census loop start (group=%d tile=%.0f map_extent=%.0f full_pass=%.0fs)",
                cfg.group, cfg.tile_size, cfg.map_extent, cfg.full_pass_interval)
    while should_run():
        try:
            await _active_layer(scan_fn, upsert_fn, players_fn, cfg, stats, should_run)
        except Exception:
            logger.exception("census active-layer error")

        cold = (await get_state("census_bounds")) is None
        last_full = _dec_float(await get_state("census_lastfull"), 0.0)
        due = time.time() - last_full >= cfg.full_pass_interval
        wipe_due = stats.wipe_pending and time.time() >= stats.wipe_settle_until
        if should_run() and (cold or due or wipe_due):
            try:
                await _full_pass(scan_fn, upsert_fn, get_state, set_state, cfg, stats, should_run)
                reaped = await reap_fn(time.time() - cfg.staleness_ttl)
                if reaped:
                    logger.info("census reaped %d stale dino rows", reaped)
            except Exception:
                logger.exception("census full-pass error")

        await asyncio.sleep(cfg.active_interval)
