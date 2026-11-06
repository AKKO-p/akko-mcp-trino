"""Kerberos (SPNEGO) from a keytab, against a stand-in for pyspnego and a fake Atlas."""

import asyncio
import base64
import logging
import sys
import time

import httpx
import pytest
from akko_mcp_trino.context import ProviderRefused, ProviderUnavailable, TableRef
from atlas_replay import ATLAS, FakeAtlas, transport
from fake_spnego import install

from akko_mcp_trino_atlas import AtlasProvider, from_env
from akko_mcp_trino_atlas.settings import read_settings
from akko_mcp_trino_atlas.spnego import SpnegoAuth

OTHER = "http://atlas-b:21000"
KEYTAB = "/run/secrets/service-keytab/atlas-reader.keytab"
PRINCIPAL = "svc-reader@EXAMPLE.TEST"
KERBEROS = {
    "ATLAS_URL": ATLAS,
    "ATLAS_AUTH": "kerberos",
    "ATLAS_PRINCIPAL": PRINCIPAL,
    "ATLAS_KEYTAB": KEYTAB,
    "ATLAS_CLUSTERS": '{"lake": "primary"}',
}
INVOICES = TableRef("lake", "ledger", "invoices")
CHALLENGE = {"www-authenticate": "Negotiate"}


def build(handler, **env: str) -> AtlasProvider:
    return AtlasProvider(read_settings({**KERBEROS, **env}), transport=transport(handler))


def calls(p: AtlasProvider, count: int = 1) -> list:
    async def run():
        results = []
        try:
            for _ in range(count):
                try:
                    results.append(await p.describe([INVOICES], deadline=time.monotonic() + 5))
                except Exception as exc:  # noqa: BLE001 - the test looks at the error
                    results.append(exc)
        finally:
            await p.aclose()
        return results

    return asyncio.run(run())


def negotiating(atlas: FakeAtlas, *, session: bool = False):
    """An Atlas that asks for Negotiate, accepts a token, and with ``session`` opens one."""
    seen: list[httpx.Request] = []
    auths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        authorization = request.headers.get("authorization", "")
        auths.append(authorization)  # as it arrived: httpx sends the same request again
        has_session = "ATLASSESSIONID=s1" in request.headers.get("cookie", "")
        if not authorization.startswith("Negotiate ") and not has_session:
            return httpx.Response(401, headers=CHALLENGE)
        response = atlas(request)
        if session and not has_session:
            response.headers["set-cookie"] = "ATLASSESSIONID=s1; Path=/; HttpOnly"
        return response

    handler.seen = seen  # type: ignore[attr-defined]
    handler.auths = auths  # type: ignore[attr-defined]
    return handler


def test_kerberos_without_pyspnego_refuses_to_start(monkeypatch):
    monkeypatch.setitem(sys.modules, "spnego", None)  # the kerberos extra is not installed
    with pytest.raises(ValueError, match=r"needs the kerberos extra") as raised:
        from_env(KERBEROS)
    assert KEYTAB not in str(raised.value) and PRINCIPAL not in str(raised.value)
    # Without a principal or a keytab, it refuses to start too, extra or not.
    install(monkeypatch)
    for name in ("ATLAS_PRINCIPAL", "ATLAS_KEYTAB"):
        with pytest.raises(ValueError, match=name):
            from_env({k: v for k, v in KERBEROS.items() if k != name})


@pytest.mark.parametrize("binding", ["gssapi", "krb5"])
def test_kerberos_without_its_bindings_refuses_to_start(monkeypatch, binding):
    # pyspnego installed alone imports, but cannot speak Kerberos without its bindings.
    install(monkeypatch)
    monkeypatch.setitem(sys.modules, binding, None)
    with pytest.raises(ValueError, match=r"needs the kerberos extra"):
        from_env(KERBEROS)


def test_a_negotiate_challenge_is_answered_from_the_keytab(monkeypatch):
    fake = install(monkeypatch)
    atlas = FakeAtlas("atlas-2.4")
    handler = negotiating(atlas)

    (found,) = calls(build(handler, ATLAS_TABLE_TYPES="hive_table"))

    assert INVOICES in found
    assert handler.auths == ["", "Negotiate " + base64.b64encode(b"token-1").decode()]
    (context,) = fake.contexts
    assert context == {
        "username": ("keytab", PRINCIPAL, KEYTAB),
        "hostname": "atlas-a",
        "service": "HTTP",
        "protocol": "kerberos",
    }

    # ATLAS_SPN names the service, for an address that is not the host's canonical name.
    fake = install(monkeypatch)
    calls(build(negotiating(FakeAtlas("atlas-2.4")), ATLAS_SPN="HTTP@atlas.example.test"))
    assert {(c["service"], c["hostname"]) for c in fake.contexts} == {
        ("HTTP", "atlas.example.test")
    }


def test_each_negotiation_builds_a_new_context(monkeypatch):
    fake = install(monkeypatch)
    calls(build(negotiating(FakeAtlas("atlas-2.4")), ATLAS_TABLE_TYPES="hive_table"), count=2)
    # Without a session, each request negotiates again, each time from the keytab: an
    # expired ticket is renewed, never kept in a context of the provider's own.
    assert len(fake.contexts) == 2


