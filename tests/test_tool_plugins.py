"""Tool plugins: other packages add tools through an entry point, chosen by name.

Mirror of the context providers: ``MCP_TOOL_PLUGINS`` names the plugins to
load, each is an entry point under ``akko_mcp_trino.tools`` whose object is a
registrar ``(mcp, client) -> None``. An unknown name refuses to start.
"""

from __future__ import annotations

import pytest

from akko_mcp_trino import plugins


class _EP:
    def __init__(self, name, obj):
        self.name, self._obj = name, obj

    def load(self):
        return self._obj


def _registrar(mcp, client):
    mcp.registered = True


def test_no_plugin_named_means_no_registrar():
    assert plugins.tool_registrars({"MCP_TOOL_PLUGINS": ""}) == []
    assert plugins.tool_registrars({}) == []


def test_named_plugins_are_loaded_in_order_from_the_entry_point_group(monkeypatch):
    other = lambda mcp, client: None  # noqa: E731
    monkeypatch.setattr(
        plugins, "_entry_points", lambda: [_EP("akko-ai", _registrar), _EP("other", other)]
    )
    assert plugins.tool_registrars({"MCP_TOOL_PLUGINS": "other, akko-ai"}) == [other, _registrar]


def test_unknown_plugin_refuses_to_start_and_names_what_is_installed(monkeypatch):
    monkeypatch.setattr(plugins, "_entry_points", lambda: [_EP("akko-ai", _registrar)])
    with pytest.raises(ValueError, match=r"unknown tool plugin 'nope'.*akko-ai"):
        plugins.tool_registrars({"MCP_TOOL_PLUGINS": "nope"})


def test_entry_points_are_read_from_the_tools_group():
    """No plugin is installed in the test environment: the group is empty, not absent."""
    assert plugins.ENTRY_POINT_GROUP == "akko_mcp_trino.tools"
    assert list(plugins._entry_points()) == []
