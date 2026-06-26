#!/usr/bin/env python3
"""
Build the Sheldon AI knowledge base from raw Obelisk + Wiki data.

Reads:  data/raw/*.json  (downloaded from Obelisk/wiki)
Writes: data/vanilla/*.json (clean, searchable format)

Usage:
    python data/scripts/build_data.py
"""

import json
import sys
from pathlib import Path

RAW_DIR = Path(__file__).parent.parent / "raw"
OUT_DIR = Path(__file__).parent.parent / "vanilla"


# ---------------------------------------------------------------------------
# Nicknames / aliases  (hand-curated, the most important part for UX)
# ---------------------------------------------------------------------------
DINO_ALIASES: dict[str, list[str]] = {
    "Acrocanthosaurus": ["acro"],
    "Allosaurus": ["allo"],
    "Amargasaurus": ["amarga"],
    "Andrewsarchus": ["andrew"],
    "Ankylosaurus": ["anky", "ankylo"],
    "Argentavis": ["argy", "argent", "arg"],
    "Baryonyx": ["bary"],
    "Basilosaurus": ["basilo", "basil"],
    "Beelzebufo": ["frog", "toad"],
    "Brontosaurus": ["bronto"],
    "Carbonemys": ["turtle", "turt", "carbon"],
    "Carcharodontosaurus": ["carchar", "carch"],
    "Carnotaurus": ["carno"],
    "Castoroides": ["beaver", "giant beaver"],
    "Chalicotherium": ["chalico"],
    "Compy": ["compsognathus"],
    "Daeodon": ["pig", "heal pig", "healing pig", "battle pig"],
    "Deinonychus": ["deino"],
    "Dilophosaur": ["dilo", "dilophosaurus"],
    "Dimetrodon": ["dime"],
    "Dimorphodon": ["dimorph"],
    "Diplodocus": ["diplo"],
    "Doedicurus": ["doed", "doedic"],
    "Dung Beetle": ["beetle", "scarab"],
    "Equus": ["horse", "unicorn"],
    "Gallimimus": ["galli"],
    "Giganotosaurus": ["giga", "gigano"],
    "Hesperornis": ["hesper"],
    "Ichthyosaurus": ["ichthy", "dolphin"],
    "Iguanodon": ["iggy", "iguan"],
    "Kaprosuchus": ["kapro"],
    "Lystrosaurus": ["lystro"],
    "Maewing": ["mae"],
    "Managarmr": ["mana"],
    "Megalodon": ["megalo", "shark"],
    "Megalosaurus": ["megalosaur"],
    "Megatherium": ["mega sloth", "megath"],
    "Mesopithecus": ["monkey", "meso"],
    "Mosasaurus": ["mosa"],
    "Moschops": ["mosc"],
    "Otter": ["otter"],
    "Oviraptor": ["ovi"],
    "Pachyrhinosaurus": ["pachy", "pachyrhino"],
    "Parasaurolophus": ["para", "parasaur"],
    "Pelagornis": ["pela"],
    "Phiomia": ["phio"],
    "Plesiosaur": ["plesi", "plesio"],
    "Procoptodon": ["kangaroo", "roo", "procop"],
    "Pteranodon": ["ptera", "pt"],
    "Pulmonoscorpius": ["scorpion", "pulmon"],
    "Quetzalcoatlus": ["quetz", "quetzal"],
    "Raptor": ["raptor"],
    "Rex": ["rex", "rexy", "t-rex", "trex", "tyrannosaurus"],
    "Sarco": ["sarco", "sarcosuchus"],
    "Snow Owl": ["snow owl", "owl"],
    "Spinosaurus": ["spino"],
    "Stegosaurus": ["stego"],
    "Tapejara": ["tape", "tap"],
    "Tek Rex": ["tek rex"],
    "Terror Bird": ["terror bird", "tb"],
    "Therizinosaurus": ["therizino", "theri", "tickle chicken", "murder chicken",
                         "fear turkey", "tickle monster"],
    "Thylacoleo": ["thyla", "marsupial lion"],
    "Triceratops": ["trike"],
    "Tropeognathus": ["trope", "tropeo"],
    "Tusoteuthis": ["tuso", "squid", "giant squid"],
    "Velonasaur": ["velo"],
    "Woolly Rhino": ["rhino", "woolly rhino"],
    "Wyvern": ["wyvern"],
    "Yutyrannus": ["yuty", "yuti", "furry rex", "fluffy rex", "feather rex"],
}