def test_the_session_cookie_avoids_a_new_negotiation(monkeypatch):
    fake = install(monkeypatch)
    handler = negotiating(FakeAtlas("atlas-2.4"), session=True)
    first, second = calls(build(handler), count=2)
    assert INVOICES in first and first == second
    assert len(fake.contexts) == 1
    after = handler.auths[2:]  # past the challenge and its answer
    assert after and all(not auth for auth in after)


def test_a_passive_redirect_negotiates_again_with_the_active_server(monkeypatch):
    fake = install(monkeypatch)
    atlas = FakeAtlas("atlas-2.4")
    active = negotiating(atlas)

    def passive_then_active(request: httpx.Request) -> httpx.Response:
        if request.url.host == "atlas-a":
            target = request.url.copy_with(host="atlas-b")
            return httpx.Response(303, headers={"location": str(target)})
        return active(request)

    (found,) = calls(
        build(passive_then_active, ATLAS_URL=f"{ATLAS},{OTHER}", ATLAS_TABLE_TYPES="hive_table")
    )
    assert INVOICES in found
    assert [c["hostname"] for c in fake.contexts] == ["atlas-b"]


def test_a_rejected_token_or_another_challenge_is_refused(monkeypatch):
    fake = install(monkeypatch)
    seen = []

    def rejects(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, headers=CHALLENGE)

    (error,) = calls(build(rejects))
    assert isinstance(error, ProviderRefused)
    assert len(seen) == 2 and len(fake.contexts) == 1  # one token sent, never another

    def basic_only(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"www-authenticate": 'Basic realm="atlas"'})

    fake = install(monkeypatch)
    (error,) = calls(build(basic_only))
    assert isinstance(error, ProviderRefused) and fake.contexts == []


def test_a_failed_negotiation_is_unavailable_and_tries_no_other_address(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    fake = install(monkeypatch)
    fake.fail = RuntimeError(f"cannot find {PRINCIPAL} in {KEYTAB}")
    seen = []

    def challenge(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(401, headers=CHALLENGE)

    (error,) = calls(build(challenge, ATLAS_URL=f"{ATLAS},{OTHER}"))
    assert isinstance(error, ProviderUnavailable)
    assert seen == ["atlas-a"]
    assert [r.getMessage() for r in caplog.records] == ["atlas unreachable"]
    text = repr(error) + repr(error.__cause__) + repr(error.__context__)
    assert KEYTAB not in text and PRINCIPAL not in text


def test_the_keytab_path_is_never_printed(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger="httpx")
    install(monkeypatch)
    atlas = FakeAtlas("atlas-2.4")
    p = build(negotiating(atlas))
    lines = p.check_lines()
    assert f"atlas: kerberos principal {PRINCIPAL}, service HTTP@<host of each address>" in lines
    calls(p)
    printed = "\n".join(lines) + repr(p.settings) + repr(SpnegoAuth(PRINCIPAL, KEYTAB, ""))
    assert KEYTAB not in printed and "keytab" not in repr(p.settings).lower()
    assert KEYTAB not in caplog.text and PRINCIPAL not in caplog.text
    with pytest.raises(ValueError) as raised:
        read_settings({**KERBEROS, "ATLAS_SPN": "HTTP"})
    assert KEYTAB not in str(raised.value)


@pytest.mark.parametrize(
    ("env", "says"),
    [
        ({"ATLAS_SPN": "HTTP"}, "ATLAS_SPN must read"),
        ({"ATLAS_SPN": "@atlas"}, "ATLAS_SPN must read"),
        ({"ATLAS_SPN": "HTTP@"}, "ATLAS_SPN must read"),
    ],
)
def test_the_service_name_is_checked_at_start(monkeypatch, env, says):
    install(monkeypatch)
    with pytest.raises(ValueError, match=says):
        read_settings({**KERBEROS, **env})
    basic = {"ATLAS_URL": ATLAS, "ATLAS_USER": "r", "ATLAS_PASSWORD": "p", **env}
    with pytest.raises(ValueError, match="need ATLAS_AUTH=kerberos"):
        read_settings({**basic, "ATLAS_CLUSTERS": '{"lake": "primary"}'})


def test_the_service_name_is_the_same_for_every_address(monkeypatch):
    fake = install(monkeypatch)
    atlas = FakeAtlas("atlas-2.4")
    active = negotiating(atlas)

    def first_down(request: httpx.Request) -> httpx.Response:
        if request.url.host == "atlas-a":
            raise httpx.ConnectError("down")
        return active(request)

    env = {"ATLAS_URL": f"{ATLAS},{OTHER}", "ATLAS_TABLE_TYPES": "hive_table"}
    calls(build(first_down, ATLAS_SPN="HTTP@atlas.example.test", **env))
    calls(build(first_down, **env))
    # Set, it names one host for every address; unset, each address names its own.
    assert [c["hostname"] for c in fake.contexts] == ["atlas.example.test", "atlas-b"]
