"""KNW-01 — get_server_status must tell the truth.

The old tool returned a hardcoded "game mod not connected" string regardless of
reality, so once V18 went live Sheldon was lying to players whenever the LLM
called it. The fix reads live state from the injected telemetry store. These
tests assert: (a) with a telemetry store present it does NOT emit the
"not connected" lie and reports live facts; (b) with no telemetry it returns an
honest "unknown / unavailable" fallback — never a false "disconnected" claim.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiosqlite

from sheldon_bridge.db.migrations import apply_migrations
from sheldon_bridge.db.telemetry_store import TelemetryStore
from sheldon_bridge.tools.knowledge import get_server_status


async def _store(tmp_path):
    db = await aiosqlite.connect(str(tmp_path / "t.db"))
    db.row_factory = aiosqlite.Row
    await apply_migrations(db, "telemetry")
    return db, TelemetryStore(db)


def _is_lie(text: str) -> bool:
    t = (text or "").lower()
    return "not connected" in t or "not yet available" in t or "not yet installed" in t


async def test_does_not_lie_when_telemetry_present(tmp_path):
    db, tel = await _store(tmp_path)
    try:
        # Seed a little live world state so the honest reply has something to report.
        await tel.swap_dino_index([
            {"dino_uid": "Rex_C_1", "species": "Rex", "level": 145, "x": 100.0, "y": 200.0},
            {"dino_uid": "Giga_C_1", "species": "Giganotosaurus", "level": 290, "x": 5.0, "y": 5.0},
        ])
        res = await get_server_status(ctx={"telemetry": tel})

        # Core: it must NOT claim the mod is disconnected.
        assert not _is_lie(res.get("message", "")), res
        assert res.get("connected") is True
        assert res.get("status") == "online"
        # And it surfaces the cheap live facts.
        assert res.get("tracked_dinos") == 2
        assert "recent_players" in res
    finally:
        await db.close()


async def test_honest_fallback_when_no_telemetry(tmp_path):
    # Mock/unconfigured ctx: telemetry key absent entirely.
    res = await get_server_status(ctx={})
    assert not _is_lie(res.get("message", "")), res
    assert res.get("connected") is None          # unknown, NOT a false "disconnected"
    assert res.get("status") == "unknown"

    # ctx itself None (registry only injects ctx when the tool declares it; still must be safe).
    res2 = await get_server_status(ctx=None)
    assert not _is_lie(res2.get("message", "")), res2
    assert res2.get("connected") is None
