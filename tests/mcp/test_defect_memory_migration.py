"""Re-keying a `memory.db` written before (target, fingerprint) was the key.

SQLite cannot alter a primary key, so `_migrate` rebuilds the table: create,
copy, drop, rename, in one transaction, after copying the file aside. The memory
deliberately outlives a release, so every row must survive it -- `''` legacy
rows included and unchanged -- and running it twice must do nothing.

The old schemas below are the shipped DDL, written out by hand rather than
derived from the module, because a fixture built from the code under test would
migrate from whatever the code now says the old schema was.
"""

from __future__ import annotations

import sqlite3

import pytest

from qaas.mcp import defect_memory
from qaas.mcp.defect_memory import MEMORY_DB

# 0.0.1: no `target` column, no `outcomes` table.
DEFECTS_0_0_1 = """
CREATE TABLE defects (
    fingerprint         TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    summary             TEXT NOT NULL DEFAULT '',
    domain              TEXT NOT NULL,
    defect_class        TEXT NOT NULL DEFAULT '',
    service             TEXT,
    endpoint            TEXT,
    ui_route            TEXT,
    paths               TEXT NOT NULL DEFAULT '[]',
    ticket_key          TEXT,
    occurrence_count    INTEGER NOT NULL DEFAULT 1,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    last_run_id         TEXT,
    resolved_at         TEXT,
    resolved_ticket_key TEXT
);
CREATE INDEX defects_domain ON defects(domain);
"""

# What every memory written since looks like: `target` appended by ALTER, the
# key still `fingerprint` alone, and `outcomes` keyed without its target.
OLD_SCHEMA = DEFECTS_0_0_1 + """
ALTER TABLE defects ADD COLUMN target TEXT NOT NULL DEFAULT '';
CREATE TABLE outcomes (
    fingerprint TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    run_id      TEXT NOT NULL,
    at          TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    agent       TEXT,
    detail      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (fingerprint, run_id, outcome)
);
CREATE INDEX outcomes_fingerprint ON outcomes(fingerprint);
"""

DEFECT_ROWS = [
    # (fingerprint, target, title, ticket_key, occurrence_count, resolved_at)
    ("sha256:legacy", "", "recorded by 0.0.1", "SHOP-1", 3, "2026-02-01"),
    ("sha256:users", "shop-a", "user list leaks emails", "SHOP-7", 1, None),
    ("sha256:orders", "merchant", "order list ignores limit", None, 2, None),
]
OUTCOME_ROWS = [
    # (fingerprint, target, run_id, outcome, detail)
    ("sha256:users", "shop-a", "run-1", "held", "confidence 0.4"),
    ("sha256:users", "shop-a", "run-2", "verified", ""),
    ("sha256:orders", "merchant", "run-1", "not_reproducible", ""),
]


def _old_memory(root) -> None:
    conn = sqlite3.connect(root / MEMORY_DB)
    conn.executescript(OLD_SCHEMA)
    conn.executemany(
        "INSERT INTO defects (fingerprint, target, title, domain, ticket_key, "
        "occurrence_count, first_seen, last_seen, resolved_at, paths) "
        "VALUES (?, ?, ?, 'api', ?, ?, '2026-01-01', '2026-03-01', ?, '[\"api/x.py\"]')",
        DEFECT_ROWS,
    )
    conn.executemany(
        "INSERT INTO outcomes (fingerprint, target, run_id, at, outcome, detail) "
        "VALUES (?, ?, ?, '2026-03-01', ?, ?)",
        OUTCOME_ROWS,
    )
    conn.commit()
    conn.close()


