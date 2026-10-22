from __future__ import annotations

import pytest

from db import fabric_backend as FB


class DeadSocket(Exception):
    def __init__(self, sqlstate="08S01", msg="Communication link failure (0)"):
        super().__init__(sqlstate, msg)


class FakeRawCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None

    def execute(self, sql, parameters=()):
        self._conn.attempts.append(sql)
        if self._conn.dead:
            raise DeadSocket()
        return self


class FakeRawConn:
    def __init__(self, dead=False):
        self.dead = dead
        self.attempts: list[str] = []
        self.closed = False

    def cursor(self):
        return FakeRawCursor(self)

    def close(self):
        self.closed = True


@pytest.fixture
def swap(monkeypatch):
    dead, live = FakeRawConn(dead=True), FakeRawConn(dead=False)
    monkeypatch.setattr(FB, "_to_tsql", lambda sql, cur: sql)
    monkeypatch.setattr(FB, "reset_connection", lambda: live)
    return dead, live


def test_dead_link_reconnects_and_retries(swap):
    dead, live = swap
    cur = FB.FabricConn(dead).execute("SELECT 1 FROM app_users")
    assert dead.attempts == ["SELECT 1 FROM app_users"], "should try the cached handle"
    assert live.attempts == ["SELECT 1 FROM app_users"], "should retry on a fresh one"
    assert cur is not None


def test_cursor_path_also_recovers(swap):
    dead, live = swap
    FB.FabricConn(dead).cursor().execute("SELECT 1")
    assert live.attempts == ["SELECT 1"]


def test_a_real_sql_error_is_not_retried(swap):
    dead, live = swap
    dead.dead = False
    raw = FB.FabricConn(dead)

    def boom(sql, parameters=()):
        raise DeadSocket("42000", "Invalid column name 'nope'")

    monkey = raw.cursor()
    monkey._c.execute = boom
    with pytest.raises(DeadSocket):
        monkey.execute("SELECT nope")
    assert live.attempts == [], "must not reconnect for a query error"


def test_retry_happens_once_not_forever(swap):
    dead, _ = swap
    still_dead = FakeRawConn(dead=True)
    import db.fabric_backend as mod

    mod.reset_connection = lambda: still_dead
    with pytest.raises(DeadSocket):
        mod.FabricConn(dead).execute("SELECT 1")
    assert len(still_dead.attempts) == 1, "exactly one retry"


@pytest.mark.parametrize("state", ["08S01", "08001", "08003", "08007", "HYT00"])
def test_dead_link_sqlstates_are_recognised(state):
    assert FB._is_dead_link(DeadSocket(state))


def test_message_match_covers_the_observed_wording():
    assert FB._is_dead_link(
        Exception("TCP Provider: An existing connection was forcibly closed by the remote host")
    )
    assert not FB._is_dead_link(Exception("Invalid column name 'x'"))
