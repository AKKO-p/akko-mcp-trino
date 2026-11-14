"""The settings of the provider read at start: the size of an Atlas answer."""

import asyncio

import pytest
from atlas_replay import ATLAS, FakeAtlas, transport

from akko_mcp_trino_atlas import AtlasProvider
from akko_mcp_trino_atlas.settings import read_settings

GOOD = {
    "ATLAS_URL": ATLAS,
    "ATLAS_USER": "reader",
    "ATLAS_PASSWORD": "reader-pass",
    "ATLAS_CLUSTERS": '{"lake": "primary"}',
}
SAYS = "ATLAS_MAX_RESPONSE_BYTES must be an integer of at least 1048576"


def test_cap_setting_bounds_and_check_line():
    assert read_settings(GOOD).max_response_bytes == 67_108_864
    for text, value in (("1048576", 1_048_576), (" 134217728 ", 134_217_728), ("", 67_108_864)):
        assert read_settings({**GOOD, "ATLAS_MAX_RESPONSE_BYTES": text}).max_response_bytes == (
            value
        )
    for wrong in ("1048575", "0", "-1", "1.5", "64MiB", "many", "1e9"):
        with pytest.raises(ValueError) as raised:
            read_settings({**GOOD, "ATLAS_MAX_RESPONSE_BYTES": wrong})
        assert str(raised.value) == SAYS

    def check_line(**env: str) -> str:
        settings = read_settings({**GOOD, **env})
        p = AtlasProvider(settings, transport=transport(FakeAtlas("atlas-2.4")))
        lines = p.check_lines()
        asyncio.run(p.aclose())
        (line,) = [line for line in lines if line.startswith("atlas: answer size: ")]
        return line

    assert check_line() == "atlas: answer size: 67108864 bytes at most"
    tuned = check_line(ATLAS_MAX_RESPONSE_BYTES="2097152")
    assert tuned == "atlas: answer size: 2097152 bytes at most"
