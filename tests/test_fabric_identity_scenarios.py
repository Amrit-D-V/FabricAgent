"""Generated IDs must come from the INSERT batch, including with trigger results."""

import pytest

from db.fabric_backend import FabricCursor


class BatchCursor:
    """An ODBC result stream where an identity exists only within its insert batch."""

    def __init__(self, identity=42, trigger=False):
        self.identity = identity
        self.trigger = trigger
        self.calls = []
        self.results = []
        self.index = 0

    @property
    def description(self):
        return self.results[self.index][0] if self.index < len(self.results) else None

    @property
    def rowcount(self):
        return self.results[self.index][2] if self.index < len(self.results) else -1

    def execute(self, sql, parameters=()):
        self.calls.append((sql, parameters))
        self.index = 0
        self.results = []
        inserted = sql.lstrip().upper().startswith("INSERT INTO")
        if inserted:
            self.results.append((None, [], 1))
            if self.trigger:
                self.results.append(([("trigger_value",)], [(999,)], -1))
        if "SCOPE_IDENTITY()" in sql:
            self.results.append(
                (
                    [("__fabricbi_inserted_id",)],
                    [(self.identity if inserted else None,)],
                    -1,
                )
            )
        elif not inserted:
            self.results.append(([("value",)], [(10,)], -1))
        return self

    def nextset(self):
        self.index += 1
        return self.index < len(self.results)

    def fetchone(self):
        rows = self.results[self.index][1]
        return rows.pop(0) if rows else None

    def fetchall(self):
        return self.results[self.index][1]


@pytest.mark.parametrize("trigger", [False, True])
def test_identity_is_captured_in_the_insert_batch(trigger):
    raw = BatchCursor(trigger=trigger)
    cur = FabricCursor(raw).execute(
        "INSERT INTO projects (name) VALUES (?);", ("Alpha",)
    )
    assert cur.lastrowid == 42
    assert cur.lastrowid == 42  # Repeated access must not execute another batch.
    assert len(raw.calls) == 1
    assert raw.calls[0][1] == ("Alpha",)
    assert cur.rowcount == 1
    assert raw.index == len(raw.results)


def test_identity_survives_cursor_reuse_but_is_reset_for_a_new_statement():
    raw = BatchCursor()
    cur = FabricCursor(raw).execute(
        "INSERT INTO projects (name) VALUES (?)", ("Alpha",)
    )
    assert cur.lastrowid == 42
    raw.identity = 43
    cur.execute("INSERT INTO projects (name) VALUES (?)", ("Beta",))
    assert cur.lastrowid == 43
    cur.execute("SELECT value FROM projects")
    assert cur.lastrowid is None
    assert cur.fetchone() == {"value": 10}


def test_insert_into_a_table_without_identity_returns_none():
    cur = FabricCursor(BatchCursor(identity=None))
    cur.execute("INSERT INTO app_users (email) VALUES (?)", ("a@example.test",))
    assert cur.lastrowid is None
    assert cur.rowcount == 1


def test_id_capture_does_not_change_normal_selects():
    raw = BatchCursor()
    cur = FabricCursor(raw).execute("SELECT value FROM projects")
    assert raw.calls == [("SELECT value FROM projects", ())]
    assert cur.fetchall() == [{"value": 10}]


def test_missing_identity_result_is_an_error_not_a_silent_null():
    class TruncatedCursor(BatchCursor):
        def execute(self, sql, parameters=()):
            super().execute(sql, parameters)
            self.results = [(None, [], 1)]

    with pytest.raises(RuntimeError, match="no identity result"):
        FabricCursor(TruncatedCursor()).execute(
            "INSERT INTO projects (name) VALUES ('A')"
        )
