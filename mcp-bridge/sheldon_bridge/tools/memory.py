"""Lorebook / durable-memory tools.

These let Sheldon persist and recall durable facts about the *requesting* survivor
— preferences, named tames, ongoing goals, relationships, in-jokes. The context
assembler injects sticky entries every turn and keyword-matched entries per
message, so a remembered fact resurfaces automatically in later conversations
without the model having to call `recall`.

Security: every entry is scoped to the requesting survivor's campaign via the
injected `ctx` (authenticated identity), never to chat-supplied identity — a tool
call can only read or write the *caller's own* memory.
"""

from __future__ import annotations

from typing import Any

from sheldon_bridge.tools.registry import tool


def _campaign_ctx(ctx: dict | None):
    """Pull (campaign_store, eos_id) from the injected per-turn context."""
    if not ctx:
        return None, None
    campaign = ctx.get("campaign")
    eos = ctx.get("eos_id")
    if not eos and ctx.get("player") is not None:
        eos = getattr(ctx["player"], "player_id", None)
    return campaign, eos


@tool(tier="player",
      description="Remember a durable fact about the survivor you're talking to (it resurfaces in future chats)")
async def remember(fact: str, topic: str = "", permanent: bool = False, ctx: dict | None = None) -> dict[str, Any]:
    """Save a durable fact about the CURRENT survivor to your long-term memory.

    Call this when they tell you something worth carrying across sessions — a
    preference, a named tame, an ongoing goal, a relationship, an in-joke, a base
    location. Do NOT use it for transient state like current health or position;
    you read those live from your sensors.

    Args:
        fact: The thing to remember, written as a standalone statement.
        topic: A short label/title for it (helps recall and keyword matching).
        permanent: True = sticky (always in your context); False = recalled by relevance.
        ctx: Injected context (campaign store + requesting survivor).
    """
    campaign, eos = _campaign_ctx(ctx)
    fact = (fact or "").strip()
    if campaign is None or not eos:
        return {"success": False, "error": "No memory store is available in this context."}
    if not fact:
        return {"success": False, "error": "Nothing to remember (the fact was empty)."}
    topic = (topic or "").strip()
    entry_id = await campaign.add_lore(
        eos, fact, title=topic,
        tier="sticky" if permanent else "cold",
        source="chat", salience=1.0 if permanent else 0.5,
    )
    return {"success": True, "id": entry_id, "permanent": bool(permanent),
            "message": "Noted" + (f" ({topic})." if topic else ".")}


@tool(tier="player",
      description="Recall what you remember about the survivor you're talking to (optionally filtered by a query)")
async def recall(query: str = "", ctx: dict | None = None) -> dict[str, Any]:
    """List durable facts you've remembered about the CURRENT survivor.

    With a query, returns the entries most relevant to it; without one, returns all
    of them. The most relevant memories are already injected into your context each
    turn — use this to review/confirm, or to answer "what do you remember about me?".

    Args:
        query: Optional topic/keywords to filter by.
        ctx: Injected context.
    """
    campaign, eos = _campaign_ctx(ctx)
    if campaign is None or not eos:
        return {"success": False, "error": "No memory store is available in this context."}
    query = (query or "").strip()
    rows = (await campaign.search_lore(eos, query, exclude_sticky=False)
            if query else await campaign.list_lore(eos))
    memories = [{"id": r["id"], "topic": r["title"] or None, "fact": r["body"],
                 "permanent": r["tier"] == "sticky"} for r in rows]
    return {"success": True, "memories": memories, "count": len(memories)}


@tool(tier="player",
      description="Forget a specific remembered fact about the current survivor (by its id from recall)")
async def forget(entry_id: int, ctx: dict | None = None) -> dict[str, Any]:
    """Delete one durable memory about the CURRENT survivor, by the id returned from
    recall. Use this when they ask you to forget something, or to correct a wrong fact
    (forget the old one, then remember the right one).

    Args:
        entry_id: The memory id (from recall).
        ctx: Injected context.
    """
    campaign, eos = _campaign_ctx(ctx)
    if campaign is None or not eos:
        return {"success": False, "error": "No memory store is available in this context."}
    try:
        entry_id = int(entry_id)
    except (TypeError, ValueError):
        return {"success": False, "error": "entry_id must be a number (from recall)."}
    ok = await campaign.remove_lore(eos, entry_id)
    return {"success": ok, "message": "Forgotten." if ok else "No memory with that id to forget."}
