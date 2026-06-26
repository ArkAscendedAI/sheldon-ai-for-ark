"""Normalize raw ARK actor/class names to a readable species/name.

The mod captures killer/victim names via Blueprint `GetDisplayName`, which at runtime yields the
actor instance name (e.g. ``Raptor_Character_BP_C_2147482977``) rather than a pretty descriptive
name. The bridge cleans these for death/damage summaries now, and the same helper will normalize
the dino census later. Deliberately TOLERANT: an already-clean name passes through unchanged, the
``Default__`` prefix and ``_Character_BP_C[_<n>]`` decorations are stripped, and the residue is
de-camelCased so modded keys like ``Bob_Raptor_Character_BP_C`` read as "Bob Raptor".
"""
import json
import logging
import os
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

# longest-first so "_Character_BP_C" wins over "_C"
_SUFFIXES = ("_Character_BP_C", "_Character_BP", "_CharacterBP_C", "_CharacterBP", "_C")
_NULLISH = {"", "none", "null", "nullptr"}


@lru_cache(maxsize=4096)
def clean_actor_name(raw: str) -> str:
    """Raw actor/class name -> readable species. Returns "" for null-ish input (e.g. a null
    DamageCauser from an environmental death yields GetDisplayName -> "None")."""
    if not raw:
        return ""
    s = raw.strip()
    if s.lower() in _NULLISH:
        return ""
    if s.startswith("Default__"):
        s = s[len("Default__"):]
    s = re.sub(r"_\d+$", "", s)          # trailing instance index (…_C_2147482977 -> …_C)
    for suf in _SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    s = s.strip("_")
    if not s:
        return raw.strip()               # was all decoration — give back the original
    s = s.replace("_", " ")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)   # de-camelCase
    return re.sub(r"\s+", " ", s).strip()


def parse_actor_ref(raw: str) -> dict:
    """Split a Blueprint `GetObjectName` result into the three identities the census/death
    events need. At runtime GetObjectName yields the actor INSTANCE name, e.g.
    ``Raptor_Character_BP_C_2147482977`` -> ::

        {"name": "Raptor",                       # clean species (display)
         "class_key": "Raptor_Character_BP_C",   # species join key (census dino_species.class_key)
         "uid": "2147482977"}                    # session-unique instance id (the individual-marry key)

    The trailing ``_<digits>`` is the per-spawn instance index — unique per live actor within a
    server session (resets on restart, which is fine for a live re-scanned census). Returns {} for
    null-ish input (e.g. an environmental death -> GetObjectName "None")."""
    if not raw:
        return {}
    s = raw.strip()
    if s.lower() in _NULLISH:
        return {}
    m = re.search(r"_(\d+)$", s)
    uid = m.group(1) if m else None
    class_key = re.sub(r"_\d+$", "", s) if m else s
    # BUFFS-10: never drop the attacker on an unexpected name format — fall back to the cleaned
    # class_key (then the raw token) so a modded/structure killer still yields a usable name.
    name = clean_actor_name(s) or clean_actor_name(class_key) or class_key or s
    return {"name": name, "class_key": class_key, "uid": uid}


# ---------------------------------------------------------------------------
# Player-identity aliasing (legacy id <-> canonical id)
# ---------------------------------------------------------------------------
# Treat two ids as the SAME survivor. This exists for deployments that switched
# the mod's identity scheme mid-campaign (e.g. a legacy numeric LinkedPlayerID
# to the player's EOS id): the same survivor can briefly emit one id from one
# call site and the other from another. Aliasing keeps a single survivor's
# history and per-player event filtering coherent across that switch.
# Canonicalizing legacy -> canonical is forward compatible: once every call site
# emits the canonical id, ``canonical_id(canonical) == canonical`` so the alias
# becomes a harmless identity and this code can be retired.
# Configure per-deployment with the SHELDON_IDENTITY_ALIASES env var (JSON
# object ``{"<alias>": "<canonical>"}``). The built-in default is empty — no
# deployment-specific ids live in source.
_DEFAULT_IDENTITY_ALIASES = {}


def _load_identity_aliases() -> dict:
    raw = (os.environ.get("SHELDON_IDENTITY_ALIASES") or "").strip()
    if not raw:
        return dict(_DEFAULT_IDENTITY_ALIASES)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed:
            return {str(k).strip(): str(v).strip() for k, v in parsed.items()}
    except Exception:
        logger.warning("SHELDON_IDENTITY_ALIASES is not valid JSON; using built-in default")
    return dict(_DEFAULT_IDENTITY_ALIASES)


IDENTITY_ALIASES = _load_identity_aliases()


def canonical_id(player_id):
    """Map an alias player id to its canonical id (the same survivor across an
    identity-scheme change). Returns the id unchanged if it has no alias. Safe on None/empty/non-str.
    Idempotent: ``canonical_id(canonical_id(x)) == canonical_id(x)`` (canonical targets
    are not themselves alias keys)."""
    if not player_id:
        return player_id
    return IDENTITY_ALIASES.get(str(player_id).strip(), player_id)
