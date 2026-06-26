#!/usr/bin/env python3
"""One-off campaign.db identity re-key: legacy player id -> new canonical id.

WHY: a mod can change its identity scheme mid-campaign — e.g. stamping each chat with the
survivor's NUMERIC GetLinkedPlayerID, then switching to the real 32-hex EOS id
(GetUniqueNetIdAsString) at every identity site. campaign.db is keyed by whatever id was
current when each row was written, so without a re-key the survivor's whole history (survivor
row, lorebook, conversation, curation state) orphans under the old key while new rows start a
blank campaign.

RUN AT THE SWITCH, COORDINATED: the moment the new-id mod goes live, before/while the survivor
first reconnects with the new id — so there's no window where new-id rows and the old history
diverge. DRY-RUN BY DEFAULT; pass --commit to write. This is a PREPARED tool — do NOT run it
before the identity-scheme switch lands.

Typical invocation at switch time (campaign.db lives on the bridge's persistent volume):
    # back up first
    docker exec sheldon-bridge cp /path/to/campaign.db /path/to/campaign.db.bak.pre-rekey
    # copy this script in, dry-run, then commit
    docker cp mcp-bridge/tools/migrate_campaign_eos.py sheldon-bridge:/tmp/
    docker exec sheldon-bridge python /tmp/migrate_campaign_eos.py --db /path/to/campaign.db --old <OLD> --new <NEW>
    docker exec sheldon-bridge python /tmp/migrate_campaign_eos.py --db /path/to/campaign.db --old <OLD> --new <NEW> --commit
Run it while the chat link is quiet (it's a fast single transaction).

Stdlib only (sqlite3) so it runs in the container with no extra deps. campaign.db has no FK
constraints, so the re-key is plain UPDATEs across the survivor-scoped tables.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

# (table, identity-column) for every survivor-scoped table in campaign.db's 001_init schema.
TABLES = [
    ("survivors", "eos_id"),
    ("lorebook_entries", "survivor_eos"),
    ("conversation_messages", "survivor_eos"),
    ("curation_state", "survivor_eos"),
]


def _counts(cur: sqlite3.Cursor, key: str) -> dict[str, int]:
    return {t: cur.execute(f"SELECT COUNT(*) FROM {t} WHERE {c}=?", (key,)).fetchone()[0]
            for t, c in TABLES}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Re-key one survivor in campaign.db (legacy player id -> new canonical id).")
    ap.add_argument("--db", required=True, help="path to campaign.db")
    ap.add_argument("--old", required=True, help="current (legacy) key in campaign.db")
    ap.add_argument("--new", required=True, help="target canonical key (e.g. a 32-hex EOS id)")
    ap.add_argument("--commit", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--absorb-empty-target", action="store_true",
                    help="if the target key already exists but is EMPTY (0 messages + 0 lore), "
                         "delete its rows first so the re-key won't PRIMARY KEY conflict")
    args = ap.parse_args(argv)

    if args.old == args.new:
        ap.error("--old and --new are identical; nothing to do")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", args.new):
        print(f"WARNING: --new {args.new!r} doesn't look like a 32-hex EOS id (continuing).",
              file=sys.stderr)
    if not os.path.exists(args.db):
        sys.exit(f"ABORT: no such campaign.db: {args.db}")

    con = sqlite3.connect(args.db)
    con.isolation_level = None  # we drive BEGIN/COMMIT explicitly
    cur = con.cursor()

    have = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t, _ in TABLES if t not in have]
    if missing:
        con.close()
        sys.exit(f"ABORT: {args.db} is missing expected tables: {missing} "
                 "(is this really a campaign.db?)")

    old_counts = _counts(cur, args.old)
    new_counts = _counts(cur, args.new)

    print(f"campaign.db : {args.db}")
    print(f"re-key      : OLD {args.old!r}  ->  NEW {args.new!r}")
    print(f"  rows OLD  : {old_counts}")
    print(f"  rows NEW  : {new_counts}  (target)")

    if sum(old_counts.values()) == 0:
        print("Nothing to migrate: the OLD key has no rows (already migrated, or wrong --old?).")
        con.close()
        return 0

    target_exists = new_counts["survivors"] > 0 or new_counts["curation_state"] > 0
    target_empty = new_counts["conversation_messages"] == 0 and new_counts["lorebook_entries"] == 0

    if not args.commit:
        print("\nDRY RUN — no changes written.")
        if target_exists and not target_empty:
            print("  WARNING: target key already has history — --commit would ABORT "
                  "(this tool never merges two identities' histories).")
        elif target_exists and target_empty:
            print("  NOTE: target key exists but is empty (fresh connect). Pass "
                  "--absorb-empty-target with --commit to delete it first.")
        print("  Re-run with --commit to apply.")
        con.close()
        return 0

    try:
        cur.execute("BEGIN")
        if target_exists:
            if not (args.absorb_empty_target and target_empty):
                cur.execute("ROLLBACK")
                con.close()
                sys.exit("ABORT: target key already exists in campaign.db — "
                         + ("it has history, refusing to merge identities."
                            if not target_empty else
                            "re-run with --absorb-empty-target to delete the empty target row first."))
            for t, c in TABLES:
                cur.execute(f"DELETE FROM {t} WHERE {c}=?", (args.new,))
            print(f"  absorbed empty target key {args.new!r} (deleted its rows).")
        for t, c in TABLES:
            cur.execute(f"UPDATE {t} SET {c}=? WHERE {c}=?", (args.new, args.old))
        cur.execute("COMMIT")
    except Exception as e:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        con.close()
        sys.exit(f"ABORT: migration failed, rolled back: {e}")

    final = _counts(cur, args.new)
    leftover = _counts(cur, args.old)
    print(f"\nOK: committed. rows now under NEW key: {final}")
    if sum(leftover.values()):
        print(f"  (unexpected leftover under OLD key: {leftover})")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
