"""Dino-census pure logic + staleness tests.

Covers the no-cook census rewrite (census.py): the two parse fixes (level-from-descriptive-
name, pipe-in-name misalignment), the geometry/throttle/wipe helpers, reactive subdivision
(density-agnostic completeness), and the staleness model (upsert_region departure-sweep +
reap_stale TTL backstop) against a real temp telemetry.db. Async tests run under
pytest-asyncio (asyncio_mode=auto).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiosqlite

from sheldon_bridge import census
from sheldon_bridge.census import (
    CensusConfig, CensusStats, parse_record, parse_scan_reply, tiles, next_interval,
    is_wipe, is_converged, _scan_cell, _active_layer,
)
from sheldon_bridge.db.migrations import apply_migrations
from sheldon_bridge.db.telemetry_store import TelemetryStore

STATS14 = [str(i) for i in range(14)]  # hp_c..speed


def rec(uid="Rex_C_1", species="Rex", level="145", gender="false", team="2",
        tamed="", loc="X=100 Y=200 Z=10", stats=None):
    return "|".join([uid, species, level, gender, team, tamed, loc] + (stats or STATS14))


# --- parse_record: the two fixes ------------------------------------------------

def test_parse_clean_record():
    d = parse_record(rec(stats=["3000", "3000"] + ["50"] * 12))
    assert d["dino_uid"] == "Rex_C_1"
    assert d["species"] == "Rex"
    assert d["level"] == 145
    assert d["gender"] == "M"           # "false" -> male
    assert d["tribe"] == "2"
    assert (d["x"], d["y"], d["z"]) == (100.0, 200.0, 10.0)
    assert d["base_stats"]["hp_c"] == 3000.0 and d["base_stats"]["speed"] == 50.0


def test_level_parsed_from_descriptive_name_when_field_zero():
    # AbsoluteBaseLevel reads 0 on stasised dinos; the level is baked into the name.
    d = parse_record(rec(species="Megalodon - Lvl 300", level="0"))
    assert d["level"] == 300
    assert d["species"] == "Megalodon"   # suffix stripped to a clean species


def test_level_field_wins_when_valid_but_name_still_cleaned():
    d = parse_record(rec(species="Rex - Lvl 5", level="145"))
    assert d["level"] == 145             # real field value preferred
    assert d["species"] == "Rex"         # descriptor still stripped


def test_gender_female():
    assert parse_record(rec(gender="true"))["gender"] == "F"


def test_pipe_in_species_name_misalignment_salvaged():
    # a modded GetDescriptiveName containing '|' yields surplus parts; wild TamedName is
    # empty so the surplus is the species -> re-joined, rigid tail stays aligned.
    d = parse_record(rec(species="Rock Drake|Variant", level="0",
                         loc="X=1 Y=2 Z=3", stats=["7"] * 14))
    assert d is not None
    assert d["species"] == "Rock Drake|Variant"
    assert (d["x"], d["y"], d["z"]) == (1.0, 2.0, 3.0)
    assert d["base_stats"]["hp_c"] == 7.0


def test_fragment_too_few_fields_dropped():
    assert parse_record("Rex_C_1|Rex|145") is None


def test_misaligned_loc_dropped():
    # a ';' inside a name fragments a record so the loc slot holds a non-vector -> drop,
    # never silently misparse. Build a 21-field record whose loc field isn't a vector.
    parts = ["uid", "Rex", "1", "false", "2", "", "NOT_A_VECTOR"] + STATS14
    assert parse_record("|".join(parts)) is None


def test_parse_scan_reply_envelope_and_raw():
    body = rec(uid="A") + ";" + rec(uid="B") + ";"
    assert {d["dino_uid"] for d in parse_scan_reply('{"type":"dino_scan","data":"%s"}' % body)} == {"A", "B"}
    assert {d["dino_uid"] for d in parse_scan_reply("dino_scan|" + body)} == {"A", "B"}
    assert parse_scan_reply("") == []


# --- geometry / throttle / detection helpers ------------------------------------

def test_tiles_cover_grid():
    ts = list(tiles(0, 0, 120000, 120000, 60000))
    assert len(ts) == 4                  # 2x2
    assert all(r > 60000 / 2 for _, _, r in ts)   # radius is half-diagonal (covers corners)


def test_next_interval_feedback():
    cfg = CensusConfig()
    assert next_interval(0.9, 0.2, cfg) > 0.2        # busy -> back off
    assert next_interval(0.01, 0.2, cfg) < 0.2       # idle -> speed up
    assert next_interval(0.9, 100.0, cfg) == cfg.max_interval   # clamped high
    assert next_interval(0.0, 0.0001, cfg) == cfg.min_interval  # clamped low


def test_wipe_and_converge():
    cfg = CensusConfig()
    assert is_wipe(10, 1000, cfg)                    # crater
    assert not is_wipe(900, 1000, cfg)               # normal variance
    assert not is_wipe(1, 10, cfg)                   # prev too small to judge
    assert is_converged(1000, 1010, cfg)            # within tolerance
    assert not is_converged(500, 1000, cfg)          # big change
    assert not is_converged(5, 0, cfg)               # no prior baseline


# --- reactive subdivision -------------------------------------------------------

class FakeWorld:
    """A fixed set of (uid,x,y) dinos; scan() returns those within the queried circle."""
    def __init__(self, dinos):
        self.dinos = dinos
        self.upserts = []           # (bounds, [uids])
        self.scan_sizes = []        # radii queried

    async def scan(self, cx, cy, cz, radius, group):
        self.scan_sizes.append(radius)
        return [{"dino_uid": u, "x": x, "y": y, "species": "Rex", "level": 1,
                 "gender": "M", "tribe": "2", "name": None, "imprint": None, "base_stats": {}}
                for u, x, y in self.dinos if (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius]

    async def upsert(self, bounds, dinos):
        self.upserts.append((bounds, [d["dino_uid"] for d in dinos]))
        return len(dinos)


async def test_scan_cell_sparse_single_upsert():
    cfg = CensusConfig(subdivide_threshold=4, min_tile_size=10000)
    w = FakeWorld([("A", 0, 0), ("B", 1000, 1000)])     # 2 < threshold
    collected = []
    n = await _scan_cell(w.scan, w.upsert, 0, 0, 60000, cfg, collected)
    assert n == 2
    assert len(w.upserts) == 1                            # no subdivision
    assert {u for u, _, _ in collected} == {"A", "B"}


async def test_scan_cell_dense_subdivides_each_dino_once():
    cfg = CensusConfig(subdivide_threshold=4, min_tile_size=10000)
    # 6 dinos spread across quadrants of a 60000 cell centered at origin -> top cell returns
    # 6 (>=4) -> subdivide; each quadrant holds <=2 -> leaf.
    pts = [("A", -15000, -15000), ("B", -14000, -16000),
           ("C", 15000, -15000), ("D", 16000, -14000),
           ("E", -15000, 15000), ("F", 15000, 15000)]
    w = FakeWorld(pts)
    collected = []
    n = await _scan_cell(w.scan, w.upsert, 0, 0, 60000, cfg, collected)
    assert n == 6
    assert len(w.upserts) > 1                             # subdivided
    # every dino upserted EXACTLY once across leaf cells (clip prevents double-counting)
    all_upserted = [u for _, uids in w.upserts for u in uids]
    assert sorted(all_upserted) == ["A", "B", "C", "D", "E", "F"]
    assert {u for u, _, _ in collected} == set("ABCDEF")


async def test_scan_cell_respects_subdivision_floor():
    cfg = CensusConfig(subdivide_threshold=2, min_tile_size=40000)   # floor near top size
    pts = [("A", 0, 0), ("B", 100, 100), ("C", 200, 200), ("D", 300, 300)]  # all clustered
    w = FakeWorld(pts)
    collected = []
    await _scan_cell(w.scan, w.upsert, 0, 0, 60000, cfg, collected)
    # 60000/2 = 30000 < min 40000 -> cannot subdivide -> single leaf despite being over threshold
    assert len(w.upserts) == 1
    assert {u for u, _, _ in collected} == set("ABCD")


# --- V21-2: timeout-driven split + no-false-sweep (the near-player slow-scan fix) -----

class CapWorld(FakeWorld):
    """A FakeWorld whose mod 'times out' (scan returns None) when a single octree query would
    return MORE than `cap` dinos — the dense Primal-Nemesis near-player failure mode (the cooked
    mod can't serialize a too-dense cell within scan_timeout). Splitting cuts per-cell density
    below the cap, so the smaller scans then succeed. Counts timeouts for assertions."""
    def __init__(self, dinos, cap):
        super().__init__(dinos)
        self.cap = cap
        self.timeouts = 0

    async def scan(self, cx, cy, cz, radius, group):
        self.scan_sizes.append(radius)
        hit = [(u, x, y) for u, x, y in self.dinos
               if (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius]
        if len(hit) > self.cap:
            self.timeouts += 1
            return None                  # mod couldn't answer in time → bridge sees a timeout
        return [{"dino_uid": u, "x": x, "y": y, "species": "Rex", "level": 1, "gender": "M",
                 "tribe": "2", "name": None, "imprint": None, "base_stats": {}} for u, x, y in hit]


async def test_scan_cell_splits_on_timeout_not_just_count():
    # subdivide_threshold disabled (1000) so ONLY a timeout can trigger the split — proves the
    # proactive timeout-split, independent of the count threshold a timeout can't reach (it
    # returns 0 rows). 6 dinos, one pair per quadrant; cap=4 → the whole cell times out, each
    # quadrant (<=2) succeeds.
    cfg = CensusConfig(subdivide_threshold=1000, min_tile_size=10000)
    pts = [("A", -15000, -15000), ("B", -14000, -16000),
           ("C", 15000, -15000), ("D", 16000, -14000),
           ("E", -15000, 15000), ("F", 15000, 15000)]
    w = CapWorld(pts, cap=4)
    collected = []
    n = await _scan_cell(w.scan, w.upsert, 0, 0, 60000, cfg, collected)
    assert w.timeouts >= 1                                # the top cell timed out...
    assert len(w.upserts) > 1                             # ...so it SPLIT (not count-driven)
    assert n == 6
    all_upserted = sorted(u for _, uids in w.upserts for u in uids)
    assert all_upserted == ["A", "B", "C", "D", "E", "F"]  # every dino landed, none lost
    # no upsert ever ran on a None/timeout (would be an empty-list false sweep with dinos present)
    assert all(uids for _, uids in w.upserts)


async def test_scan_cell_timeout_at_floor_skips_upsert_no_false_sweep():
    # cap=0 → EVERY non-empty scan times out, even sub-cells; floor near top so it can't split.
    # The cell must be SKIPPED (no upsert) — an empty departure-sweep here would delete every
    # known dino in the square (the bug). Returns 0, calls upsert ZERO times.
    cfg = CensusConfig(subdivide_threshold=2, min_tile_size=40000)
    w = CapWorld([("A", 0, 0), ("B", 100, 100)], cap=0)
    collected = []
    n = await _scan_cell(w.scan, w.upsert, 0, 0, 60000, cfg, collected)
    assert w.timeouts >= 1
    assert n == 0
    assert w.upserts == []                                # NEVER false-sweeps the square
    assert collected == []


async def test_scan_cell_genuine_empty_still_sweeps():
    # A real empty reply ([]) is NOT a timeout: the area genuinely has no dinos now, so the
    # departure sweep SHOULD run (any dino that left is removed). Distinct from None.
    cfg = CensusConfig(subdivide_threshold=4, min_tile_size=10000)
    w = CapWorld([], cap=10)                              # scan returns [] (0 <= cap), not None
    collected = []
    n = await _scan_cell(w.scan, w.upsert, 0, 0, 60000, cfg, collected)
    assert w.timeouts == 0
    assert n == 0
    assert len(w.upserts) == 1                            # the sweep DID run (empty area)
    assert w.upserts[0][1] == []


async def test_active_layer_subdivides_dense_region():
    # The reported bug: a dense near-player region. The active layer must split it (not time out
    # every cycle). cap=4, 6 dinos around the player at origin within active_radius.
    cfg = CensusConfig(subdivide_threshold=1000, min_tile_size=10000, active_radius=60000)
    pts = [("A", -20000, -20000), ("B", -19000, -21000),
           ("C", 20000, -20000), ("D", 21000, -19000),
           ("E", -20000, 20000), ("F", 20000, 20000)]
    w = CapWorld(pts, cap=4)
    stats = CensusStats()

    async def players_fn():
        return [{"x": 0, "y": 0, "z": 0}]

    await _active_layer(w.scan, w.upsert, players_fn, cfg, stats)
    assert stats.active_scans == 1
    assert w.timeouts >= 1                                # the wide near-player scan timed out...
    assert len(w.upserts) > 1                             # ...and split instead of dying
    assert sorted(u for _, uids in w.upserts for u in uids) == ["A", "B", "C", "D", "E", "F"]


async def test_active_layer_timeout_floor_no_false_removal():
    # If even the floor near-player cell times out (cap=0) and can't split, the active layer must
    # NOT upsert — pre-fix it false-swept the player's whole neighbourhood every cycle.
    cfg = CensusConfig(min_tile_size=200000, active_radius=60000)   # floor > cell → no split
    w = CapWorld([("A", 1000, 1000), ("B", -1000, -1000)], cap=0)
    stats = CensusStats()

    async def players_fn():
        return [{"x": 0, "y": 0, "z": 0}]

    await _active_layer(w.scan, w.upsert, players_fn, cfg, stats)
    assert stats.active_scans == 1
    assert w.timeouts >= 1
    assert w.upserts == []                                # no false sweep of near-player dinos


async def test_active_layer_sparse_single_scan_unchanged():
    # The common case must be untouched: a sparse neighbourhood = exactly one scan, one upsert,
    # to the same square the old inscribed-square path used (px +/- active_radius/sqrt2).
    cfg = CensusConfig(subdivide_threshold=600, active_radius=60000)
    w = CapWorld([("A", 1000, 1000), ("B", -2000, 3000)], cap=600)
    stats = CensusStats()

    async def players_fn():
        return [{"x": 0, "y": 0, "z": 0}]

    await _active_layer(w.scan, w.upsert, players_fn, cfg, stats)
    assert w.timeouts == 0
    assert len(w.upserts) == 1
    (bx0, by0, bx1, by1), uids = w.upserts[0]
    r_in = 60000 / census._SQRT2
    assert abs(bx0 - (-r_in)) < 1 and abs(bx1 - r_in) < 1   # same inscribed square as before
    assert sorted(uids) == ["A", "B"]


# --- staleness: departure-sweep + reaper (real temp telemetry.db) ---------------

def _dino(uid, x, y):
    return {"dino_uid": uid, "species": "Rex", "name": None, "level": 1, "gender": "M",
            "tribe": "2", "imprint": None, "base_stats": {}, "x": x, "y": y, "z": 0}


async def _store(tmp_path):
    db = await aiosqlite.connect(str(tmp_path / "t.db"))
    db.row_factory = aiosqlite.Row
    await apply_migrations(db, "telemetry")
    return db, TelemetryStore(db)


async def test_upsert_region_departure_sweep(tmp_path):
    db, store = await _store(tmp_path)
    b = (0, 0, 1000, 1000)
    await store.upsert_region(b, [_dino("A", 10, 10), _dino("B", 20, 20)])
    assert await store.count_dinos() == 2
    await store.upsert_region(b, [_dino("A", 10, 10)])     # B departed the tile
    rows = await store.search_dinos(limit=10)
    assert [r["dino_uid"] for r in rows] == ["A"]          # B swept
    await db.close()


async def test_upsert_region_dedup_on_move(tmp_path):
    db, store = await _store(tmp_path)
    await store.upsert_region((0, 0, 1000, 1000), [_dino("A", 10, 10)])
    await store.upsert_region((5000, 5000, 6000, 6000), [_dino("A", 5500, 5500)])  # A moved
    assert await store.count_dinos() == 1                  # one A, not two (uid dedup)
    row = (await store.search_dinos(limit=1))[0]
    assert row["x"] == 5500
    await db.close()


async def test_reap_stale_removes_only_old_and_cleans_rtree(tmp_path):
    db, store = await _store(tmp_path)
    now = time.time()
    await store.upsert_region((0, 0, 1000, 1000), [_dino("OLD", 10, 10)])
    await db.execute("UPDATE dino_index SET captured_at=? WHERE dino_uid='OLD'", (now - 10000,))
    await db.commit()
    await store.upsert_region((2000, 2000, 3000, 3000), [_dino("NEW", 2010, 2010)])
    reaped = await store.reap_stale(now - 5400)
    assert reaped == 1
    assert await store.count_dinos() == 1                  # only NEW
    assert [r["dino_uid"] for r in await store.dinos_within(10, 10, 500)] == []  # rtree cleaned
    assert await store.reap_stale(now - 5400) == 0         # idempotent
    await db.close()