def build_dinos():
    """Build the dino knowledge base from ASA values + wiki creature info."""
    print("Building dino database...")

    # Load ASA values (stats, taming, breeding)
    with open(RAW_DIR / "asa-values.json") as f:
        asa = json.load(f)

    # Load wiki creature info (names, diet, temperament, groups)
    with open(RAW_DIR / "wiki-creatures-info.json") as f:
        wiki_creatures = json.load(f)

    # Build a lookup from blueprint path to wiki data
    wiki_by_bp: dict[str, dict] = {}
    for key, entry in wiki_creatures.items():
        if not isinstance(entry, dict) or "info" not in entry:
            continue
        info = entry["info"]
        bp = info.get("blueprint")
        if bp:
            # Normalize: strip trailing _C if present
            bp_clean = bp.rstrip("_C").rstrip(".")
            wiki_by_bp[bp_clean] = {
                "key": key,
                "commonName": info.get("commonName"),
                "diet": info.get("diet"),
                "temperament": info.get("temperament"),
                "groups": info.get("groups", []),
                "taming": info.get("taming", {}),
                "dossier": info.get("dossier", {}),
            }

    # Build alias index (lowercase alias -> canonical name)
    alias_index: dict[str, str] = {}
    for canonical, aliases in DINO_ALIASES.items():
        alias_index[canonical.lower()] = canonical
        for a in aliases:
            alias_index[a.lower()] = canonical

    dinos = []
    seen_bps: set[str] = set()

    # ----- Pass 1: ASA values (species with stats/taming/breeding) -----
    for species in asa["species"]:
        bp = species.get("blueprintPath", "")
        name = species.get("name", "")

        # Derive name from blueprint if missing
        if not name:
            bp_part = bp.split("/")[-1].split(".")[0] if bp else ""
            bp_part = bp_part.replace("_Character_BP", "").replace("_character_BP", "")
            bp_part = bp_part.replace("_C", "")
            name = bp_part.replace("_", " ").strip()
            if not name:
                continue

        has_stats = bool(species.get("fullStatsRaw"))
        has_taming = bool(species.get("taming"))
        has_breeding = bool(species.get("breeding"))
        if not has_stats and not has_taming and not has_breeding:
            continue

        if bp in seen_bps:
            continue
        seen_bps.add(bp)

        species["_resolved_name"] = name
        dinos.append(("asa", species))

    # ----- Pass 2: Wiki creatures (fill in dinos missing from ASA values) -----
    # These are the core vanilla dinos that ASA values has as stubs
    for key, entry in wiki_creatures.items():
        if not isinstance(entry, dict) or "info" not in entry:
            continue
        if key.startswith("_"):
            continue

        info = entry["info"]
        common_name = info.get("commonName")
        bp = info.get("blueprint", "")

        if not common_name or not bp:
            continue

        # Normalize bp to match ASA values format
        bp_normalized = bp.rstrip("_C").rstrip(".")
        if bp_normalized in seen_bps or bp in seen_bps:
            continue

        # Only include if it looks like a tameable creature (not a boss variant)
        groups = info.get("groups", [])
        if any(g in groups for g in ["Bosses", "Event Creatures"]):
            continue

        seen_bps.add(bp)
        seen_bps.add(bp_normalized)

        # Build a synthetic species entry from wiki data
        wiki_species = {
            "blueprintPath": bp,
            "_resolved_name": common_name,
            "_from_wiki": True,
        }
        dinos.append(("wiki", wiki_species))

    # ----- Now build the final dino objects -----
    final_dinos = []
    for source, species in dinos:
        name = species["_resolved_name"]
        bp = species.get("blueprintPath", "")

        bp_clean = bp.split(".")[0] if "." in bp else bp
        wiki = wiki_by_bp.get(bp_clean, {})
        # Also try with _C suffix stripped
        if not wiki:
            bp_alt = bp.rstrip("_C").rstrip(".")
            bp_alt_clean = bp_alt.split(".")[0] if "." in bp_alt else bp_alt
            wiki = wiki_by_bp.get(bp_alt_clean, {})

        # Match to wiki data
        bp_clean = bp.split(".")[0] if "." in bp else bp
        wiki = wiki_by_bp.get(bp_clean, {})

        # Determine nicknames
        nicknames = []
        for canonical, aliases in DINO_ALIASES.items():
            if canonical.lower() == name.lower():
                nicknames = aliases
                break

        # Build taming info
        taming_raw = species.get("taming", {})
        taming = {}
        if taming_raw and isinstance(taming_raw, dict):
            taming = {
                "violent": taming_raw.get("violent", False),
                "nonViolent": taming_raw.get("nonViolent", False),
                "affinityNeeded0": taming_raw.get("affinityNeeded0"),
                "foodConsumptionBase": taming_raw.get("foodConsumptionBase"),
            }
        wiki_taming = wiki.get("taming", {})
        if wiki_taming:
            taming["kibble"] = wiki_taming.get("kibble")
            taming["favoriteFood"] = wiki_taming.get("favoriteFood")

        # Build breeding info
        breeding_raw = species.get("breeding", {})
        breeding = {}
        if breeding_raw and isinstance(breeding_raw, dict):
            breeding = {
                "gestationTime": breeding_raw.get("gestationTime"),
                "incubationTime": breeding_raw.get("incubationTime"),
                "maturationTime": breeding_raw.get("maturationTime"),
                "matingCooldownMin": breeding_raw.get("matingCooldownMin"),
                "matingCooldownMax": breeding_raw.get("matingCooldownMax"),
                "eggTempMin": breeding_raw.get("eggTempMin"),
                "eggTempMax": breeding_raw.get("eggTempMax"),
            }

        # Build stats (base wild values only)
        stats_raw = species.get("fullStatsRaw", [])
        stat_names = [
            "health", "stamina", "torpidity", "oxygen", "food",
            "water", "temperature", "weight", "melee", "speed",
            "fortitude", "crafting",
        ]
        base_stats = {}
        if stats_raw:
            for i, sname in enumerate(stat_names):
                if i < len(stats_raw) and stats_raw[i]:
                    base_stats[sname] = stats_raw[i][0]  # base value

        dino = {
            "name": name,
            "blueprint": bp,
            "nicknames": nicknames,
            "diet": wiki.get("diet"),
            "temperament": wiki.get("temperament"),
            "groups": wiki.get("groups", []),
            "variants": species.get("variants", []),
            "taming": taming if taming else None,
            "breeding": breeding if breeding else None,
            "baseStats": base_stats if base_stats else None,
            "colors": len(species.get("colors", [])) if species.get("colors") else 0,
        }
        final_dinos.append(dino)

    # Sort by name
    final_dinos.sort(key=lambda d: d["name"])

    # Post-process: add cross-reference aliases for wiki name variants
    # so players can use either common or wiki names
    dino_names_in_db = {d["name"] for d in final_dinos}
    wiki_name_aliases = {
        # wiki name -> common aliases to also resolve to it
        "Therizinosaur": ["therizinosaurus"],
        "Spino": ["spinosaurus"],
        "Quetzal": ["quetzalcoatlus"],
    }
    for wiki_name, extra_aliases in wiki_name_aliases.items():
        if wiki_name in dino_names_in_db:
            for alias in extra_aliases:
                alias_index[alias.lower()] = wiki_name

    # Also ensure DINO_ALIASES canonical names point to actual DB entries
    # e.g., "Therizinosaurus" in aliases should resolve to "Therizinosaur"
    canonical_redirects = {
        "Therizinosaurus": "Therizinosaur",
        "Spinosaurus": "Spino",
        "Quetzalcoatlus": "Quetzal",
        "Brontosaurus": "Brontosaurus",  # keep as-is, fuzzy search will catch it
    }
    for old_canonical, new_canonical in canonical_redirects.items():
        if new_canonical in dino_names_in_db and old_canonical not in dino_names_in_db:
            # Update all aliases that point to old_canonical
            for alias_key, alias_val in list(alias_index.items()):
                if alias_val == old_canonical:
                    alias_index[alias_key] = new_canonical

    output = {
        "version": "0.1.0",
        "source": "arkutils/Obelisk + ark.wiki.gg",
        "game": "ASA",
        "count": len(final_dinos),
        "dinos": final_dinos,
        "aliases": alias_index,
    }

    out_path = OUT_DIR / "dinos.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Wrote {len(final_dinos)} dinos to {out_path}")
    return final_dinos


