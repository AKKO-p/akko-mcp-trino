"""The tools a server exposes: groups (MCP_TOOLSETS), tools added (MCP_TOOLS) or excluded
(MCP_EXCLUDE_TOOLS). Unknown names refuse to start; a tool left out is not registered."""

import anyio
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from akko_mcp_trino.__main__ import check_config
from akko_mcp_trino.config import BUILTIN_TOOLS, TOOLSETS, Config
from akko_mcp_trino.server import build_server, instructions_for
from tests.tool_doubles import core_config


def _real(name, instructions=None):
    return MCPServer(name, instructions=instructions)


def _listed(mcp) -> list[str]:
    return [tool.name for tool in anyio.run(mcp.list_tools)]


def test_the_default_exposes_the_nine_tools_and_says_nothing():
    config = core_config()
    assert config.exposed_tools() == list(BUILTIN_TOOLS) and len(BUILTIN_TOOLS) == 9
    assert not config.tools_restricted() and "exposes these tools" not in instructions_for(config)
    mcp, _ = build_server(config, mcp_factory=_real)
    assert _listed(mcp) == list(BUILTIN_TOOLS)


def test_groups_tools_and_exclusions_combine_in_the_servers_order():
    config = core_config(
        toolsets="query, profile", tools="describe_table", exclude_tools="explain_query"
    )
    assert config.exposed_tools() == ["describe_table", "profile_table", "execute_query"]
    assert core_config(toolsets="").exposed_tools() == []
    assert config.problems() == []
    assert core_config(toolsets="discovery").exposed_tools() == list(TOOLSETS["discovery"])


def test_a_tool_left_out_is_not_listed_and_its_call_is_refused_as_unknown():
    config = core_config(toolsets="discovery", exclude_tools="search_columns")
    mcp, _ = build_server(config, mcp_factory=_real)
    assert _listed(mcp) == [t for t in TOOLSETS["discovery"] if t != "search_columns"]
    for name in ("execute_query", "search_columns"):
        with pytest.raises(ToolError, match="Unknown tool"):
            anyio.run(mcp.call_tool, name, {"sql": "SELECT 1"})


def test_the_hosts_are_told_which_tools_are_exposed_when_fewer_than_nine():
    config = core_config(toolsets="discovery")
    assert instructions_for(config).endswith(
        " The administrator of this server exposes these tools only: "
        + ", ".join(TOOLSETS["discovery"])
        + "."
    )
    both = instructions_for(core_config(toolsets="query"))
    assert both.startswith("WARNING, set by the administrator of this server: MCP_TOOLSETS")
    assert both.endswith("exposes these tools only: explain_query, execute_query.")


@pytest.mark.parametrize(
    ("over", "problem"),
    [
        ({"toolsets": "discovery,writes"}, "MCP_TOOLSETS names 'writes', which is no group"),
        ({"tools": "drop_table"}, "MCP_TOOLS names 'drop_table', which is no tool"),
        ({"toolsets": "", "tools": ""}, "leave no tool exposed"),
        ({"exclude_tools": ",".join(BUILTIN_TOOLS)}, "leave no tool exposed"),
    ],
)
def test_an_unknown_name_or_no_tool_at_all_refuses_to_start(over, problem):
    config = core_config(**over)
    assert any(problem in p for p in config.problems()), config.problems()
    with pytest.raises(ValueError, match=problem.split(",")[0].replace("'", ".")):
        build_server(config, mcp_factory=_real)


def test_leaving_out_every_discovery_tool_is_warned_of():
    warning = (
        "MCP_TOOLSETS leaves out every discovery tool: the agent writes its queries without "
        "listing or describing the tables it may read; add discovery"
    )
    assert warning in core_config(toolsets="query,profile").warnings()
    assert warning not in core_config(toolsets="query", tools="list_tables").warnings()
    assert warning not in core_config().warnings()


def _plugin(mcp, client):
    @mcp.tool(name="plugin_tool")
    def plugin_tool() -> str:
        """A plugin's tool."""
        return "{}"

    mcp.add_tool(lambda: "{}", name="plugin_other")


def test_a_plugins_tools_are_exposed_unless_excluded_and_an_unknown_exclusion_refuses():
    mcp, _ = build_server(
        core_config(toolsets="query"), mcp_factory=_real, extra_tool_registrars=[_plugin]
    )
    assert _listed(mcp) == ["explain_query", "execute_query", "plugin_tool", "plugin_other"]
    config = core_config(toolsets="query", exclude_tools="plugin_tool")
    mcp, _ = build_server(config, mcp_factory=_real, extra_tool_registrars=[_plugin])
    assert _listed(mcp) == ["explain_query", "execute_query", "plugin_other"]
    with pytest.raises(ValueError, match="MCP_EXCLUDE_TOOLS names 'no_such_tool'"):
        build_server(
            core_config(exclude_tools="no_such_tool"),
            mcp_factory=_real,
            extra_tool_registrars=[_plugin],
        )


def test_a_plugin_alone_may_serve_and_nothing_left_at_all_refuses():
    only_plugin = core_config(toolsets="", tool_plugins="p")
    assert only_plugin.problems() == []
    mcp, _ = build_server(only_plugin, mcp_factory=_real, extra_tool_registrars=[_plugin])
    assert _listed(mcp) == ["plugin_tool", "plugin_other"]
    nothing = core_config(toolsets="", tool_plugins="p", exclude_tools="plugin_tool,plugin_other")
    with pytest.raises(ValueError, match="leave no tool exposed"):
        build_server(nothing, mcp_factory=_real, extra_tool_registrars=[_plugin])


def test_no_setting_reopens_a_write():
    """The read-only check is TRINO_READ_ONLY's alone: the query group stays annotated
    read-only, whatever the tools chosen."""
    mcp, _ = build_server(core_config(toolsets="query"), mcp_factory=_real)
    execute = {t.name: t for t in anyio.run(mcp.list_tools)}["execute_query"]
    assert (
        execute.annotations.read_only_hint is True and execute.annotations.destructive_hint is False
    )
    for over in (
        {"toolsets": "query"},
        {"tools": "execute_query"},
        {"exclude_tools": "explain_query"},
    ):
        assert core_config(**over).read_only is True


def test_check_says_the_tools_exposed(capsys):
    assert check_config(core_config(toolsets="discovery,query", exclude_tools="explain_query")) == 0
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("tools exposed:"))
    assert line == (
        "tools exposed: "
        + ", ".join([*TOOLSETS["discovery"], "execute_query"])
        + "; excluded: explain_query"
    )
    check_config(core_config())
    assert "tools exposed: " + ", ".join(BUILTIN_TOOLS) + " (all nine)" in capsys.readouterr().out
    assert check_config(core_config(toolsets="nope")) == 2


def test_the_settings_are_read_from_the_environment(monkeypatch):
    for name in ("MCP_TOOLSETS", "MCP_TOOLS", "MCP_EXCLUDE_TOOLS"):
        monkeypatch.delenv(name, raising=False)
    assert Config.from_env().toolsets is None  # unset: every group
    monkeypatch.setenv("MCP_TOOLSETS", "")
    monkeypatch.setenv("MCP_TOOLS", "execute_query")
    monkeypatch.setenv("MCP_EXCLUDE_TOOLS", "explain_query")
    config = Config.from_env()
    assert (config.toolsets, config.tools, config.exclude_tools) == (
        "",
        "execute_query",
        "explain_query",
    )
    assert config.exposed_tools() == ["execute_query"]
