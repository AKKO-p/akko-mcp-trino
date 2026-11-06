"""The package as the core finds it: its entry point and the settings its factory demands."""

from importlib.metadata import entry_points, version

import pytest
from fake_spnego import install

import akko_mcp_trino_atlas
from akko_mcp_trino_atlas import AtlasProvider, AtlasSettings, from_env
from akko_mcp_trino_atlas.settings import read_settings

# A quote, a backslash and an accent, as a mounted secret may hold them.
PASSWORD = 'reader-"pass\\é'


def test_the_package_registers_its_entry_point():
    found = [ep for ep in entry_points(group="akko_mcp_trino.context") if ep.name == "atlas"]
    assert [ep.value for ep in found] == ["akko_mcp_trino_atlas:from_env"]
    assert found[0].load() is akko_mcp_trino_atlas.from_env
    assert version("akko-mcp-trino-atlas") == akko_mcp_trino_atlas.__version__


CLUSTERS = {"ATLAS_CLUSTERS": '{"lake": "primary"}'}
GOOD = {"ATLAS_URL": "http://atlas:21000", "ATLAS_USER": "r", "ATLAS_PASSWORD": "p", **CLUSTERS}

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
    ({**GOOD, "ATLAS_CLUSTERS": ""}, "ATLAS_CLUSTERS is not set"),
    ({**GOOD, "ATLAS_CLUSTERS": "{lake"}, "not JSON"),
    ({**GOOD, "ATLAS_CLUSTERS": '["lake"]'}, "map each Trino catalog"),
    ({**GOOD, "ATLAS_CLUSTERS": "{}"}, "map each Trino catalog"),
    ({**GOOD, "ATLAS_CLUSTERS": '{"lake": " "}'}, "map each Trino catalog"),
    ({**GOOD, "ATLAS_CLUSTERS": '{"lake": 1}'}, "map each Trino catalog"),
    ({**GOOD, "ATLAS_CLUSTERS": '{" ": "primary"}'}, "map each Trino catalog"),
    ({**GOOD, "ATLAS_CLUSTERS": '{"Lake": "a", "lake": "b"}'}, "twice once folded"),
    ({**GOOD, "ATLAS_TABLE_TYPES": " , "}, "ATLAS_TABLE_TYPES"),
    ({**GOOD, "ATLAS_MAX_QUERY_CHARS": "many"}, "ATLAS_MAX_QUERY_CHARS"),
    ({**GOOD, "ATLAS_MAX_QUERY_CHARS": "0"}, "ATLAS_MAX_QUERY_CHARS"),
    ({**GOOD, "ATLAS_MAX_QUERY_CHARS": "1.5"}, "ATLAS_MAX_QUERY_CHARS"),
    ({**GOOD, "ATLAS_TIMEOUT_SECONDS": "-1"}, "ATLAS_TIMEOUT_SECONDS"),
    ({**GOOD, "ATLAS_TIMEOUT_SECONDS": "nan"}, "ATLAS_TIMEOUT_SECONDS"),
    ({**GOOD, "ATLAS_OWNER_ATTRIBUTE": "owner"}, "ATLAS_OWNER_ATTRIBUTE"),
    ({**GOOD, "ATLAS_OWNER_ATTRIBUTE": ".owner"}, "ATLAS_OWNER_ATTRIBUTE"),
]


def test_from_env_requires_the_url_a_way_to_authenticate_and_the_clusters(tmp_path, monkeypatch):
    install(monkeypatch)  # a stand-in for the kerberos extra
    for env, says in REFUSED:
        with pytest.raises(ValueError, match=says) as raised:
            from_env(env)
        assert "secret" not in str(raised.value) and "atlas:21000" not in str(raised.value)

    password = tmp_path / "password"
    password.write_text(PASSWORD + "\n", encoding="utf-8")
    token = tmp_path / "token"
    token.write_text("knox-token\n", encoding="utf-8")
    url = {"ATLAS_URL": "https://atlas-a:21443/, http://atlas-b:21000", **CLUSTERS}

    provider = from_env({**url, "ATLAS_USER": " reader ", "ATLAS_PASSWORD_FILE": str(password)})
    assert isinstance(provider, AtlasProvider)
    basic = provider.settings
    assert basic.urls == ("https://atlas-a:21443", "http://atlas-b:21000")
    assert (basic.auth, basic.user, basic.secret()) == ("basic", "reader", PASSWORD)
    plain = read_settings({**url, "ATLAS_AUTH": "Basic", "ATLAS_USER": "r", "ATLAS_PASSWORD": "p"})
    assert (plain.auth, plain.secret()) == ("basic", "p")
    knox = read_settings({**url, "ATLAS_KNOX_TOKEN_FILE": str(token)})
    assert (knox.auth, knox.user, knox.secret()) == ("knox", "", "knox-token")
    kerberos = read_settings(
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


def test_the_settings_have_their_defaults_and_fold_the_catalogs():
    settings = read_settings({**GOOD, "ATLAS_CLUSTERS": '{" Lake ": " primary "}'})
    assert dict(settings.clusters) == {"lake": "primary"}
    assert settings.table_types == ("hive_table", "iceberg_table")
    assert (settings.max_query_chars, settings.timeout_seconds) == (6000, 5.0)
    assert settings.glossaries == settings.classifications == frozenset()
    assert settings.business_metadata == frozenset()
    assert (settings.owner_attribute, settings.term_status_attribute) == ("", "")
    assert (settings.verify, settings.proxy) == (True, "")
    tuned = read_settings(
        {
            **GOOD,
            "ATLAS_TABLE_TYPES": "iceberg_table, hive_table,iceberg_table",
            "ATLAS_MAX_QUERY_CHARS": "4000",
            "ATLAS_TIMEOUT_SECONDS": "2.5",
            "ATLAS_GLOSSARIES": "finance, *",
            "ATLAS_TERM_STATUS_ATTRIBUTE": " validation ",
        }
    )
    assert tuned.table_types == ("iceberg_table", "hive_table")
    assert (tuned.max_query_chars, tuned.timeout_seconds) == (4000, 2.5)
    assert tuned.glossaries == frozenset({"finance", "*"})
    assert tuned.term_status_attribute == "validation"
