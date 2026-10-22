from __future__ import annotations

import pytest

from report import paginated_store as ps

PROJECT = "Client_A"


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    (root / PROJECT / "paginated").mkdir(parents=True)
    monkeypatch.setattr(ps, "_store", lambda: root.resolve())
    return root.resolve()


# --- Everything lands in the store ------------------------------------------


@pytest.mark.parametrize(
    "raw,why",
    [
        ("Client_A/paginated/R.rdl", "the documented relative form"),
        ("R.rdl", "a bare file name"),
        ("R", "a bare name with no extension"),
        ("paginated/R.rdl", "'paginated' as the leading segment"),
        ("/etc/cron.d/R.rdl", "an absolute path outside the store"),
        ("../../../../etc/R.rdl", "a traversal"),
        ("..\\..\\windows\\R.rdl", "a windows-style traversal"),
        ("Client_A/../../../R.rdl", "a traversal hidden mid-path"),
    ],
)
def test_every_path_resolves_inside_the_store(store, raw, why):
    dest, err = ps.resolve_dest(raw, active_project=PROJECT)
    assert err == "", f"{why}: {err}"
    assert dest.is_relative_to(store), f"{why}: escaped to {dest}"


@pytest.mark.parametrize(
    "raw",
    ["R.rdl", "/etc/cron.d/R.rdl", "../../../../etc/R.rdl", "paginated/R.rdl"],
)
def test_the_layout_is_always_project_then_paginated(store, raw):
    dest, _err = ps.resolve_dest(raw, active_project=PROJECT)
    assert dest.parent == store / PROJECT / "paginated"
    assert dest.name == "R.rdl"


def test_a_named_project_beats_the_selected_one(store):
    dest, _err = ps.resolve_dest("Other/paginated/R.rdl", active_project=PROJECT)
    assert dest.parent == store / "Other" / "paginated"


def test_an_absolute_path_inside_the_store_keeps_its_project(store):
    given = store / "Other" / "paginated" / "R.rdl"
    dest, _err = ps.resolve_dest(str(given), active_project=PROJECT)
    assert dest == given


def test_an_extension_is_added_but_never_doubled(store):
    for raw in ("R", "R.rdl", "R.RDL"):
        dest, _err = ps.resolve_dest(raw, active_project=PROJECT)
        assert dest.name.lower().endswith(".rdl")
        assert not dest.name.lower().endswith(".rdl.rdl")


# --- Refusals ---------------------------------------------------------------


def test_no_project_anywhere_is_refused_not_guessed(store):
    dest, err = ps.resolve_dest("R.rdl", active_project="")
    assert dest is None
    assert "no project" in err.lower()


@pytest.mark.parametrize("raw", ["", "   ", "/"])
def test_an_empty_path_is_refused(store, raw):
    dest, err = ps.resolve_dest(raw, active_project=PROJECT)
    assert dest is None and err


def test_a_project_name_of_only_punctuation_does_not_become_a_directory(store):
    dest, err = ps.resolve_dest("...", active_project="!!!")
    assert dest is None and "no project" in err.lower()


# --- Finding one back -------------------------------------------------------


def test_a_report_is_found_by_every_name_it_could_be_called(store):
    f = store / PROJECT / "paginated" / "R.rdl"
    f.write_text("<Report/>", encoding="utf-8")
    for raw in ("R.rdl", "Client_A/paginated/R.rdl", str(f)):
        assert ps.find_existing(raw, active_project=PROJECT) == f, raw


def test_a_report_filed_outside_paginated_stays_readable(store):
    odd = store / PROJECT / "report" / "Legacy.rdl"
    odd.parent.mkdir(parents=True)
    odd.write_text("<Report/>", encoding="utf-8")
    assert ps.find_existing(str(odd), active_project=PROJECT) == odd


def test_a_file_outside_the_store_is_not_returned(store, tmp_path):
    outside = tmp_path / "elsewhere.rdl"
    outside.write_text("<Report/>", encoding="utf-8")
    assert ps.find_existing(str(outside), active_project=PROJECT) is None


def test_a_missing_report_is_none_rather_than_a_path(store):
    assert ps.find_existing("Nope.rdl", active_project=PROJECT) is None


def test_the_listing_covers_the_whole_store(store):
    a = store / PROJECT / "paginated" / "A.rdl"
    b = store / "Other" / "paginated" / "B.rdl"
    b.parent.mkdir(parents=True)
    for f in (a, b):
        f.write_text("<Report/>", encoding="utf-8")
    assert ps.all_rdls() == sorted([str(a), str(b)])


# --- The slug has to match the one the app writes ---------------------------


def test_the_slug_matches_create_project():
    import inspect
    import re

    from db import database

    src = inspect.getsource(database.create_project)
    found = re.search(r'slug = re\.sub\(r"(?P<pat>[^"]+)", "_", name\)', src)
    assert found, "create_project no longer builds its slug the expected way"
    assert found.group("pat") == r"[^\w\-]", found.group("pat")

    for name in ("Client A rollout", "My Project! v2", "Ünicode nàme", "a-b_c"):
        expected = re.sub(r"[^\w\-]", "_", name).strip("_")
        assert ps.project_slug(name) == expected, name


