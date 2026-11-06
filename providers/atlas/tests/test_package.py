"""The package as the core finds it: its entry point and the settings its factory demands."""

from importlib.metadata import entry_points, version

import pytest

import akko_mcp_trino_atlas
from akko_mcp_trino_atlas import AtlasSettings, from_env

# A quote, a backslash and an accent, as a mounted secret may hold them.
PASSWORD = 'reader-"pass\\é'


def test_the_package_registers_its_entry_point():
    found = [ep for ep in entry_points(group="akko_mcp_trino.context") if ep.name == "atlas"]
    assert [ep.value for ep in found] == ["akko_mcp_trino_atlas:from_env"]
    assert found[0].load() is akko_mcp_trino_atlas.from_env
    assert version("akko-mcp-trino-atlas") == akko_mcp_trino_atlas.__version__


# Settings the factory refuses, and what its message says; none of them names the address.
REFUSED = [
    ({}, "ATLAS_URL is not set"),
    ({"ATLAS_URL": " , "}, "ATLAS_URL is not set"),
    ({"ATLAS_URL": "atlas:21000", "ATLAS_USER": "r", "ATLAS_PASSWORD": "p"}, "http or https"),
    ({"ATLAS_URL": "ftp://atlas:21000"}, "http or https"),
    ({"ATLAS_URL": "http://reader:secret@atlas:21000"}, "credentials"),
    ({"ATLAS_URL": "http://atlas:abc", "ATLAS_USER": "r", "ATLAS_PASSWORD": "p"}, "port"),
    (
        {
            "ATLAS_URL": "http://atlas:21000",
            "ATLAS_USER": "r",
            "ATLAS_PASSWORD": "p",
            "ATLAS_KEYTAB": "/k",
        },
        "ATLAS_AUTH=kerberos",
    ),
    (
        {
            "ATLAS_URL": "http://atlas:21000",
            "ATLAS_AUTH": "kerberos",
            "ATLAS_PRINCIPAL": "r",
            "ATLAS_KEYTAB": "/k",
            "ATLAS_KNOX_TOKEN": "t",
        },
        "does not take",
    ),
    ({"ATLAS_URL": "http://atlas:21000"}, "a way to authenticate"),
    ({"ATLAS_URL": "http://atlas:21000", "ATLAS_USER": "reader"}, "ATLAS_PASSWORD"),
    ({"ATLAS_URL": "http://atlas:21000", "ATLAS_PASSWORD": "p"}, "ATLAS_USER"),
    (
        {
            "ATLAS_URL": "http://atlas:21000",
            "ATLAS_USER": "reader",
            "ATLAS_PASSWORD": "p",
            "ATLAS_KNOX_TOKEN": "t",
        },
        "either",
    ),
    ({"ATLAS_URL": "http://atlas:21000", "ATLAS_AUTH": "ntlm"}, "basic or kerberos"),
    ({"ATLAS_URL": "http://atlas:21000", "ATLAS_AUTH": "kerberos"}, "ATLAS_PRINCIPAL"),
    (
        {"ATLAS_URL": "http://atlas:21000", "ATLAS_AUTH": "kerberos", "ATLAS_PRINCIPAL": "r"},
        "ATLAS_KEYTAB",
    ),
]


def test_from_env_requires_the_url_and_a_way_to_authenticate(tmp_path):
    for env, says in REFUSED:
        with pytest.raises(ValueError, match=says) as raised:
            from_env(env)
        assert "secret" not in str(raised.value) and "atlas:21000" not in str(raised.value)

    password = tmp_path / "password"
    password.write_text(PASSWORD + "\n", encoding="utf-8")
    token = tmp_path / "token"
    token.write_text("knox-token\n", encoding="utf-8")
    url = {"ATLAS_URL": "https://atlas-a:21443/, http://atlas-b:21000"}

    basic = from_env({**url, "ATLAS_USER": " reader ", "ATLAS_PASSWORD_FILE": str(password)})
    assert basic.urls == ("https://atlas-a:21443", "http://atlas-b:21000")
    assert (basic.auth, basic.user, basic.secret()) == ("basic", "reader", PASSWORD)
    plain = from_env({**url, "ATLAS_AUTH": "Basic", "ATLAS_USER": "r", "ATLAS_PASSWORD": "p"})
    assert (plain.auth, plain.secret()) == ("basic", "p")
    knox = from_env({**url, "ATLAS_KNOX_TOKEN_FILE": str(token)})
    assert (knox.auth, knox.user, knox.secret()) == ("knox", "", "knox-token")
    kerberos = from_env(
        {
            **url,
            "ATLAS_AUTH": "kerberos",
            "ATLAS_PRINCIPAL": "reader@EXAMPLE",
            "ATLAS_KEYTAB": " /k ",
        }
    )
    assert (kerberos.auth, kerberos.principal, kerberos.keytab, kerberos.secret()) == (
        "kerberos",
        "reader@EXAMPLE",
        "/k",
        "",
    )
    assert isinstance(kerberos, AtlasSettings) and PASSWORD not in repr(basic)
