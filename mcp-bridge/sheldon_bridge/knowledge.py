"""
ARK knowledge base — searchable dino, item, engram, and spawn data.

Loads JSON files from data directories at startup. Provides tiered search:
exact match → alias match → fuzzy match.
"""

import json
import logging
from pathlib import Path
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """A search result with the matched entry, confidence score, and match type."""
    entry: dict
    score: float
    match_type: str  # "exact", "alias", "fuzzy"


class KnowledgeBase:
    """Loads and searches the ARK data files."""

    def __init__(self, data_dirs: list[str | Path]):
        self.dinos: list[dict] = []
        self.items: list[dict] = []
        self.engrams: list[dict] = []
        self.spawns: dict[str, dict] = {}  # map_name -> spawn data

        self._dino_by_name: dict[str, dict] = {}
        self._dino_aliases: dict[str, str] = {}
        self._dino_search_choices: dict[str, str] = {}  # display_name -> canonical_name

        self._item_by_name: dict[str, dict] = {}
        self._item_search_choices: dict[str, str] = {}

        for data_dir in data_dirs:
            self._load_dir(Path(data_dir))

        self._build_search_indices()
        logger.info(
            f"Knowledge base loaded: {len(self.dinos)} dinos, "
            f"{len(self.items)} items, {len(self.engrams)} engrams, "
            f"{len(self.spawns)} maps"
        )

    def _load_dir(self, data_dir: Path):
        """Load all JSON files from a data directory."""
        if not data_dir.exists():
            logger.warning(f"Data directory not found: {data_dir}")
            return

        # Dinos (load all dinos*.json files — vanilla + mod data)
        for dino_file in sorted(data_dir.glob("dinos*.json")):
            if "sample" in dino_file.name:
                continue
            try:
                with open(dino_file) as f:
                    data = json.load(f)
                new_dinos = data.get("dinos", [])
                self.dinos.extend(new_dinos)
                # Merge aliases
                for alias, canonical in data.get("aliases", {}).items():
                    self._dino_aliases[alias.lower()] = canonical
                logger.info(f"  Loaded {len(new_dinos)} dinos from {dino_file.name}")
            except Exception as e:
                logger.error(f"  Failed to load {dino_file.name}: {e}")

        # Items
        item_file = data_dir / "items.json"
        if item_file.exists():
            with open(item_file) as f:
                data = json.load(f)
            new_items = data.get("items", [])
            self.items.extend(new_items)
            logger.info(f"  Loaded {len(new_items)} items from {item_file}")

        # Engrams
        engram_file = data_dir / "engrams.json"
        if engram_file.exists():
            with open(engram_file) as f:
                data = json.load(f)
            new_engrams = data.get("engrams", [])
            self.engrams.extend(new_engrams)
            logger.info(f"  Loaded {len(new_engrams)} engrams from {engram_file}")

        # Spawn maps
        for spawn_file in data_dir.glob("spawns-*.json"):
            map_name = spawn_file.stem.replace("spawns-", "")
            with open(spawn_file) as f:
                data = json.load(f)
            self.spawns[map_name] = data
            logger.info(f"  Loaded spawn data for {map_name}")

    def _build_search_indices(self):
        """Build lookup dicts and search choice lists for fuzzy matching."""
        # Dino indices
        for dino in self.dinos:
            name = dino["name"]
            self._dino_by_name[name] = dino
            self._dino_by_name[name.lower()] = dino
            # Add to search choices
            self._dino_search_choices[name] = name
            for nick in dino.get("nicknames", []):
                self._dino_search_choices[nick] = name

        # Add aliases to search choices
        for alias, canonical in self._dino_aliases.items():
            self._dino_search_choices[alias] = canonical

        # Item indices
        for item in self.items:
            name = item["name"]
            self._item_by_name[name] = item
            self._item_by_name[name.lower()] = item
            self._item_search_choices[name] = name

    def search_dino(self, query: str, limit: int = 5) -> list[SearchResult]:
        """Search for a dino by name, nickname, or fuzzy match."""
        query_lower = query.lower().strip()

        # Tier 1: Exact name match
        exact = self._dino_by_name.get(query_lower)
        if exact:
            return [SearchResult(entry=exact, score=100.0, match_type="exact")]

        # Tier 2: Exact alias match
        canonical = self._dino_aliases.get(query_lower)
        if canonical:
            dino = self._dino_by_name.get(canonical) or self._dino_by_name.get(canonical.lower())
            if dino:
                return [SearchResult(entry=dino, score=95.0, match_type="alias")]

        # Tier 3: Fuzzy match against names + nicknames + aliases
        results = process.extract(
            query,
            self._dino_search_choices.keys(),
            scorer=fuzz.WRatio,
            limit=limit,
            score_cutoff=55,
        )

        search_results = []
        seen_names: set[str] = set()
        for match_key, score, _ in results:
            canonical_name = self._dino_search_choices[match_key]
            if canonical_name in seen_names:
                continue
            seen_names.add(canonical_name)
            dino = self._dino_by_name.get(canonical_name) or self._dino_by_name.get(
                canonical_name.lower()
            )
            if dino:
                search_results.append(
                    SearchResult(entry=dino, score=score, match_type="fuzzy")
                )

        return search_results

    def search_item(self, query: str, limit: int = 5) -> list[SearchResult]:
        """Search for an item by name or fuzzy match."""
        query_lower = query.lower().strip()

        # Tier 1: Exact match
        exact = self._item_by_name.get(query_lower)
        if exact:
            return [SearchResult(entry=exact, score=100.0, match_type="exact")]

        # Tier 2: Fuzzy match
        results = process.extract(
            query,
            self._item_search_choices.keys(),
            scorer=fuzz.WRatio,
            limit=limit,
            score_cutoff=55,
        )

        search_results = []
        seen_names: set[str] = set()
        for match_key, score, _ in results:
            canonical_name = self._item_search_choices[match_key]
            if canonical_name in seen_names:
                continue
            seen_names.add(canonical_name)
            item = self._item_by_name.get(canonical_name) or self._item_by_name.get(
                canonical_name.lower()
            )
            if item:
                search_results.append(
                    SearchResult(entry=item, score=score, match_type="fuzzy")
                )

        return search_results

    def get_spawn_locations(self, dino_name: str, map_name: str = "") -> dict:
        """Get spawn locations for a dino on a specific map (or all maps)."""
        results = {}
        maps_to_check = [map_name] if map_name else list(self.spawns.keys())

        for m in maps_to_check:
            spawn_data = self.spawns.get(m, {})
            spawns = spawn_data.get("spawns", {})

            # Try exact match first, then partial
            locations = spawns.get(dino_name, [])
            if not locations:
                # Fuzzy match against spawn entry names
                for spawn_name, locs in spawns.items():
                    if dino_name.lower() in spawn_name.lower():
                        locations = locs
                        break

            if locations:
                results[m] = locations

        return results

    def format_dino_info(self, dino: dict) -> str:
        """Format a dino entry as a human-readable string for the LLM."""
        lines = [f"**{dino['name']}**"]

        if dino.get("nicknames"):
            lines.append(f"Also known as: {', '.join(dino['nicknames'])}")

        lines.append(f"Blueprint: {dino['blueprint']}")

        if dino.get("diet"):
            lines.append(f"Diet: {dino['diet']}")
        if dino.get("temperament"):
            lines.append(f"Temperament: {dino['temperament']}")
        if dino.get("groups"):
            lines.append(f"Groups: {', '.join(dino['groups'])}")

        taming = dino.get("taming")
        if taming:
            taming_parts = []
            if taming.get("kibble"):
                taming_parts.append(f"Kibble: {taming['kibble']}")
            if taming.get("favoriteFood"):
                taming_parts.append(f"Favorite Food: {taming['favoriteFood']}")
            if taming_parts:
                lines.append(f"Taming: {', '.join(taming_parts)}")

        breeding = dino.get("breeding")
        if breeding:
            if breeding.get("eggTempMin") and breeding.get("eggTempMax"):
                lines.append(
                    f"Egg Temperature: {breeding['eggTempMin']}°C - {breeding['eggTempMax']}°C"
                )

        stats = dino.get("baseStats")
        if stats:
            stat_parts = []
            for stat_name in ["health", "stamina", "weight", "melee"]:
                val = stats.get(stat_name)
                if val:
                    stat_parts.append(f"{stat_name.title()}: {val}")
            if stat_parts:
                lines.append(f"Base Stats: {', '.join(stat_parts)}")

        return "\n".join(lines)

    def format_item_info(self, item: dict) -> str:
        """Format an item entry as a human-readable string for the LLM."""
        lines = [f"**{item['name']}**"]

        if item.get("description"):
            lines.append(item["description"][:200])

        lines.append(f"Blueprint: {item['blueprint']}")

        if item.get("type"):
            lines.append(f"Type: {item['type']}")
        if item.get("weight"):
            lines.append(f"Weight: {item['weight']}")
        if item.get("stackSize"):
            lines.append(f"Stack Size: {item['stackSize']}")

        crafting = item.get("crafting")
        if crafting:
            lines.append(f"Required Level: {crafting.get('levelReq', '?')}")
            recipe = crafting.get("recipe", [])
            if recipe:
                recipe_str = ", ".join(f"{r['qty']}x {r['item']}" for r in recipe)
                lines.append(f"Recipe: {recipe_str}")

        return "\n".join(lines)
