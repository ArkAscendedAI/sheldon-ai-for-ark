-- Persist oxygen in the cached player_state snapshot. get_vitals always RETURNED oxygen
-- live, but there was no column to store it, so the assembler's "implant sensors" block
-- couldn't show it on later turns (it was dropped from _DB_VITALS/_VITALS). Additive,
-- nullable — existing rows get NULL (the assembler skips None), so this is a clean forward
-- migration with no backfill needed.
ALTER TABLE player_state ADD COLUMN oxygen REAL;