@pytest.fixture
def web_caller(monkeypatch):
    monkeypatch.setenv("FABRICBI_CALLER_EMAIL", "editor@example.test")
    monkeypatch.setenv("FABRICBI_CALLER_ROLE", "editor")
    monkeypatch.setenv("FABRICBI_CALLER_PROJECT", PROJECT)


def test_web_paths_cannot_name_another_project(store, web_caller):
    for path in ("Other/paginated/R.rdl", str(store / "Other" / "R.rdl")):
        dest, error = ps.resolve_dest(path)
        assert dest is None and error


def test_web_bare_filename_uses_the_validated_project(store, web_caller):
    dest, error = ps.resolve_dest("R.rdl", active_project="Other")
    assert error == ""
    assert dest == store / PROJECT / "paginated" / "R.rdl"


def test_web_listing_and_reads_are_confined(store, web_caller):
    own = store / PROJECT / "paginated" / "Own.RDL"
    other = store / "Other" / "paginated" / "Private.rdl"
    other.parent.mkdir(parents=True)
    for f in (own, other):
        f.write_text("<Report/>", encoding="utf-8")
    assert ps.all_rdls() == [str(own)]
    assert ps.find_existing(str(own)) == own
    assert ps.find_existing(str(other)) is None
    assert ps.find_existing("Other/paginated/Private.rdl") is None


def test_non_rdl_files_cannot_be_read_or_overwritten(store, web_caller):
    text = store / PROJECT / "paginated" / "notes.txt"
    text.write_text("private fixture", encoding="utf-8")
    assert ps.find_existing(str(text)) is None
    assert text.read_text(encoding="utf-8") == "private fixture"


def test_web_caller_without_project_has_no_file_access(store, web_caller, monkeypatch):
    monkeypatch.delenv("FABRICBI_CALLER_PROJECT")
    own = store / PROJECT / "paginated" / "Own.rdl"
    own.write_text("<Report/>", encoding="utf-8")
    assert ps.all_rdls() == []
    assert ps.find_existing(str(own)) is None
    assert ps.resolve_dest(str(own))[0] is None


def test_symlink_to_another_project_is_excluded(store, web_caller):
    private = store / "Other" / "Secret.rdl"
    private.parent.mkdir()
    private.write_text("<Report/>", encoding="utf-8")
    link = store / PROJECT / "paginated" / "Link.rdl"
    try:
        link.symlink_to(private)
    except OSError:
        pytest.skip("Symlinks unavailable on this system")
    assert ps.find_existing(str(link)) is None
    assert ps.resolve_dest(str(link))[0] is None
    assert ps.all_rdls() == []


def test_mcp_file_tools_cannot_cross_project_scope(store, web_caller, monkeypatch):
    from llm import pbi_tool_exec as executor
    from llm.mcp_tools import make_handler
    from model import tool_guards

    monkeypatch.setattr(tool_guards, "_ALLOWED_PATH_ROOTS", (str(store),))
    monkeypatch.setattr(executor, "_build_trace", lambda *a, **k: None)
    # This test targets authorization; structural validation has its own suite.
    monkeypatch.setattr("report.rdl_builder.validate_rdl", lambda xml: [])
    private = store / "Other" / "paginated" / "Private.rdl"
    private.parent.mkdir(parents=True)
    private.write_text("private fixture", encoding="utf-8")
    for tool, args in (
        ("pbi_paginated_get", {}), ("pbi_paginated_delete", {}),
        ("pbi_paginated_set", {"xml": "replacement"}),
    ):
        out = make_handler(tool)(path=str(private), **args)
        assert out.startswith("ERROR"), out
        assert private.read_text(encoding="utf-8") == "private fixture"
    assert "Private" not in make_handler("pbi_paginated_list")()

    own = store / PROJECT / "paginated" / "Own.rdl"
    make_handler("pbi_paginated_set")(path=str(own), xml="own fixture")
    assert make_handler("pbi_paginated_get")(path=str(own)) == "own fixture"
    monkeypatch.setenv("FABRICBI_CALLER_ROLE", "viewer")
    assert make_handler("pbi_paginated_delete")(path=str(own)).startswith("REFUSED")
    assert own.is_file()
    monkeypatch.setenv("FABRICBI_CALLER_ROLE", "editor")
    assert make_handler("pbi_paginated_delete")(path=str(own)).startswith("Deleted")
    assert not own.exists()


def test_resolved_target_must_also_be_an_rdl(store, web_caller, monkeypatch):
    from pathlib import Path

    link = store / PROJECT / "paginated" / "Alias.rdl"
    target = link.with_name("notes.txt")
    link.write_text("fixture", encoding="utf-8")
    target.write_text("private", encoding="utf-8")
    original = Path.resolve
    # Model a symlink on platforms that cannot create one without elevation.
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **k: (
        target if p == link else original(p, *a, **k)
    ))
    assert ps.find_existing(str(link)) is None
    assert ps.resolve_dest(str(link))[0] is None
    assert ps.all_rdls() == []
