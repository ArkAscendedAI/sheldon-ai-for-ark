"""Engram class-key -> clean display name (vanilla catalog dumped from the DevKit 2026-06-20,
836 engrams). The cooked engram getter emits each engram's class key via GetDisplayName
(e.g. "Default__EngramEntry_Campfire_C" or "EngramEntry_Campfire_C"); we map it to the clean
name ("Campfire") here, so the mod stays a dumb key-emitter and names stay tunable with NO cook.
Mod engrams absent from the catalog fall back to a heuristic de-camelCase of the key, and the
catalog can be regenerated (with mods loaded) without a cook."""
import json
import os
import re
import functools

_PATH = os.path.join(os.path.dirname(__file__), "data", "engram_catalog.json")


@functools.lru_cache(maxsize=1)
def _by_key() -> dict:
    try:
        data = json.load(open(_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {e["key"]: (e.get("name") or "") for e in data if e.get("key")}


def _candidates(raw: str):
    """Yield catalog-key candidates from whatever the runtime getter emitted — tolerant of a
    'Default__' CDO prefix and a '_C_<n>' instance suffix, since the exact runtime form of
    GetDisplayName isn't known until the first in-game reply."""
    raw = (raw or "").strip()
    if not raw:
        return
    yield raw
    if raw.startswith("Default__"):
        yield raw[len("Default__"):]
    stripped = re.sub(r"(_C)_\d+$", r"\1", raw)          # EngramEntry_X_C_12 -> _C
    if stripped != raw:
        yield stripped
        if stripped.startswith("Default__"):
            yield stripped[len("Default__"):]


def _heuristic(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"^Default__", "", s)
    s = re.sub(r"^EngramEntry_", "", s)
    s = re.sub(r"_C(_\d+)?$", "", s)
    s = s.replace("_", " ")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)        # camelCase -> spaced
    return s.strip() or (raw or "").strip()


def clean_engram_name(raw: str) -> str:
    """Catalog-exact for known engrams, heuristic de-camelCase for unknown (modded) ones."""
    cat = _by_key()
    for c in _candidates(raw):
        if cat.get(c):
            return cat[c]
    return _heuristic(raw)
