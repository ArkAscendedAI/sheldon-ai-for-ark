"""Persistence foundation for the Sheldon bridge.

Two SQLite stores:
  - campaign.db  — durable, cluster-scoped (survivor memory, lorebook, chat history)
  - telemetry.db — volatile, server-scoped (live "world" state)

This package holds the SQL schema, a forward-only migration runner, the
per-cluster/per-server connection manager, and the data-access stores. Nothing
here is imported by the live server path until Phase 0 integration (task #6),
so it is safe to build and test in isolation.
"""
