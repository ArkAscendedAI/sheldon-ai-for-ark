"""
Pre-populate the semantic cache with common ARK questions and answers.

These are high-frequency questions that nearly every player asks. Pre-caching
them means instant responses with zero LLM cost for the most common queries.

Categories:
  - Taming basics
  - Resource gathering
  - Crafting essentials
  - Survival mechanics
  - Common dino questions
"""

from __future__ import annotations

import logging

from sheldon_bridge.cache import SemanticCache

logger = logging.getLogger(__name__)

# TTL categories
STABLE_TTL = 604800.0    # 7 days — core game mechanics that rarely change
MEDIUM_TTL = 172800.0    # 2 days — may vary by server settings
SHORT_TTL = 43200.0      # 12 hours — server-specific info

FAQ_ENTRIES: list[dict] = [
    # === Taming Basics ===
    {
        "query": "How do I tame a dinosaur?",
        "response": (
            "There are two main taming methods in ARK:\n\n"
            "**Knockout Taming (most common):**\n"
            "1. Knock the dino unconscious with Tranq Arrows, Tranq Darts, or Shocking Tranq Darts\n"
            "2. Put its preferred food in its inventory\n"
            "3. Keep it unconscious with Narcotics or Narcoberries\n"
            "4. Wait for the taming bar to fill\n\n"
            "**Passive Taming:**\n"
            "1. Approach the dino carefully (some require you to be crouching)\n"
            "2. Put the food in your last hotbar slot\n"
            "3. Press the use key when prompted to feed it\n"
            "4. Wait for it to get hungry again, then repeat\n\n"
            "**Tips:**\n"
            "- Higher level dinos take longer to tame but are stronger\n"
            "- Using the preferred kibble or food makes taming much faster\n"
            "- Building a taming pen helps keep aggressive dinos contained\n"
            "- Use Dododex or ask me for specific taming requirements!"
        ),
        "ttl": STABLE_TTL,
        "category": "taming",
    },
    {
        "query": "What are narcotics used for?",
        "response": (
            "**Narcotics** are used to keep dinosaurs unconscious during knockout taming. "
            "They increase a dino's Torpor, preventing it from waking up before taming completes.\n\n"
            "**Crafting:** 5x Narcoberry + 1x Spoiled Meat → 1 Narcotic (at Mortar and Pestle)\n\n"
            "**Usage:** Place them in the unconscious dino's inventory and remote-use them "
            "(or they'll be consumed automatically when torpor gets low).\n\n"
            "**Alternative:** Bio Toxin (from Cnidaria/jellyfish) is more effective per unit "
            "but harder to obtain."
        ),
        "ttl": STABLE_TTL,
        "category": "taming",
    },
    {
        "query": "What is kibble and how do I make it?",
        "response": (
            "**Kibble** is a crafted food used for taming dinos. It's the fastest and most "
            "efficient taming food for most creatures.\n\n"
            "ARK has 6 kibble tiers (from weakest to strongest):\n"
            "1. **Basic** — Extra Small eggs\n"
            "2. **Simple** — Small eggs\n"
            "3. **Regular** — Medium eggs\n"
            "4. **Superior** — Large eggs\n"
            "5. **Exceptional** — Extra Large eggs\n"
            "6. **Extraordinary** — Special eggs (Yutyrannus, etc.)\n\n"
            "Each kibble recipe uses: Eggs of the right size + Fiber + Mejoberry + "
            "Water + various other ingredients depending on the tier.\n\n"
            "Craft kibble in a **Cooking Pot** or **Industrial Cooker** with water."
        ),
        "ttl": STABLE_TTL,
        "category": "taming",
    },

    # === Resource Gathering ===
    {
        "query": "How do I get metal ingots?",
        "response": (
            "**Metal Ingots** are crafted by smelting Metal Ore in a Refining Forge or "
            "Industrial Forge.\n\n"
            "**Steps:**\n"
            "1. Find metal-rich rocks (shiny, gold-colored rocks, usually on mountains)\n"
            "2. Mine them with a pickaxe or an Ankylosaurus (best metal gatherer)\n"
            "3. Put the Metal Ore into a Refining Forge with fuel (wood, thatch, or sparkpowder)\n"
            "4. 2 Metal Ore smelts into 1 Metal Ingot\n\n"
            "**Tips:**\n"
            "- Mountains and volcano areas have the most metal\n"
            "- An Ankylosaurus gathers far more metal than any tool\n"
            "- The Industrial Forge smelts 40x faster than the Refining Forge\n"
            "- An Argentavis can carry an Ankylosaurus for mobile metal farming"
        ),
        "ttl": STABLE_TTL,
        "category": "resources",
    },
    {
        "query": "What is cementing paste and how do I get it?",
        "response": (
            "**Cementing Paste** is a key crafting ingredient used in many advanced recipes.\n\n"
            "**Ways to get it:**\n"
            "1. **Craft it:** 4x Chitin/Keratin + 8x Stone → 1 Cementing Paste (Mortar and Pestle)\n"
            "2. **Beaver dams:** Raid Castoroides (beaver) dams in rivers — they contain free paste!\n"
            "3. **Beelzebufo (frog):** Kill insects with a tamed frog — it auto-converts them to paste\n"
            "4. **Achatina (snail):** Tamed snails passively produce Achatina Paste (substitute)\n\n"
            "**Tip:** Beaver dams are the easiest source early game. "
            "Look for them along rivers and lakes."
        ),
        "ttl": STABLE_TTL,
        "category": "resources",
    },

    # === Crafting Essentials ===
    {
        "query": "How do I make a fabricator?",
        "response": (
            "A **Fabricator** is an advanced crafting station needed for high-tier items.\n\n"
            "**Recipe:** 50x Metal Ingot + 35x Cementing Paste + 20x Crystal + "
            "15x Oil + 50x Polymer/Organic Polymer\n\n"
            "**Requires:** Level 48 + 24 Engram Points to unlock\n\n"
            "**Fuel:** Gasoline (crafted from 6x Oil + 5x Hide in a Refining Forge)\n\n"
            "**Used for:** Assault rifles, riot gear, electronics, industrial items, "
            "and many other advanced craftables."
        ),
        "ttl": STABLE_TTL,
        "category": "crafting",
    },

    # === Survival Mechanics ===
    {
        "query": "How do I level up fast?",
        "response": (
            "**Fastest ways to earn XP in ARK:**\n\n"
            "1. **Craft Narcotics** — very cheap to make, good XP per craft\n"
            "2. **Kill alpha dinos** — huge XP rewards\n"
            "3. **Explorer Notes** — double XP for a few minutes when collected\n"
            "4. **Broth of Enlightenment** — crafted XP boost consumable\n"
            "5. **Craft rafts** — decent XP if you have the materials\n"
            "6. **Tame dinos** — you get XP for completing tames\n"
            "7. **Build structures** — each piece gives some XP\n"
            "8. **Tribe XP sharing** — being in a tribe with active members helps\n\n"
            "**Early game tip:** Mass-craft Narcotics. They're cheap (Narcoberries + Spoiled Meat) "
            "and give solid XP."
        ),
        "ttl": STABLE_TTL,
        "category": "survival",
    },
    {
        "query": "What should I tame first?",
        "response": (
            "**Recommended early tames (in order):**\n\n"
            "1. **Parasaur** — easy to tame, acts as an alarm for nearby threats\n"
            "2. **Trike (Triceratops)** — great berry gatherer, decent fighter\n"
            "3. **Raptor** — fast travel mount, good early combat dino\n"
            "4. **Pteranodon** — your first flyer, changes the game completely\n"
            "5. **Ankylosaurus** — the best metal gatherer in the game\n"
            "6. **Argentavis** — powerful flyer, can carry Ankylosaurus for metal runs\n\n"
            "**Tips:**\n"
            "- Bolas work on Raptors and Parasaurs (immobilizes them for taming)\n"
            "- Build a small taming pen early — makes everything easier\n"
            "- Taming a Pteranodon opens up the entire map to you"
        ),
        "ttl": STABLE_TTL,
        "category": "survival",
    },

    # === Common Dino Questions ===
    {
        "query": "What is the best combat dino?",
        "response": (
            "The **best combat dinos** depend on the situation:\n\n"
            "**All-around best:** Rex — high HP, high damage, easy to breed\n"
            "**Boss fights:** Rexes (19) + 1 Yutyrannus (courage buff)\n"
            "**PvP raiding:** Giganotosaurus — highest raw damage\n"
            "**Support:** Yutyrannus — courage roar buffs all nearby tames\n"
            "**Tank:** Stegosaurus — great damage reduction in turret mode\n"
            "**Aquatic:** Mosasaurus or Tusoteuthis\n"
            "**Flying combat:** Wyvern (Lightning or Fire)\n"
            "**Cave runs:** Baryonyx — fits in most caves, water-capable\n\n"
            "For most PvE content, a well-bred Rex army with a Yutyrannus "
            "for support is the gold standard."
        ),
        "ttl": STABLE_TTL,
        "category": "dinos",
    },
    {
        "query": "How does breeding work?",
        "response": (
            "**ARK Breeding Steps:**\n\n"
            "1. **Mate:** Put a male and female of the same species near each other with "
            "Mating enabled. Wait for the mating bar to fill.\n"
            "2. **Egg/Gestation:** Either an egg drops (most dinos) or the female gestates "
            "(mammals). Eggs need specific temperatures — use air conditioners or campfires.\n"
            "3. **Hatch/Birth:** Baby is born at low food — immediately put food in its inventory!\n"
            "4. **Maturation:** Baby → Juvenile → Adolescent → Adult. Needs constant food until adult.\n"
            "5. **Imprinting:** Every few hours, the baby requests care (cuddle, walk, specific food). "
            "Imprinting gives up to 30% stat bonus and rider damage bonus.\n\n"
            "**Tips:**\n"
            "- Use a Maewing to auto-nurse babies (game changer!)\n"
            "- Mutations can increase stats — breed for mutations\n"
            "- Breeding can produce dinos far stronger than wild tames"
        ),
        "ttl": STABLE_TTL,
        "category": "dinos",
    },
]


def warm_up_cache(cache: SemanticCache) -> int:
    """Pre-populate the cache with common Q&A pairs. Returns count added."""
    count = 0
    for entry in FAQ_ENTRIES:
        cache.store(
            query=entry["query"],
            response=entry["response"],
            ttl=entry.get("ttl", STABLE_TTL),
            category=entry.get("category", "general"),
        )
        count += 1

    logger.info(f"Cache warmed with {count} FAQ entries")
    return count
