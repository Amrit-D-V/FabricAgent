"""Scenario matrix for the storage browser's boundary.

safe_resolve is the whole security model of this feature: the browser serves
whatever it returns, so anything that escapes a root is served too. As
page-local code in pages/13_Storage_Browser.py it had no tests at all, which
is why these exist now that it is importable.

The rule is one line -- resolve, then check containment -- but it has to hold
against `..`, absolute paths, symlinks pointing out, and a root that does not
exist yet.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import storage


@pytest.fixture
def root(tmp_path):
    """A storage root with a file in it, and a secret next to it -- outside."""
    inside = tmp_path / "root"
    (inside / "sub").mkdir(parents=True)
    (inside / "keep.txt").write_text("in")
    (inside / "sub" / "nested.txt").write_text("nested")
    (tmp_path / "secret.txt").write_text("out")
    return inside


# --- What is allowed through -------------------------------------------------


def test_the_root_itself_resolves(root):
    assert storage.safe_resolve(root, "") == root.resolve()


def test_a_file_in_the_root_resolves(root):
    assert storage.safe_resolve(root, "keep.txt") == (root / "keep.txt").resolve()


def test_a_nested_path_resolves(root):
    target = storage.safe_resolve(root, "sub/nested.txt")
    assert target == (root / "sub" / "nested.txt").resolve()


def test_an_interior_dotdot_that_stays_inside_is_allowed(root):
    """`sub/../keep.txt` never leaves the root, so it is legitimate."""
    assert storage.safe_resolve(root, "sub/../keep.txt") == (root / "keep.txt").resolve()


def test_a_path_that_does_not_exist_still_resolves(root):
    """Resolution is about containment, not existence -- the caller checks
    is_file()/is_dir() afterwards."""
    assert storage.safe_resolve(root, "not-here.txt") is not None


# --- What is refused ---------------------------------------------------------


@pytest.mark.parametrize(
    "escape",
    [
        "..",
        "../secret.txt",
        "sub/../../secret.txt",
        "../../etc/passwd",
        "sub/../../../..",
    ],
)
def test_traversal_out_of_the_root_is_refused(root, escape):
    assert storage.safe_resolve(root, escape) is None


def test_an_absolute_path_cannot_escape(root, tmp_path):
    """`root / "/etc/passwd"` discards the root in pathlib, so an absolute
    path is the shortest way out if containment is not re-checked."""
    assert storage.safe_resolve(root, str(tmp_path / "secret.txt")) is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need admin on Windows")
def test_a_symlink_pointing_out_of_the_root_is_refused(root, tmp_path):
    """resolve() follows symlinks, which is exactly why it is used here."""
    (root / "escape").symlink_to(tmp_path / "secret.txt")
    assert storage.safe_resolve(root, "escape") is None


def test_a_missing_root_does_not_open_the_filesystem(tmp_path):
    """A root that was never created must not resolve to the whole disk."""
    missing = tmp_path / "never-made"
    assert storage.safe_resolve(missing, "../secret.txt") is None


# --- resolve() only serves declared roots ------------------------------------


def test_an_unknown_root_label_is_refused():
    assert storage.resolve("Not A Root", "") is None


def test_every_declared_root_is_reachable():
    for label in storage.ROOTS:
        # None would mean the label is declared but unusable.
        assert storage.resolve(label, "") is not None, label


def test_a_root_label_cannot_be_smuggled_as_a_path():
    assert storage.resolve("Logs/../..", "") is None


# --- Listing and preview stay inside -----------------------------------------


def test_listing_refuses_an_escaping_path(monkeypatch, root):
    monkeypatch.setitem(storage.ROOTS, "T", (root, "test"))
    assert "error" in storage.list_dir("T", "../")


def test_listing_returns_entries_relative_to_the_root(monkeypatch, root):
    monkeypatch.setitem(storage.ROOTS, "T", (root, "test"))
    names = {e["name"] for e in storage.list_dir("T", "")["entries"]}
    assert names == {"sub", "keep.txt"}


def test_listing_puts_folders_first(monkeypatch, root):
    monkeypatch.setitem(storage.ROOTS, "T", (root, "test"))
    entries = storage.list_dir("T", "")["entries"]
    assert entries[0]["isDir"] is True


def test_preview_refuses_an_escaping_path(monkeypatch, root):
    monkeypatch.setitem(storage.ROOTS, "T", (root, "test"))
    assert storage.preview("T", "../secret.txt")["kind"] == "error"


def test_preview_reads_a_text_file(monkeypatch, root):
    monkeypatch.setitem(storage.ROOTS, "T", (root, "test"))
    result = storage.preview("T", "keep.txt")
    assert result["kind"] == "text"
    assert result["text"] == "in"


def test_preview_truncates_a_large_file(monkeypatch, root):
    monkeypatch.setitem(storage.ROOTS, "T", (root, "test"))
    monkeypatch.setattr(storage, "MAX_PREVIEW_BYTES", 4)
    (root / "big.txt").write_text("abcdefghij")
    result = storage.preview("T", "big.txt")
    assert result["truncated"] is True
    assert len(result["text"]) == 4


def test_preview_of_a_directory_is_refused(monkeypatch, root):
    monkeypatch.setitem(storage.ROOTS, "T", (root, "test"))
    assert storage.preview("T", "sub")["kind"] == "error"


# --- Zipping -----------------------------------------------------------------


def test_zipping_stops_at_the_file_cap(monkeypatch, root):
    monkeypatch.setattr(storage, "MAX_ZIP_FILES", 1)
    _blob, added, truncated = storage.zip_tree(root, "root")
    assert added == 1
    assert truncated is True


def test_zipping_a_tree_includes_nested_files(root):
    import io
    import zipfile

    blob, added, truncated = storage.zip_tree(root, "root")
    assert added == 2 and truncated is False
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert any(n.endswith("nested.txt") for n in names)


# --- Size formatting ---------------------------------------------------------


@pytest.mark.parametrize(
    "n,expected", [(0, "0 B"), (512, "512 B"), (1024, "1.0 KB"), (1536, "1.5 KB")]
)
def test_sizes_read_as_people_expect(n, expected):
    assert storage.fmt_size(n) == expected
