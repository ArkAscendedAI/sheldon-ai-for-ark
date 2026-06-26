"""Knowledge-base tools — ARK encyclopedia lookups.

These tools query the local data layer (JSON files) and return structured
information about dinos, items, recipes, and map locations. They do NOT
interact with the game server.

Available to all tiers (player, admin, superadmin).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

from sheldon_bridge.tools.registry import tool

logger = logging.getLogger(__name__)

# In-memory data stores (populated by load_data())
_dino_db: list[dict] = []
_dino_aliases: dict[str, str] = {}  # alias -> canonical name
_item_db: list[dict] = []
_all_dino_names: dict[str, str] = {}  # searchable name -> canonical name


def load_data(data_dirs: list[str]) -> None:
    """Load all knowledge base data from JSON files.

    Called once at startup. Loads from multiple directories (vanilla + custom),
    with custom data overlaying/extending vanilla.
    """
    global _dino_db, _dino_aliases, _item_db, _all_dino_names

    _dino_db = []
    _dino_aliases = {}
    _item_db = []
    _all_dino_names = {}

    for data_dir in data_dirs:
        path = Path(data_dir)
        if not path.exists():
            logger.debug(f"Data directory not found, skipping: {data_dir}")
            continue

        # Load dino files
        for f in sorted(path.glob("dinos*.json")):
            if "sample" in f.name:
                continue
            try:
                raw = json.loads(f.read_text())
                # Handle both wrapped {"dinos": [...], "aliases": {...}} and raw list format
                if isinstance(raw, dict):
                    dinos = raw.get("dinos", [])
                    _dino_aliases.update(raw.get("aliases", {}))
                else:
                    dinos = raw
                _dino_db.extend(dinos)
                logger.info(f"Loaded {len(dinos)} dinos from {f.name}")
            except Exception as e:
                logger.error(f"Failed to load {f}: {e}")

        # Load item files
        for f in sorted(path.glob("items*.json")):
            try:
                raw = json.loads(f.read_text())
                # Handle both wrapped {"items": [...]} and raw list format
                if isinstance(raw, dict):
                    items = raw.get("items", [])
                else:
                    items = raw
                _item_db.extend(items)
                logger.info(f"Loaded {len(items)} items from {f.name}")
            except Exception as e:
                logger.error(f"Failed to load {f}: {e}")

    # Build searchable name index
    for dino in _dino_db:
        name = dino.get("name", "")
        _all_dino_names[name.lower()] = name
        for nick in dino.get("nicknames", []):
            _all_dino_names[nick.lower()] = name

    # Add aliases to the index
    for alias, canonical in _dino_aliases.items():
        _all_dino_names[alias.lower()] = canonical

    logger.info(
        f"Knowledge base loaded: {len(_dino_db)} dinos, "
        f"{len(_item_db)} items, {len(_dino_aliases)} aliases"
    )


def _search_dinos(query: str, mod_filter: str = "", limit: int = 5) -> list[dict]:
    """Search for dinos by name, nickname, or fuzzy match."""
    query_lower = query.lower().strip()

    # Tier 1: Exact match on name
    for dino in _dino_db:
        if query_lower == dino.get("name", "").lower():
            if not mod_filter or mod_filter.lower() in dino.get("mod", "").lower():
                return [dino]

    # Tier 2: Exact match on nickname/alias
    canonical = _all_dino_names.get(query_lower)
    if canonical:
        results = [d for d in _dino_db if d.get("name") == canonical]
        if mod_filter:
            filtered = [d for d in results if mod_filter.lower() in d.get("mod", "").lower()]
            if filtered:
                return filtered
        return results[:limit]

    # Tier 3: Fuzzy match
    if not _all_dino_names:
        return []

    matches = process.extract(
        query_lower,
        _all_dino_names.keys(),
        scorer=fuzz.WRatio,
        limit=limit,
        score_cutoff=55,
    )

    results = []
    seen = set()
    for match_key, score, _ in matches:
        canonical_name = _all_dino_names[match_key]
        if canonical_name in seen:
            continue
        seen.add(canonical_name)
        for dino in _dino_db:
            if dino.get("name") == canonical_name:
                if not mod_filter or mod_filter.lower() in dino.get("mod", "").lower():
                    results.append(dino)
                break

    return results[:limit]


@tool(tier="player", description="Look up a dinosaur by name, nickname, or partial match")
def lookup_dino(query: str, mod_filter: str = "") -> dict[str, Any]:
    """Search for a dinosaur by common name, nickname, species name, or partial match.

    Returns blueprint path, taming info, stats, and mod variants.
    Use this whenever a player mentions a dino and you need the blueprint or details.

    Args:
        query: The dino name or nickname to search for (e.g., "furry rex", "yuty", "Rex")
        mod_filter: Optional mod name to filter results (e.g., a mod name)
    """
    results = _search_dinos(query, mod_filter)

    if not results:
        # Try without mod filter as fallback
        if mod_filter:
            results = _search_dinos(query)

    if not results:
        return {
            "found": False,
            "query": query,
            "message": f"No dino found matching '{query}'. Try a different name or nickname.",
            "suggestions": _get_dino_suggestions(query),
        }

    if len(results) == 1:
        dino = results[0]
        return {
            "found": True,
            "name": dino.get("name"),
            "blueprint": dino.get("blueprint", "unknown"),
            "nicknames": dino.get("nicknames", []),
            "diet": dino.get("diet", "unknown"),
            "temperament": dino.get("temperament", "unknown"),
            "tameable": dino.get("tameable", True),
            "taming": dino.get("taming", {}),
            "mod": dino.get("mod", "vanilla"),
            "variants": dino.get("variants", []),
        }

    # Multiple results
    return {
        "found": True,
        "multiple": True,
        "count": len(results),
        "results": [
            {
                "name": d.get("name"),
                "blueprint": d.get("blueprint", "unknown"),
                "mod": d.get("mod", "vanilla"),
            }
            for d in results
        ],
    }


@tool(tier="player", description="Look up an item by name for blueprint, recipe, or crafting info")
def lookup_item(query: str) -> dict[str, Any]:
    """Search for an item by name or partial match.

    Returns blueprint path, crafting recipe, stack size, and weight.
    Use this for any item-related questions.

    Args:
        query: The item name to search for (e.g., "metal ingot", "rex saddle")
    """
    query_lower = query.lower().strip()

    # Exact match
    for item in _item_db:
        if query_lower == item.get("name", "").lower():
            return {"found": True, **item}

    # Fuzzy match
    item_names = {item.get("name", "").lower(): item for item in _item_db}
    matches = process.extract(query_lower, item_names.keys(), scorer=fuzz.WRatio, limit=3)

    if matches and matches[0][1] >= 60:
        best_match = item_names[matches[0][0]]
        return {"found": True, **best_match}

    return {
        "found": False,
        "query": query,
        "message": f"No item found matching '{query}'.",
    }


@tool(tier="player", description="Get current server status and live world facts (connection state, tracked dino count, recently-seen players)")
async def get_server_status(ctx: dict | None = None) -> dict[str, Any]:
    """Report the live status of the connected ARK server.

    Reads the telemetry store (populated by the in-game mod) for cheap liveness
    facts: how many dinos the background census is currently tracking, how many
    survivors have been seen recently, how stale that data is, and any
    server-state keys the map has published. Use this to answer "is the server
    up?" / "how's the server doing?" honestly.

    Args:
        ctx: Injected context carrying the telemetry store.
    """
    telemetry = ctx.get("telemetry") if ctx else None
    if telemetry is None:
        # No telemetry wired (mock client / unconfigured ctx). Report honestly that we
        # can't read live state right now — NEVER claim the mod is disconnected.
        return {
            "status": "unknown",
            "connected": None,
            "message": "Live server status is unavailable right now (no telemetry "
            "available in this context). The bridge is running, but I can't read live "
            "world state.",
        }

    # The mod is connected — report truthful live facts. Each read is cheap (indexed
    # COUNT / recent-window scan) and individually guarded so a single failed query
    # never makes the whole status tool error out.
    result: dict[str, Any] = {
        "status": "online",
        "connected": True,
        "message": "Server is connected and the in-game mod is reporting live data.",
    }

    try:
        result["tracked_dinos"] = await telemetry.count_dinos()
    except Exception:
        pass

    try:
        # Survivors whose state was captured in the last 5 minutes ~= recently active.
        import time

        recent = await telemetry.recent_players(time.time() - 300)
        result["recent_players"] = len(recent)
    except Exception:
        pass

    try:
        import time

        fresh = await telemetry.newest_capture()
        if fresh:
            result["census_age_seconds"] = round(time.time() - fresh)
    except Exception:
        pass

    try:
        state = await telemetry.get_server_state()
        if state:
            result["server_state_keys"] = sorted(state.keys())
    except Exception:
        pass

    return result


def _get_dino_suggestions(query: str) -> list[str]:
    """Get close-match suggestions for a failed dino search."""
    if not _all_dino_names:
        return []

    matches = process.extract(
        query.lower(), _all_dino_names.keys(), scorer=fuzz.WRatio, limit=3, score_cutoff=40
    )
    return [_all_dino_names[m[0]] for m in matches]
