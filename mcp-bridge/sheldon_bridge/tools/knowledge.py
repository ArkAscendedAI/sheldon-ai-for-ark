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
            try:
                dinos = json.loads(f.read_text())
                _dino_db.extend(dinos)
                logger.info(f"Loaded {len(dinos)} dinos from {f.name}")
            except Exception as e:
                logger.error(f"Failed to load {f}: {e}")

        # Load alias files
        for f in sorted(path.glob("aliases*.json")):
            try:
                aliases = json.loads(f.read_text())
                _dino_aliases.update(aliases.get("dinos", {}))
                logger.info(f"Loaded {len(aliases.get('dinos', {}))} dino aliases from {f.name}")
            except Exception as e:
                logger.error(f"Failed to load {f}: {e}")

        # Load item files
        for f in sorted(path.glob("items*.json")):
            try:
                items = json.loads(f.read_text())
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
        mod_filter: Optional mod name to filter results (e.g., "Primal Nemesis")
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


@tool(tier="player", description="Get current server status and information")
def get_server_status(ctx: dict | None = None) -> dict[str, Any]:
    """Get information about the current server including name, rates, and player count.

    Returns server name, map, rates, mod count, and online player count.
    """
    # In v0.1, this returns static info from config. When the game mod is connected,
    # it will query live data via WebSocket.
    return {
        "status": "online",
        "message": "Server status is not yet available (game mod not connected). "
        "This will show live data once the in-game mod is installed.",
    }


def _get_dino_suggestions(query: str) -> list[str]:
    """Get close-match suggestions for a failed dino search."""
    if not _all_dino_names:
        return []

    matches = process.extract(
        query.lower(), _all_dino_names.keys(), scorer=fuzz.WRatio, limit=3, score_cutoff=40
    )
    return [_all_dino_names[m[0]] for m in matches]
