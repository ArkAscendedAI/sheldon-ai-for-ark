"""Context assembler — builds the per-message context fed to the LLM.

Context layers:
  1. base system (persona + response-format, passed in from config)
  2. requester line (survivor name + admin status -> the LLM self-limits)
  3. diegetic LOCAL telemetry snapshot (own vitals/position + server state) — always
  4. lorebook: sticky entries (always) + keyword hits for the current message
  5. recent conversation, most-recent-first within the remaining token budget

Beyond-perception data (global dino census, remote tribes) is NOT injected here —
that's an on-demand "sensors" tool. Semantic embedding retrieval augments step 4
in a later phase; the keyword search is the Phase-0 stand-in.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sheldon_bridge import gps
from sheldon_bridge.db.campaign_store import CampaignStore
from sheldon_bridge.db.telemetry_store import TelemetryStore

_VITALS = ("health", "food", "water", "stamina", "oxygen", "weight", "torpor")  # oxygen persisted since telemetry migration 003
_CHAT_RESERVE = 512  # headroom for the incoming user msg + response framing
_STALE_AFTER_S = 90  # player_state older than this gets an explicit "call get_vitals" nudge (the stale-150 bug)
_DEATH_PUSH_MAX_AGE_S = 1800  # only push deaths from the last ~30 min into the prompt (older = use the tool)
_DEATH_PUSH_MAX = 3          # cap how many recent deaths to surface in the block
# server_state keys that are INTERNAL bookkeeping, never shown to the LLM: the census loop's
# bounds/cursors and the GPS conversion constants. Without this filter the assembler dumped
# raw `census_bounds=[...], census_lastfull=<epoch>, ...` into Sheldon's context every turn.
_INTERNAL_STATE_PREFIXES = ("census_", "gps_")


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _fmt_age(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _fmt_stat(v) -> str:
    """Show 0..1 vitals as a percentage; pass other numbers through."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if 0.0 <= f <= 1.0:
        return f"{f * 100:.0f}%"
    return f"{f:.0f}" if f == int(f) else f"{f:.1f}"


@dataclass
class AssembledContext:
    system_prompt: str
    messages: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


