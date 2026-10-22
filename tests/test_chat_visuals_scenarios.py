"""Scenario matrix for charts in the chat.

The assistant draws by calling `render_chart`, which runs inside the MCP
subprocess -- so its return value cannot carry the spec back. execute_pbi_tool
appends the call to bridge_io/session_<id>_tools.jsonl instead, and the turn
reads that file afterwards.

Three things have to hold for a chart to reach the browser:

  * the tool has to be reachable at all (it was omitted from the allowlist,
    which is why the assistant only ever answered with tables);
  * the option object has to survive JSON, so no JavaScript callbacks in it;
  * the log has to be drained per turn, or turn 2 re-shows turn 1's chart.
"""

from __future__ import annotations

import json
import pathlib

import pytest

import api.chat as chat


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A per-turn spec log, in a temp bridge_io."""
    monkeypatch.setattr(chat, "PROJECT_ROOT", tmp_path)
    (tmp_path / "bridge_io").mkdir()

    def write(*calls):
        log = tmp_path / "bridge_io" / "session_sid_tools.jsonl"
        log.write_text(
            "\n".join(json.dumps(c) for c in calls) + "\n", encoding="utf-8"
        )
        return log

    return write


def _chart(**over):
    spec = {
        "chart_type": "bar",
        "title": "Sales",
        "categories": ["Jan", "Feb"],
        "series": [{"name": "Total", "data": [1, 2]}],
    }
    spec.update(over)
    return {"name": "render_chart", "input": spec}


# --- The tool has to be reachable -------------------------------------------


def test_the_chart_tools_are_in_the_allowlist():
    """Omitted, the model cannot call render_chart at all and silently falls
    back to a markdown table -- which is exactly what went wrong."""
    assert "mcp__charts__*" in chat.ALLOWED_TOOLS


def test_the_charts_server_is_registered():
    servers = json.loads(chat._powerbi_mcp_config())["mcpServers"]
    assert "charts" in servers
    assert servers["charts"]["args"][0].endswith("chart_mcp_server.py")


def test_the_charts_server_exposes_only_the_two_visual_tools():
    """It is a network-reachable endpoint: the point of a second tiny server is
    that drawing a chart does not also grant the model-edit tools."""
    import chart_mcp_server

    assert set(chart_mcp_server.mcp._tool_manager._tools) == {
        "render_chart",
        "render_map",
    }


def test_both_prompts_ask_for_charts():
    for mode in ("dashboard", "general"):
        assert "render_chart" in chat.system_prompt(mode), mode


# --- Draining the spec log --------------------------------------------------


def test_a_chart_call_becomes_an_echarts_option(session):
    session(_chart())
    visuals = chat._drain_visuals("sid")
    assert len(visuals) == 1
    assert visuals[0]["kind"] == "chart"
    assert visuals[0]["title"] == "Sales"
    assert visuals[0]["option"]["series"][0]["name"] == "Total"


def test_the_option_survives_json(session):
    """A JavaScript formatter in the option would break serialisation, which is
    why the SI formatter is attached in the browser instead."""
    session(_chart())
    json.dumps(chat._drain_visuals("sid"))  # must not raise


def test_several_charts_in_one_turn_all_come_back(session):
    session(_chart(title="A"), _chart(title="B"), _chart(title="C"))
    assert [v["title"] for v in chat._drain_visuals("sid")] == ["A", "B", "C"]


def test_a_map_call_is_kept_as_a_spec(session):
    session({"name": "render_map", "input": {"title": "Sites", "points": [1, 2]}})
    visual = chat._drain_visuals("sid")[0]
    assert visual["kind"] == "map"
    assert visual["title"] == "Sites"


def test_the_log_is_deleted_after_reading(session):
    log = session(_chart())
    chat._drain_visuals("sid")
    assert not log.exists()


def test_a_second_turn_does_not_reshow_the_first_chart(session):
    session(_chart())
    assert len(chat._drain_visuals("sid")) == 1
    assert chat._drain_visuals("sid") == []


def test_no_chart_call_means_no_visuals(session):
    assert chat._drain_visuals("never-ran") == []


def test_a_corrupt_log_does_not_break_the_answer(session, tmp_path):
    """The reply is what the user is waiting for; an unreadable spec log must
    cost the chart, not the answer."""
    (tmp_path / "bridge_io" / "session_sid_tools.jsonl").write_text(
        "{not json\n", encoding="utf-8"
    )
    assert chat._drain_visuals("sid") == []


def test_blank_lines_are_skipped(session, tmp_path):
    (tmp_path / "bridge_io" / "session_sid_tools.jsonl").write_text(
        json.dumps(_chart()) + "\n\n", encoding="utf-8"
    )
    assert len(chat._drain_visuals("sid")) == 1


# --- Each turn gets its own log ---------------------------------------------


def test_the_session_id_scopes_the_log(session, tmp_path):
    """Two concurrent chats must not read each other's charts, which is why the
    id is passed per subprocess rather than set on os.environ."""
    (tmp_path / "bridge_io" / "session_one_tools.jsonl").write_text(
        json.dumps(_chart(title="mine")) + "\n", encoding="utf-8"
    )
    assert chat._drain_visuals("two") == []
    assert chat._drain_visuals("one")[0]["title"] == "mine"


def test_the_cli_receives_the_session_id_per_call():
    """extra_env scopes it to the subprocess; os.environ would race."""
    import inspect

    from llm.llm_client import _stream_claude_cli, stream_text

    assert "extra_env" in inspect.signature(stream_text).parameters
    assert "extra_env" in inspect.signature(_stream_claude_cli).parameters
    assert "PBI_AGENT_SESSION_ID" in inspect.getsource(chat.run_chat)


# --- The builder no longer needs Streamlit ----------------------------------


def test_the_option_builder_is_framework_free():
    """It was ui/echarts_builder.py and imported streamlit_echarts for a JsCode
    formatter. Nothing Streamlit may remain in the render path."""
    import inspect

    import core.echarts

    source = inspect.getsource(core.echarts)
    assert "streamlit" not in source


def test_every_chart_type_the_tool_offers_can_be_built():
    """The schema is the contract. A type the model may request but the
    builder cannot draw produces nothing at all, which is how the 3-D scatter
    went missing when its Plotly renderer was dropped."""
    from core.echarts import _build_echarts_options
    from llm.pbi_tool_specs import PBI_TOOLS

    spec = next(t for t in PBI_TOOLS if t["name"] == "render_chart")
    offered = spec["input_schema"]["properties"]["chart_type"]["enum"]

    undrawable = []
    for chart_type in offered:
        option = _build_echarts_options(
            {
                "chart_type": chart_type,
                "title": chart_type,
                "categories": ["A", "B"],
                # Three series, so the shapes needing more than one (bubble,
                # scatter3d, gantt) get what they ask for.
                "series": [
                    {"name": "X", "data": [1, 2]},
                    {"name": "Y", "data": [3, 4]},
                    {"name": "Z", "data": [5, 6]},
                ],
            }
        )
        if not option.get("series"):
            undrawable.append(chart_type)
        json.dumps(option)  # every option has to survive the wire

    assert not undrawable, f"offered but not drawable: {undrawable}"


def test_the_three_d_scatter_places_one_point_per_row():
    """Three measures, one axis each, aligned positionally against categories
    the way every other chart here aligns its series."""
    from core.echarts import _build_echarts_options

    option = _build_echarts_options(
        {
            "chart_type": "scatter3d",
            "categories": ["North", "South"],
            "series": [
                {"name": "Volume", "data": [10, 20]},
                {"name": "Margin", "data": [1.5, 2.5]},
                {"name": "Discount", "data": [5, 3]},
            ],
        }
    )
    assert option["series"][0]["type"] == "scatter3D"
    assert option["series"][0]["data"] == [[10, 1.5, 5, "North"], [20, 2.5, 3, "South"]]
    # The axes carry the measure names, or the reader cannot tell them apart.
    assert option["xAxis3D"]["name"] == "Volume"
    assert option["zAxis3D"]["name"] == "Discount"


def test_a_three_d_point_missing_a_measure_is_dropped():
    """Plotted at zero it would read as a real reading of nought."""
    from core.echarts import _build_echarts_options

    option = _build_echarts_options(
        {
            "chart_type": "scatter3d",
            "categories": ["ok", "incomplete"],
            "series": [
                {"data": [1, None]},
                {"data": [2, 2]},
                {"data": [3, 3]},
            ],
        }
    )
    assert len(option["series"][0]["data"]) == 1


@pytest.mark.parametrize("chart_type", ["bar", "line", "pie", "donut", "scatter", "area"])
def test_every_common_chart_type_builds(chart_type):
    from core.echarts import _build_echarts_options

    option = _build_echarts_options(
        {
            "chart_type": chart_type,
            "title": chart_type,
            "categories": ["A", "B"],
            "series": [{"name": "S", "data": [1, 2]}],
        }
    )
    assert option["series"]
    json.dumps(option)


# --- A chart that comes back empty must be explainable ----------------------


def test_the_raw_spec_is_kept_alongside_the_option(session):
    """An option with no data renders as a blank white box, which reads as a
    broken UI. Keeping the spec lets the page say what the model actually
    sent instead."""
    session(_chart())
    visual = chat._drain_visuals("sid")[0]
    assert visual["spec"]["chart_type"] == "bar"
    assert visual["spec"]["series"][0]["data"] == [1, 2]


def test_a_series_less_call_still_returns_a_visual(session):
    """The model can call render_chart with nothing plottable. That must reach
    the UI as an explainable empty chart, not vanish."""
    session({"name": "render_chart", "input": {"title": "Empty", "chart_type": "bar"}})
    visuals = chat._drain_visuals("sid")
    assert len(visuals) == 1
    assert visuals[0]["spec"].get("series") is None


# --- FastMCP nests **kwargs one level too deep ------------------------------


def _nested(**over):
    """The shape a real call actually arrived in: the whole spec under "kwargs".

    FastMCP models a `def handler(**kwargs)` signature as a single parameter
    literally named "kwargs", so a correct tool call lands one level down. Read
    as-is every field is missing and the chart renders as a blank white box.
    """
    inner = {
        "chart_type": "donut",
        "title": "Sales by Bike Subcategory (2020)",
        "categories": ["Road Bikes", "Mountain Bikes"],
        "series": [{"name": "Total Sales", "data": [4872483.85, 1532449.73]}],
    }
    inner.update(over)
    return {"name": "render_chart", "input": {"kwargs": inner}}


def test_a_kwargs_wrapped_spec_is_unwrapped(session):
    session(_nested())
    visual = chat._drain_visuals("sid")[0]
    assert visual["title"] == "Sales by Bike Subcategory (2020)"
    points = sum(len(s.get("data") or []) for s in visual["option"]["series"])
    assert points == 2, "the wrapper made every field read as missing"


def test_an_unwrapped_spec_still_works(session):
    """The unwrap must not break the plain shape."""
    session(_chart())
    assert chat._drain_visuals("sid")[0]["option"]["series"][0]["name"] == "Total"


def test_a_spec_with_a_real_kwargs_field_is_left_alone(session):
    """Only a lone "kwargs" key is a wrapper. A spec that happens to carry one
    alongside real fields must not be unwrapped."""
    session({"name": "render_chart", "input": {
        "chart_type": "bar", "categories": ["A"],
        "series": [{"name": "S", "data": [1]}], "kwargs": {"noise": 1},
    }})
    assert chat._drain_visuals("sid")[0]["option"]["series"][0]["data"] == [1]


# --- The advertised schema and the validated model have to agree ------------
#
# FastMCP validates a call against a model built from the handler's SIGNATURE,
# while the servers advertise the hand-written schema by overriding
# `tool.parameters`. A `def handler(**kwargs)` signature makes those two
# disagree, and both halves of the disagreement broke a chart in production:
# the advertised shape was rejected with "kwargs Field required" so the tool
# never ran, and the shape that did validate arrived one level too deep so
# every field read as missing.


def _arg_fields(server, tool: str) -> set[str]:
    """The field names FastMCP will actually validate against."""
    return set(server.mcp._tool_manager._tools[tool].fn_metadata.arg_model.model_fields)


def test_the_validated_model_names_the_schema_fields():
    import chart_mcp_server

    fields = _arg_fields(chart_mcp_server, "render_chart")
    assert {"chart_type", "categories", "series", "title"} <= fields
    assert fields != {"kwargs"}, "the **kwargs signature is what broke charts"


def test_every_pbi_tool_validates_its_own_fields_too():
    """pbi_mcp_server shared the same handler shape, so it had the same latent
    bug for all 66 tools -- charts only surfaced it first."""
    import pbi_mcp_server
    from llm.pbi_tool_specs import PBI_TOOLS

    for spec in PBI_TOOLS:
        declared = set(spec["input_schema"].get("properties") or {})
        if not declared:
            continue
        fields = _arg_fields(pbi_mcp_server, spec["name"])
        assert fields != {"kwargs"}, spec["name"]


def test_a_reserved_field_name_still_registers():
    """`model_name` collides with Pydantic's protected `model_` namespace. It
    must not take the tool down -- the handler keeps **kwargs precisely so it
    accepts whatever name validation hands back."""
    import pbi_mcp_server

    assert "model_name" in _arg_fields(pbi_mcp_server, "pbi_report_create")


def test_the_advertised_schema_is_the_hand_written_one():
    """A schema derived from a signature of Any would lose the descriptions
    and enums that tell the model which chart type fits the question."""
    import chart_mcp_server

    advertised = chart_mcp_server.mcp._tool_manager._tools["render_chart"].parameters
    assert "donut" in advertised["properties"]["chart_type"]["enum"]
    assert "kwargs" not in advertised["properties"], "the escape hatch is not advertised"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(dict, id="flat"),
        pytest.param(lambda d: {"kwargs": d}, id="kwargs-wrapped"),
        pytest.param(lambda d: dict(d, unknown_field="x"), id="with-an-unknown-field"),
    ],
)
def test_a_chart_call_reaches_the_tool_whatever_shape_it_arrives_in(payload, monkeypatch):
    """The one that matters: drive FastMCP's real validation path."""
    import asyncio

    import chart_mcp_server
    import llm.mcp_tools as mcp_tools

    seen = {}
    monkeypatch.setattr(
        mcp_tools, "execute_pbi_tool", lambda name, args: seen.update(args) or "ok"
    )
    spec = {
        "chart_type": "donut",
        "title": "Sales",
        "categories": ["A", "B"],
        "series": [{"name": "Total", "data": [1, 2]}],
    }
    asyncio.run(
        chart_mcp_server.mcp._tool_manager.call_tool("render_chart", payload(spec))
    )
    assert seen.get("chart_type") == "donut"
    assert seen.get("series") == spec["series"]
    assert "unknown_field" not in seen, "only the schema's own fields reach the tool"


def test_unsent_optional_fields_do_not_arrive_as_null(monkeypatch):
    """Every generated field is defaulted, so an unsent one arrives as None.
    The tools distinguish "not provided" from "provided as null"."""
    import asyncio

    import chart_mcp_server
    import llm.mcp_tools as mcp_tools

    seen = {}
    monkeypatch.setattr(
        mcp_tools, "execute_pbi_tool", lambda name, args: seen.update(args) or "ok"
    )
    asyncio.run(
        chart_mcp_server.mcp._tool_manager.call_tool(
            "render_chart", {"chart_type": "bar", "series": [{"name": "S", "data": [1]}]}
        )
    )
    assert set(seen) == {"chart_type", "series"}
    assert all(v is not None for v in seen.values())


def test_a_property_that_cannot_be_a_parameter_falls_back():
    """Rather than register something subtly wrong, an unusable property name
    leaves the **kwargs signature in place."""
    from llm.mcp_tools import signature_for

    assert signature_for({"properties": {"class": {}}}) is None
    assert signature_for({"properties": {"_private": {}}}) is None
    assert signature_for({"properties": {"has space": {}}}) is None
    assert signature_for({"properties": {}}) is None