def build_items():
    """Build the item knowledge base from wiki items data."""
    print("Building item database...")

    with open(RAW_DIR / "wiki-items.json") as f:
        raw = json.load(f)

    raw_items = raw.get("items", [])
    items = []

    for ri in raw_items:
        name = ri.get("name", "Unknown")
        bp = ri.get("bp", ri.get("blueprintPath", ""))

        # Parse crafting info
        crafting = None
        craft_raw = ri.get("crafting")
        if craft_raw and isinstance(craft_raw, dict):
            recipe = []
            for ingredient in craft_raw.get("recipe", []):
                recipe.append({
                    "item": ingredient.get("type", "").split(".")[-1].replace(
                        "PrimalItemResource_", "").replace("PrimalItem_", ""),
                    "qty": ingredient.get("qty", 1),
                })
            crafting = {
                "levelReq": craft_raw.get("levelReq", 0),
                "xp": craft_raw.get("xp", 0),
                "time": craft_raw.get("time", 0),
                "recipe": recipe,
            }

        item = {
            "name": name,
            "blueprint": bp,
            "description": ri.get("description", ""),
            "type": ri.get("type", ""),
            "weight": ri.get("weight"),
            "stackSize": ri.get("stackSize"),
            "crafting": crafting,
        }
        items.append(item)

    items.sort(key=lambda i: i["name"])

    output = {
        "version": "0.1.0",
        "source": "arkutils/Obelisk",
        "game": "ASA",
        "count": len(items),
        "items": items,
    }

    out_path = OUT_DIR / "items.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Wrote {len(items)} items to {out_path}")
    return items