class ContextAssembler:
    def __init__(self, token_budget: int = 14000):
        self.budget = token_budget

    async def assemble(
        self,
        *,
        base_system: str,
        campaign: CampaignStore,
        telemetry: TelemetryStore | None,
        eos_id: str,
        character_name: str,
        is_admin: bool,
        user_message: str,
        recent_events: list | None = None,
    ) -> AssembledContext:
        parts: list[str] = [base_system.strip()] if base_system else []
        parts.append(self._requester_block(character_name, is_admin))

        tele_block, tele_meta = await self._telemetry_block(telemetry, eos_id)
        if tele_block:
            parts.append(tele_block)

        deaths_block = self._recent_deaths_block(recent_events)
        if deaths_block:
            parts.append(deaths_block)

        lore_block, lore_count = await self._lorebook_block(campaign, eos_id, user_message)
        if lore_block:
            parts.append(lore_block)

        system_prompt = "\n\n".join(p for p in parts if p)
        sys_tokens = _est_tokens(system_prompt)

        chat_budget = max(1000, self.budget - sys_tokens - _CHAT_RESERVE)
        rows = await campaign.recent_within_budget(eos_id, chat_budget)
        messages = [{"role": r["role"], "content": r["content"]} for r in rows]

        return AssembledContext(
            system_prompt=system_prompt,
            messages=messages,
            meta={
                "sys_tokens": sys_tokens,
                "chat_msgs": len(messages),
                "chat_budget": chat_budget,
                "lore_entries": lore_count,
                **tele_meta,
            },
        )

    # --- blocks --------------------------------------------------------------

    def _requester_block(self, character_name: str, is_admin: bool) -> str:
        # ADD-01: the survivor name reaches the system prompt verbatim — neutralize newlines + markdown
        # heading markers so a crafted name can't inject a fake "## ..." section or role framing.
        who = (character_name or "").replace("\n", " ").replace("\r", " ").replace("#", "").strip()[:64] or "the survivor"
        admin = (
            "They ARE a server admin — you may run admin/console actions on their behalf."
            if is_admin
            else "They are NOT an admin — do not attempt admin/console actions for them."
        )
        return f"## Current survivor\n\nYou are speaking with {who}. {admin}"

    async def _telemetry_block(self, telemetry: TelemetryStore | None, eos_id: str) -> tuple[str, dict]:
        if telemetry is None:
            return "", {"telemetry": "none"}
        ps = await telemetry.get_player_state(eos_id)
        server = await telemetry.get_server_state()
        if not ps and not server:
            return "", {"telemetry": "empty"}

        lines: list[str] = []
        age_s: float | None = None
        if ps:
            if ps["captured_at"]:
                age_s = time.time() - ps["captured_at"]
            vit = [f"{k} {_fmt_stat(ps[k])}" for k in _VITALS if ps[k] is not None]
            if vit:
                lines.append("Vitals: " + ", ".join(vit))
            if ps["region"]:
                lines.append(f"Location: {ps['region']}")
            if ps["x"] is not None and ps["y"] is not None:
                mapgps = gps.from_server_state(server)  # None until the mod publishes this map's constants
                lines.append("Coords: " + gps.describe_location(ps["x"], ps["y"], ps["z"], mapgps))
            if ps["level"] is not None:
                lines.append(f"Level: {ps['level']}")
        if server:
            sv = ", ".join(
                f"{k}={v}" for k, v in server.items()
                if v is not None and not k.startswith(_INTERNAL_STATE_PREFIXES)
            )
            if sv:
                lines.append("Server: " + sv)
        if not lines:
            return "", {"telemetry": "empty"}

        # Honest framing: this is a CACHED snapshot (player_state only refreshes when a getter
        # like get_vitals runs), NOT a live feed. Calling it "live" caused the stale-150 bug —
        # Sheldon confidently quoting an hours-old full-health read. Show the age, and when the
        # read is stale, tell the LLM to take a fresh reading instead of quoting these numbers.
        age_note = f" ({_fmt_age(age_s)})" if age_s is not None else ""
        block = f"## Implant sensors — last cached read{age_note}\n\n" + "\n".join(lines)
        if age_s is not None and age_s > _STALE_AFTER_S:
            block += (
                "\n\nThese readings are cached from an earlier scan, not a live feed. For the "
                "survivor's CURRENT health/vitals/status, call get_vitals — it takes a fresh "
                "reading (and refreshes this block) and is authoritative. Don't quote the numbers "
                "above as current."
            )
        return block, {"telemetry": "present", "telemetry_age_s": int(age_s) if age_s is not None else None}

    def _recent_deaths_block(self, recent_events: list | None) -> str:
        """PUSH recent deaths into the system prompt so "what killed me?" works without the LLM
        having to call get_recent_events (it wasn't, so death attribution looked dead even though
        the bridge had the frame). `recent_events` is already per-player scoped + identity-aliased
        by the caller; we surface only DEATHS here (damage is high-frequency noise — leave it to
        the tool), newest first, recent ones only, with anti-hallucination framing (the live convo
        showed Sheldon inventing a killer AND falsely claiming the log was empty)."""
        if not recent_events:
            return ""
        now = time.time()
        deaths = [e for e in reversed(recent_events)
                  if e.get("event") == "death_event"
                  and (not e.get("at") or now - e["at"] <= _DEATH_PUSH_MAX_AGE_S)]
        if not deaths:
            return ""
        lines = []
        for e in deaths[:_DEATH_PUSH_MAX]:
            at = e.get("at")
            age = f" ({_fmt_age(now - at)})" if at else ""
            lines.append(f"- {e.get('summary') or 'Died (cause unknown)'}{age}")
        return (
            "## Recent deaths (your implant's event log — authoritative)\n\n"
            "These are the survivor's own recent deaths, newest first. If they ask what killed "
            "them, answer from THIS list — it IS the event log. Never say the log is empty when "
            "entries are listed here, and never name a killer or cause that is not listed.\n"
            + "\n".join(lines)
        )

    async def _lorebook_block(self, campaign: CampaignStore, eos_id: str, user_message: str) -> tuple[str, int]:
        sticky = await campaign.get_sticky_lore(eos_id)
        hits = await campaign.search_lore(eos_id, user_message) if user_message else []

        seen: set[int] = set()
        lines: list[str] = []
        for e in list(sticky) + list(hits):
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            prefix = f"{e['title']}: " if e["title"] else ""
            lines.append(f"- {prefix}{e['body']}")
        if not lines:
            return "", 0
        await campaign.touch_lore(list(seen))
        block = "## What you remember about this survivor\n\n" + "\n".join(lines)
        return block, len(lines)
