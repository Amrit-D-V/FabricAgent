from __future__ import annotations

import re
from pathlib import Path

import pytest

ROUTES_FILE = Path(__file__).resolve().parent.parent / "api" / "routes_storage.py"
STORAGE_PATHS = (
    "/storage/roots",
    "/storage/list",
    "/storage/preview",
    "/storage/file",
    "/storage/zip",
)


@pytest.fixture(scope="module")
def source() -> str:
    return ROUTES_FILE.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", STORAGE_PATHS)
def test_storage_route_requires_admin(source: str, path: str) -> None:
    decorator = re.search(
        r'@route\(\s*"%s"\s*(?P<rest>[^)]*)\)' % re.escape(path), source
    )
    assert decorator, f"{path} is no longer registered -- update this test"
    rest = decorator.group("rest")
    assert 'capability="admin"' in rest, (
        f'{path} lost capability="admin". storage.resolve() does not scope by '
        f"user, so this route would expose every client's files to any signed-in "
        f"user."
    )


def test_no_storage_route_is_public(source: str) -> None:
    assert "public=True" not in source, (
        "a storage route was made public -- these stream files off disk"
    )


def test_viewer_capability_would_be_a_noop() -> None:
    from auth.auth import _ROLE_RANK

    assert _ROLE_RANK.get("viewer") == 0
    assert _ROLE_RANK["admin"] > _ROLE_RANK["editor"] > _ROLE_RANK["viewer"]