def build_spawn_maps():
    """Build spawn location data from wiki spawn map JSON."""
    print("Building spawn map data...")

    maps_built = 0
    for spawn_file in sorted(RAW_DIR.glob("wiki-spawns-*.json")):
        map_name = spawn_file.stem.replace("wiki-spawns-", "")
        print(f"  Processing {map_name}...")

        with open(spawn_file) as f:
            raw = json.load(f)

        if not isinstance(raw, list):
            print(f"    Skipping {map_name}: unexpected format")
            continue

        # Parse spawn entries into a dino -> locations mapping
        dino_spawns: dict[str, list[dict]] = {}

        for entry in raw:
            entry_name = entry.get("n", "")
            for group in entry.get("e", []):
                for creature in group.get("s", []):
                    creature_name = creature.get("n", "")
                    if not creature_name:
                        continue

                    # Collect all spawn zones for this creature in this entry
                    for zone in entry.get("s", []):
                        for bbox in zone.get("l", []):
                            if len(bbox) >= 4:
                                if creature_name not in dino_spawns:
                                    dino_spawns[creature_name] = []
                                dino_spawns[creature_name].append({
                                    "lat": round((bbox[0] + bbox[2]) / 2, 1),
                                    "lon": round((bbox[1] + bbox[3]) / 2, 1),
                                    "group": entry_name,
                                })

        # Deduplicate and simplify
        for dino_name in dino_spawns:
            seen = set()
            unique = []
            for loc in dino_spawns[dino_name]:
                key = (loc["lat"], loc["lon"])
                if key not in seen:
                    seen.add(key)
                    unique.append(loc)
            dino_spawns[dino_name] = unique

        output = {
            "version": "0.1.0",
            "source": "ark.wiki.gg",
            "map": map_name,
            "speciesCount": len(dino_spawns),
            "spawns": dino_spawns,
        }

        out_path = OUT_DIR / f"spawns-{map_name}.json"
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2)
        print(f"    {len(dino_spawns)} species with spawn data")
        maps_built += 1

    print(f"  Built {maps_built} spawn maps")


def build_engrams():
    """Build engram data from wiki engrams."""
    print("Building engram database...")

    with open(RAW_DIR / "wiki-engrams.json") as f:
        raw = json.load(f)

    engram_list = raw.get("engrams", raw)
    if isinstance(engram_list, dict):
        engram_list = list(engram_list.values())

    engrams = []
    for e in engram_list:
        if not isinstance(e, dict):
            continue
        engram = {
            "name": e.get("name", "Unknown"),
            "blueprint": e.get("blueprintPath", e.get("bp", "")),
            "level": e.get("requiredLevel", e.get("level", 0)),
            "points": e.get("requiredPoints", e.get("points", 0)),
            "group": e.get("group", ""),
        }
        engrams.append(engram)

    engrams.sort(key=lambda e: (e.get("level", 0), e["name"]))

    output = {
        "version": "0.1.0",
        "source": "arkutils/Obelisk",
        "game": "ASA",
        "count": len(engrams),
        "engrams": engrams,
    }

    out_path = OUT_DIR / "engrams.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Wrote {len(engrams)} engrams to {out_path}")


def print_summary():
    """Print a summary of all generated files."""
    print("\n=== Knowledge Base Summary ===")
    for f in sorted(OUT_DIR.glob("*.json")):
        size_kb = f.stat().st_size / 1024
        with open(f) as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            count = data.get("count", data.get("speciesCount", "?"))
        elif isinstance(data, list):
            count = len(data)
        else:
            count = "?"
        print(f"  {f.name:30s}  {size_kb:7.1f} KB  ({count} entries)")


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    build_dinos()
    build_items()
    build_spawn_maps()
    build_engrams()
    print_summary()