def _snapshot(path) -> dict:
    """Everything that a migration could lose or change, read with plain sqlite3.

    NULLs are left out, so a column a later migration *adds* (empty on every old
    row) does not read as a change -- while a value that became NULL still does.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return {
            table: sorted(
                tuple(sorted((k, v) for k, v in dict(r).items() if v is not None))
                for r in conn.execute(f"SELECT * FROM {table}")
            )
            for table in ("defects", "outcomes")
        }
    finally:
        conn.close()


def _keys(conn) -> dict[str, tuple[str, ...]]:
    return {table: defect_memory._primary_key(conn, table) for table in defect_memory._KEYS}


def _backups(root) -> list:
    return sorted(root.glob(f"{MEMORY_DB}.bak-*"))


def test_an_old_memory_is_rekeyed_with_every_row_kept(tmp_path):
    _old_memory(tmp_path)
    before = _snapshot(tmp_path / MEMORY_DB)

    conn = defect_memory.connect(tmp_path)
    try:
        assert _keys(conn) == {
            "defects": ("target", "fingerprint"),
            "outcomes": ("target", "fingerprint", "run_id", "outcome"),
        }
        indexes = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"defects_domain", "outcomes_fingerprint"} <= indexes, "the rebuild dropped an index"
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables == {"defects", "outcomes"}, "a scratch table was left behind"
    finally:
        conn.close()

    # Every row, every column, byte for byte -- the `''` row still `''`.
    assert _snapshot(tmp_path / MEMORY_DB) == before
    legacy = [dict(r) for r in before["defects"] if dict(r)["target"] == ""]
    assert len(legacy) == 1 and legacy[0]["fingerprint"] == "sha256:legacy"

    # Copied aside first, under the name the hand-made backup already used, and
    # the copy is the old schema with the old rows.
    (backup,) = _backups(tmp_path)
    assert _snapshot(backup) == before
    old = sqlite3.connect(backup)
    old.row_factory = sqlite3.Row
    try:
        assert defect_memory._primary_key(old, "defects") == ("fingerprint",)
    finally:
        old.close()


def test_the_rekeyed_memory_holds_one_fingerprint_in_two_targets(tmp_path):
    """The point of the migration: before it, this INSERT was a UNIQUE violation."""
    _old_memory(tmp_path)
    defect_memory.connect(tmp_path).close()

    conn = defect_memory.connect(tmp_path)
    try:
        conn.execute(
            "INSERT INTO defects (fingerprint, target, title, domain, first_seen, last_seen) "
            "VALUES ('sha256:users', 'shop-b', 'a different defect', 'api', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO outcomes (fingerprint, target, run_id, at, outcome) "
            "VALUES ('sha256:users', 'shop-b', 'run-1', 'now', 'held')"
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM defects WHERE fingerprint = 'sha256:users'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_a_0_0_1_memory_gains_the_column_and_the_key_in_one_step(tmp_path):
    conn = sqlite3.connect(tmp_path / MEMORY_DB)
    conn.executescript(DEFECTS_0_0_1)
    conn.execute(
        "INSERT INTO defects (fingerprint, title, domain, first_seen, last_seen) "
        "VALUES ('sha256:old', 'a defect from 0.0.1', 'api', '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    conn = defect_memory.connect(tmp_path)
    try:
        assert _keys(conn)["defects"] == ("target", "fingerprint")
        row = conn.execute("SELECT * FROM defects").fetchone()
        assert (row["fingerprint"], row["target"], row["title"]) == (
            "sha256:old", "", "a defect from 0.0.1",
        )
    finally:
        conn.close()


def test_rerunning_the_migration_is_a_no_op(tmp_path, monkeypatch):
    _old_memory(tmp_path)
    defect_memory.connect(tmp_path).close()

    def schema():
        conn = sqlite3.connect(tmp_path / MEMORY_DB)
        try:
            return sorted(conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
        finally:
            conn.close()

    migrated, rows, backups = schema(), _snapshot(tmp_path / MEMORY_DB), _backups(tmp_path)

    def must_not_rebuild(conn, table):
        raise AssertionError(f"rebuilt {table} a second time")

    monkeypatch.setattr(defect_memory, "_rekey", must_not_rebuild)
    conn = defect_memory.connect(tmp_path)
    try:
        defect_memory._migrate(conn)  # and once more, explicitly
    finally:
        conn.close()

    assert schema() == migrated
    assert _snapshot(tmp_path / MEMORY_DB) == rows
    assert _backups(tmp_path) == backups, "a no-op migration wrote another backup"


def test_a_fresh_memory_is_created_with_the_current_key_and_no_backup(tmp_path):
    """Guards `_KEYS` against drifting from the DDL: if they disagreed, every
    `connect` would rebuild the table and write a backup, forever."""
    conn = defect_memory.connect(tmp_path)
    try:
        assert _keys(conn) == {table: key for table, (key, _) in defect_memory._KEYS.items()}
        assert defect_memory._stale_keys(conn) == []
    finally:
        conn.close()
    assert _backups(tmp_path) == []


def test_a_rebuild_that_fails_part_way_rolls_back_all_of_it(tmp_path, monkeypatch):
    """One transaction: `defects` already rebuilt, `outcomes` fails, and the file
    is left exactly as it was rather than half-migrated."""
    _old_memory(tmp_path)
    before = _snapshot(tmp_path / MEMORY_DB)
    real = defect_memory._rekey

    def fails_on_outcomes(conn, table):
        real(conn, table)
        if table == "outcomes":
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(defect_memory, "_rekey", fails_on_outcomes)
    with pytest.raises(sqlite3.OperationalError):
        defect_memory.connect(tmp_path)

    conn = sqlite3.connect(tmp_path / MEMORY_DB)
    conn.row_factory = sqlite3.Row
    try:
        assert _keys(conn) == {
            "defects": ("fingerprint",),
            "outcomes": ("fingerprint", "run_id", "outcome"),
        }
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables == {"defects", "outcomes"}
    finally:
        conn.close()
    assert _snapshot(tmp_path / MEMORY_DB) == before

    # And the next open, with nothing failing, completes it.
    monkeypatch.setattr(defect_memory, "_rekey", real)
    conn = defect_memory.connect(tmp_path)
    try:
        assert defect_memory._stale_keys(conn) == []
    finally:
        conn.close()
    assert _snapshot(tmp_path / MEMORY_DB) == before


def test_a_migration_another_process_finished_first_is_not_repeated(tmp_path, monkeypatch):
    """Several agents open the memory at once. The unlocked check can say "stale"
    and be out of date by the time the lock is held; the check under the lock is
    what stops a second rebuild."""
    _old_memory(tmp_path)
    before = _snapshot(tmp_path / MEMORY_DB)
    real_rekey = defect_memory._rekey
    real_backup = defect_memory._backup
    rebuilt_by_us: list[str] = []

    def backup_then_lose_the_race(conn):
        made = real_backup(conn)
        other = sqlite3.connect(tmp_path / MEMORY_DB)
        other.row_factory = sqlite3.Row
        other.execute("BEGIN IMMEDIATE")
        for table in defect_memory._stale_keys(other):
            real_rekey(other, table)
        other.commit()
        other.close()
        return made

    monkeypatch.setattr(defect_memory, "_backup", backup_then_lose_the_race)
    monkeypatch.setattr(defect_memory, "_rekey", lambda conn, table: rebuilt_by_us.append(table))
    defect_memory.connect(tmp_path).close()

    assert rebuilt_by_us == []
    assert _snapshot(tmp_path / MEMORY_DB) == before
    # The copy taken before losing the race is kept: it predates any rebuild.
    (backup,) = _backups(tmp_path)
    assert _snapshot(backup) == before
