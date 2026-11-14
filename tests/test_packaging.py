"""The extras that install the Atlas provider, and the versions they pin."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_atlas_extras_pin_the_provider_released_with_the_core():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    extras = project["optional-dependencies"]
    # No compiler: the plain extra brings the provider alone (Basic and Knox).
    assert extras["atlas"] == [f"akko-mcp-trino-atlas=={version}"]
    # The Kerberos bindings are built from source: they are an extra of their own.
    assert extras["atlas-kerberos"] == [f"akko-mcp-trino-atlas[kerberos]=={version}"]
    # The provider is released under the same number (absent from the sdist, which prunes it).
    init = ROOT / "providers/atlas/akko_mcp_trino_atlas/__init__.py"
    if init.exists():
        assert re.search(rf'^__version__ = "{re.escape(version)}"$', init.read_text(), re.M)
