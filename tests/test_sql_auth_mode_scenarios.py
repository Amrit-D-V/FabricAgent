"""The API's auth identifiers and stored display names select the same driver."""

import pytest

from fabric import fabric_sql as sql


@pytest.mark.parametrize(
    "identifier,connector",
    [
        ("aad_token", "_connect_aad_token"),
        ("service_principal", "_connect_service_principal"),
        ("sql", "_connect_sql_auth"),
        ("aad_password", "_connect_aad_password"),
    ],
)
def test_auth_identifiers_and_legacy_names_use_the_right_connector(
    monkeypatch, identifier, connector
):
    calls = []
    for name in (
        "_connect_aad_token",
        "_connect_service_principal",
        "_connect_sql_auth",
        "_connect_aad_password",
    ):
        monkeypatch.setattr(
            sql, name, lambda *args, name=name: calls.append((name, args)) or "conn"
        )
    for mode in (identifier, sql.AUTH_MODES_BY_ID[identifier]):
        assert (
            sql._open_connection("server", "database", mode, "reader", "secret")
            == "conn"
        )
    expected = ("server", "database")
    if identifier in {"sql", "aad_password"}:
        expected += ("reader", "secret")
    assert calls == [(connector, expected), (connector, expected)]


def test_unknown_auth_mode_is_rejected():
    with pytest.raises(ValueError, match="Unknown auth mode"):
        sql._open_connection("server", "database", "typo")
