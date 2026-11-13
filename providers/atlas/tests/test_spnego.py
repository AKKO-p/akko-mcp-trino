"""Kerberos (SPNEGO) from a keytab, against a stand-in for pyspnego and a fake Atlas."""

import asyncio
import base64
import logging
import sys
import time

import httpx
import pytest
from akko_mcp_trino.context import (
    OK,
    UNAVAILABLE,
    ContextChain,
    ContextLoop,
    ContextSettings,
    ProviderRefused,
    ProviderUnavailable,
    TableRef,
)
from atlas_replay import ATLAS, Counted, FakeAtlas, transport
from fake_spnego import install

from akko_mcp_trino_atlas import AtlasProvider, from_env
from akko_mcp_trino_atlas.settings import read_settings
from akko_mcp_trino_atlas.spnego import SpnegoAuth

OTHER = "https://atlas-b:21443"
# Set for each test to a file of its own (see ``keytab_file``): it must exist at start.
KEYTAB = "service-keytab/atlas-reader.keytab"
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


class Krb5Error(Exception):
    """As ``krb5.Krb5Error`` carries it: a message and an MIT Kerberos code."""

    def __init__(self, message: str, err_code: int) -> None:
        super().__init__(message)
        self.err_code = err_code


class SpnegoError(Exception):
    """As ``spnego.exceptions.SpnegoError`` wraps a GSSAPI error, whose minor code is
    unsigned."""

    def __init__(self, base_error: Exception) -> None:
        super().__init__("Getting GSSAPI credential")
        self.base_error = base_error


class GSSError(Exception):
    def __init__(self, min_code: int) -> None:
        super().__init__("Cannot contact any KDC")
        self.min_code = min_code


PREAUTH_FAILED = -1765328360  # KRB5KDC_ERR_PREAUTH_FAILED
PRINCIPAL_UNKNOWN = -1765328378  # KRB5KDC_ERR_C_PRINCIPAL_UNKNOWN
# From krb5.h of MIT Kerberos.
KDC_UNREACH = -1765328228  # KRB5_KDC_UNREACH
REALM_UNKNOWN = -1765328230  # KRB5_REALM_UNKNOWN
REALM_CANT_RESOLVE = -1765328164  # KRB5_REALM_CANT_RESOLVE
SERVICE_UNKNOWN = -1765328229  # KRB5_SERVICE_UNKNOWN: the KDC answered


@pytest.fixture(autouse=True)
def keytab_file(tmp_path, monkeypatch):
    """A keytab of the test's own: the provider checks at start that it can be read."""
    path = tmp_path / "service-keytab" / "atlas-reader.keytab"
    path.parent.mkdir()
    path.write_bytes(b"\x05\x02 not a real keytab")
    monkeypatch.setattr(sys.modules[__name__], "KEYTAB", str(path))
    monkeypatch.setitem(KERBEROS, "ATLAS_KEYTAB", str(path))
    return path


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
        "protocol": "negotiate",
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


def challenge_counting(seen: list[str]):
    def challenge(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(401, headers=CHALLENGE)

    return challenge


@pytest.mark.parametrize(
    "failure",
    [
        Krb5Error(f"Preauthentication failed for {PRINCIPAL}", PREAUTH_FAILED),
        Krb5Error(f"Client {PRINCIPAL} not found in Kerberos database", PRINCIPAL_UNKNOWN),
        OSError(f"No such file or directory: {KEYTAB}"),  # the keytab cannot be used
        Krb5Error("Server not found in Kerberos database", SERVICE_UNKNOWN),
    ],
)
def test_a_negotiation_the_kdc_refuses_closes_like_a_refused_account(monkeypatch, caplog, failure):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    fake = install(monkeypatch)
    fake.fail = failure
    seen: list[str] = []

    (error,) = calls(build(challenge_counting(seen), ATLAS_URL=f"{ATLAS},{OTHER}"))

    # A refusal, as a 401: the core keeps the source closed for MCP_CONTEXT_REFUSED_RETRY_SECONDS,
    # so a stale keytab is not tried again every few seconds against the KDC.
    assert isinstance(error, ProviderRefused)
    assert seen == ["atlas-a"]  # no other address, the same credentials would fail there
    assert not caplog.records
    text = repr(error) + repr(error.__cause__) + repr(error.__context__)
    assert KEYTAB not in text and PRINCIPAL not in text


@pytest.mark.parametrize(
    "failure",
    [
        Krb5Error("Cannot contact any KDC for realm", KDC_UNREACH),
        Krb5Error("Cannot find KDC for realm", REALM_UNKNOWN),
        Krb5Error("Cannot resolve network address for KDC in realm", REALM_CANT_RESOLVE),
        SpnegoError(GSSError(KDC_UNREACH & 0xFFFFFFFF)),
        SpnegoError(GSSError(REALM_UNKNOWN & 0xFFFFFFFF)),
    ],
)
def test_a_kdc_that_cannot_be_reached_is_an_outage(monkeypatch, caplog, failure):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    fake = install(monkeypatch)
    fake.fail = failure
    seen: list[str] = []

    (error,) = calls(build(challenge_counting(seen), ATLAS_URL=f"{ATLAS},{OTHER}"))

    assert isinstance(error, ProviderUnavailable)
    assert seen == ["atlas-a"]
    assert [r.getMessage() for r in caplog.records] == ["atlas unreachable"]


def test_the_check_says_a_refused_negotiation_is_refused(monkeypatch):
    fake = install(monkeypatch)
    fake.fail = Krb5Error("Preauthentication failed", PREAUTH_FAILED)
    p = build(challenge_counting([]))
    lines = p.check_lines()
    asyncio.run(p.aclose())
    assert lines[-1] == "UNREACHABLE: atlas refused the account"


def test_the_keytab_must_be_readable_at_start(monkeypatch, tmp_path):
    install(monkeypatch)
    missing = str(tmp_path / "service-keytab" / "gone.keytab")
    for path in (missing, str(tmp_path)):  # a missing file, a directory
        with pytest.raises(ValueError, match="ATLAS_KEYTAB: the file cannot be read") as raised:
            read_settings({**KERBEROS, "ATLAS_KEYTAB": path})
        assert path not in str(raised.value) and "service-keytab" not in str(raised.value)


def test_a_rotated_keytab_changes_the_stamp(monkeypatch, keytab_file):
    install(monkeypatch)
    p = build(negotiating(FakeAtlas("atlas-2.4")))
    before = p.credentials_stamp()
    keytab_file.write_bytes(b"\x05\x02 another keytab, longer")
    after = p.credentials_stamp()
    keytab_file.unlink()  # a keytab removed while the server runs changes the stamp too
    gone = p.credentials_stamp()
    asyncio.run(p.aclose())
    assert before and after != before and gone not in (before, after)
    assert KEYTAB not in before + after and "keytab" not in (before + after).lower()


def test_the_keytab_path_is_never_printed(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger="httpx")
    install(monkeypatch)
    atlas = FakeAtlas("atlas-2.4")
    p = build(negotiating(atlas))
    lines = p.check_lines()
    authentication = f"kerberos, principal {PRINCIPAL}, service HTTP@<host of each address>"
    assert f"atlas: authentication: {authentication}" in lines
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


async def _beside(p: AtlasProvider, deadline_seconds: float, count: int = 1):
    """``count`` calls at once on the loop, and a task beside them that ticks every 20 ms;
    returns the calls' results, how long they took and the longest gap between two ticks."""
    gaps: list[float] = []
    done = asyncio.Event()

    async def tick() -> None:
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    async def one() -> object:
        try:
            return await p.describe([INVOICES], deadline=time.monotonic() + deadline_seconds)
        except Exception as exc:  # noqa: BLE001 - the test looks at the error
            return exc

    ticker = asyncio.create_task(tick())
    start = time.monotonic()
    try:
        results = await asyncio.gather(*(one() for _ in range(count)))
        elapsed = time.monotonic() - start
    finally:
        done.set()
        await ticker
        await p.aclose()
    return results, elapsed, max(gaps)


def slow_kdc(monkeypatch, seconds: float, late: Exception | None = None):
    fake = install(monkeypatch)
    fake.step_seconds = seconds
    fake.late = late
    return fake


def test_a_slow_kdc_holds_neither_the_loop_nor_the_call_past_its_deadline(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    fake = slow_kdc(monkeypatch, 3.0)
    p = build(negotiating(FakeAtlas("atlas-2.4")), ATLAS_TABLE_TYPES="hive_table")
    try:
        (error,), elapsed, gap = asyncio.run(_beside(p, 1.0))
    finally:
        fake.release.set()
    # The deadline cuts the negotiation, and the loop serves the other task meanwhile.
    assert elapsed < 1.0 + 0.5, f"the call took {elapsed:.2f} s"
    assert gap < 0.5, f"the loop stalled {gap:.2f} s"
    assert isinstance(error, ProviderUnavailable)
    assert len(fake.contexts) == 1
    # The negotiation ran on a thread of the provider's own, never on the loop's.
    assert fake.threads and all(name.startswith("atlas-kerberos") for name in fake.threads)


def test_a_kdc_found_unreachable_after_the_deadline_leaves_nothing_behind(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger="httpx")
    unreachable = Krb5Error("Cannot contact any KDC for realm", KDC_UNREACH)
    fake = slow_kdc(monkeypatch, 0.6, late=unreachable)
    p = build(negotiating(FakeAtlas("atlas-2.4")), ATLAS_TABLE_TYPES="hive_table")

    async def run():
        results = await _beside(p, 0.2)
        await asyncio.sleep(0.8)  # the negotiation ends, and fails, after the call
        return results

    (error,), elapsed, _ = asyncio.run(run())
    # Cut at its deadline: unavailable, and the late failure is dropped with the call.
    assert elapsed < 0.2 + 0.5, f"the call took {elapsed:.2f} s"
    assert isinstance(error, ProviderUnavailable)
    assert "atlas unreachable" not in caplog.text
    assert "exception was never retrieved" not in caplog.text
    assert PRINCIPAL not in caplog.text and KEYTAB not in caplog.text
    # Within the deadline, the same failure is an outage, as before.
    fake = slow_kdc(monkeypatch, 0.05, late=unreachable)
    (error,) = calls(build(negotiating(FakeAtlas("atlas-2.4")), ATLAS_TABLE_TYPES="hive_table"))
    assert isinstance(error, ProviderUnavailable) and len(fake.contexts) == 1


def test_negotiations_at_once_are_bounded_by_the_concurrent_requests(monkeypatch):
    fake = slow_kdc(monkeypatch, 3.0)
    p = build(
        negotiating(FakeAtlas("atlas-2.4")),
        ATLAS_TABLE_TYPES="hive_table",
        ATLAS_MAX_CONCURRENT_REQUESTS="1",
    )
    try:
        results, elapsed, gap = asyncio.run(_beside(p, 0.5, count=3))
    finally:
        fake.release.set()
    assert elapsed < 0.5 + 0.5, f"the calls took {elapsed:.2f} s"
    assert gap < 0.5, f"the loop stalled {gap:.2f} s"
    assert all(isinstance(r, ProviderUnavailable) for r in results)
    # One negotiation ran; the two that waited for a thread were dropped at their deadline.
    assert len(fake.contexts) == 1


def _chain(p: AtlasProvider, retry_after: float = 30.0) -> tuple[ContextChain, ContextLoop]:
    """The core's chain over the provider alone, on a loop of its own."""
    settings = ContextSettings(
        timeout_seconds=5.0,
        reserve_seconds=0.0,
        max_concurrency=8,
        retry_after_seconds=retry_after,
        refused_retry_seconds=60.0,
        batch=50,
        ttl_seconds=60.0,
        cache_bytes=1_000_000,
        terms_cache_bytes=1_000_000,
    )
    loop = ContextLoop()
    loop.start()
    return ContextChain([p], settings, loop=loop), loop


def test_full_pool_of_stale_negotiations_is_unreachable_at_once(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="httpx")
    fake = slow_kdc(monkeypatch, 10.0)  # a hung KDC: no step answers until the test ends
    p = build(
        negotiating(FakeAtlas("atlas-2.4")),
        ATLAS_TABLE_TYPES="hive_table",
        ATLAS_MAX_CONCURRENT_REQUESTS="2",
    )
    chain, loop = _chain(p)

    def gather(seconds: float):
        start = time.monotonic()
        got = loop.submit(chain._gather((INVOICES,), start + seconds), timeout=5)
        return got.statuses[p.name], time.monotonic() - start

    try:
        # Two calls cut at their deadline: each leaves a negotiation on a thread of the pool,
        # and opens no pause, since the deadline cut it.
        for _ in range(2):
            status, _ = gather(0.3)
            assert status == UNAVAILABLE
        assert p.name not in chain._breakers
        assert len(fake.contexts) == 2
        # Every thread holds a negotiation whose call is gone: the next call does not wait for
        # the KDC, it is unavailable at once, well within its deadline, and the chain opens the
        # retry pause.
        status, elapsed = gather(2.0)
        assert status == UNAVAILABLE
        assert elapsed < 0.2, f"the call took {elapsed:.2f} s"
        assert chain._breakers[p.name].status == UNAVAILABLE
        assert len(fake.contexts) == 2
        assert PRINCIPAL not in caplog.text and KEYTAB not in caplog.text
        # During the pause, Atlas is not asked at all.
        status, elapsed = gather(2.0)
        assert status == UNAVAILABLE and elapsed < 0.2
        assert len(fake.contexts) == 2
    finally:
        fake.release.set()
        chain.close()
        loop.close()


def test_kdc_wait_does_not_move_first_address(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger="httpx")
    fake = slow_kdc(monkeypatch, 3.0)
    p = build(
        negotiating(FakeAtlas("atlas-2.4")),
        ATLAS_URL=f"{ATLAS},{OTHER}",
        ATLAS_TABLE_TYPES="hive_table",
    )
    try:
        (error,), elapsed, _ = asyncio.run(_beside(p, 0.5))
    finally:
        fake.release.set()
    assert isinstance(error, ProviderUnavailable)
    assert elapsed < 0.5 + 0.5, f"the call took {elapsed:.2f} s"
    # The address answered its challenge at once; the wait was the KDC's, not the address's.
    assert "did not answer in time" not in caplog.text
    assert p._client._first == 0
    assert [c["hostname"] for c in fake.contexts] == ["atlas-a"]


def test_slow_but_live_kdc_is_not_an_outage(monkeypatch):
    fake = slow_kdc(monkeypatch, 10.0)
    # The first negotiation hangs; the next ones answer, slowly.
    fake.delays[2] = 0.3
    p = build(
        negotiating(FakeAtlas("atlas-2.4")),
        ATLAS_TABLE_TYPES="hive_table",
        ATLAS_MAX_CONCURRENT_REQUESTS="2",
    )
    chain, loop = _chain(p)
    try:
        start = time.monotonic()
        got = loop.submit(chain._gather((INVOICES,), start + 0.2), timeout=5)
        assert got.statuses[p.name] == UNAVAILABLE
        # One thread holds a negotiation whose call is gone, one is free: the next call waits
        # for the KDC, as 0.8.0 did, and reads Atlas.
        start = time.monotonic()
        got = loop.submit(chain._gather((INVOICES,), start + 3.0), timeout=5)
        elapsed = time.monotonic() - start
        assert got.statuses[p.name] == OK
        assert INVOICES in got.tables
        assert elapsed >= 0.3
        assert p.name not in chain._breakers
        assert len(fake.contexts) == 2
    finally:
        fake.release.set()
        chain.close()
        loop.close()


def _until(condition, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > end:
            return False
        time.sleep(0.01)
    return True


def test_the_kdc_pause_ends_once_the_kdc_answers_again(monkeypatch):
    fake = slow_kdc(monkeypatch, 10.0)
    p = build(
        negotiating(FakeAtlas("atlas-2.4")),
        ATLAS_TABLE_TYPES="hive_table",
        ATLAS_MAX_CONCURRENT_REQUESTS="2",
    )
    chain, loop = _chain(p, retry_after=0.3)

    def gather(seconds: float):
        start = time.monotonic()
        got = loop.submit(chain._gather((INVOICES,), start + seconds), timeout=5)
        return got.statuses[p.name], time.monotonic() - start

    try:
        for _ in range(2):
            assert gather(0.3)[0] == UNAVAILABLE
        status, elapsed = gather(2.0)
        assert status == UNAVAILABLE and elapsed < 0.2, f"the call took {elapsed:.2f} s"
        opened = time.monotonic()
        # The KDC answers again: the threads it held are free, and no longer counted.
        fake.release.set()
        assert _until(lambda: not p._client._kerberos._stale, 3.0), "the threads stay counted"
        time.sleep(max(0.0, opened + 0.3 + 0.05 - time.monotonic()))  # the pause is over
        status, _ = gather(3.0)
        assert status == OK
        assert len(fake.contexts) == 3
    finally:
        fake.release.set()
        chain.close()
        loop.close()


def test_a_pool_full_of_live_negotiations_queues_the_next_one(monkeypatch):
    # A slow KDC that answers: every negotiation takes 0.3 s, and every call is in its deadline.
    fake = slow_kdc(monkeypatch, 0.3)
    p = build(
        negotiating(FakeAtlas("atlas-2.4")),
        ATLAS_TABLE_TYPES="hive_table",
        ATLAS_MAX_CONCURRENT_REQUESTS="2",
    )
    chain, loop = _chain(p)

    async def three() -> list:
        async def one():
            try:
                return await p.describe([INVOICES], deadline=time.monotonic() + 3.0)
            except Exception as exc:  # noqa: BLE001 - the test looks at the error
                return exc

        return await asyncio.gather(one(), one(), one())

    try:
        # Both threads are busy when the third call negotiates: it waits for one, as 0.8.0 did.
        results = loop.submit(three(), timeout=5)
        assert all(isinstance(r, dict) and INVOICES in r for r in results), results
        assert len(fake.contexts) == 3
        status = loop.submit(chain._gather((INVOICES,), time.monotonic() + 3.0), timeout=5)
        assert status.statuses[p.name] == OK
        assert p.name not in chain._breakers
    finally:
        fake.release.set()
        chain.close()
        loop.close()


def _slow_after_token(handler, seconds: float, host: str | None = None):
    """``handler``, with the authenticated requests (to ``host`` alone when set) answered
    after ``seconds``."""

    async def slow(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Negotiate ") and host in (None, request.url.host):
            await asyncio.sleep(seconds)
        return handler(request)

    return slow


def test_the_challenge_counts_as_an_answer_of_the_address(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="httpx")
    # The address never answered before; the KDC takes most of the deadline, and Atlas is slow
    # after the token: the deadline cuts a short wait on an address that answered its challenge.
    fake = slow_kdc(monkeypatch, 0.4)
    p = build(
        _slow_after_token(negotiating(FakeAtlas("atlas-2.4")), 1.0),
        ATLAS_URL=f"{ATLAS},{OTHER}",
        ATLAS_TABLE_TYPES="hive_table",
    )
    try:
        (error,), _, _ = asyncio.run(_beside(p, 0.5))
    finally:
        fake.release.set()
    assert isinstance(error, ProviderUnavailable)
    assert len(fake.contexts) == 1
    assert "did not answer in time" not in caplog.text
    assert p._client._first == 0


def test_an_address_hung_after_a_fast_negotiation_is_left(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="httpx")
    install(monkeypatch)  # a fast KDC
    handler = negotiating(FakeAtlas("atlas-2.4"))
    p = build(
        _slow_after_token(handler, 3.0, host="atlas-a"),
        ATLAS_URL=f"{ATLAS},{OTHER}",
        ATLAS_TABLE_TYPES="hive_table",
        ATLAS_CONNECT_TIMEOUT_SECONDS="0.2",
    )

    async def run():
        try:
            try:
                first: object = await p.describe([INVOICES], deadline=time.monotonic() + 0.5)
            except ProviderUnavailable as exc:
                first = exc
            asked = len(handler.seen)
            second = await p.describe([INVOICES], deadline=time.monotonic() + 3.0)
            return first, second, [r.url.host for r in handler.seen[asked:]]
        finally:
            await p.aclose()

    first, second, hosts = asyncio.run(run())
    # The first address answered its challenge, then hung on the authenticated request: that
    # wait is its own, so it is named once and left.
    assert isinstance(first, ProviderUnavailable)
    assert "atlas address 1 of 2 in ATLAS_URL did not answer in time" in caplog.text
    assert INVOICES in second
    assert hosts and hosts[0] == "atlas-b"


def test_a_synchronous_client_negotiates_too(monkeypatch):
    install(monkeypatch)
    auths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auths.append(request.headers.get("authorization", ""))
        if not auths[-1] and request.url.path != "/open":
            return httpx.Response(401, headers=CHALLENGE)
        return httpx.Response(200, json={})

    auth = SpnegoAuth(PRINCIPAL, KEYTAB, "")
    try:
        with httpx.Client(transport=httpx.MockTransport(handler), auth=auth) as client:
            assert client.get(f"{ATLAS}/api/atlas/admin/version").status_code == 200
            assert client.get(f"{ATLAS}/open").status_code == 200  # no challenge, no token
    finally:
        auth.close()
    assert auths == ["", "Negotiate " + base64.b64encode(b"token-1").decode(), ""]


def test_a_challenge_body_is_never_read(monkeypatch):
    install(monkeypatch)
    atlas = FakeAtlas("atlas-2.4")
    bodies: list[Counted] = []
    # Four times the cap in plain bytes, and a stacked coding no client should decode.
    payloads = {"identity": b" " * (4 * 1_048_576), "gzip, gzip": b"\x1f\x8b not gzip"}

    for coding, payload in payloads.items():

        def handler(request: httpx.Request, coding=coding, payload=payload) -> httpx.Response:
            if not request.headers.get("authorization"):
                bodies.append(Counted(payload))
                headers = {**CHALLENGE, "content-encoding": coding}
                return httpx.Response(401, headers=headers, stream=bodies[-1])
            return atlas(request)

        p = build(handler, ATLAS_TABLE_TYPES="hive_table", ATLAS_MAX_RESPONSE_BYTES="1048576")
        (found,) = calls(p)
        assert INVOICES in found, coding
    assert len(bodies) == 2
    assert all(body.sent == 0 and body.closed for body in bodies), [b.sent for b in bodies]


def test_an_asynchronous_httpx_client_negotiates_too(monkeypatch):
    install(monkeypatch)
    auths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auths.append(request.headers.get("authorization", ""))
        if not auths[-1] and request.url.path != "/open":
            return httpx.Response(401, headers=CHALLENGE)
        return httpx.Response(200, json={})

    auth = SpnegoAuth(PRINCIPAL, KEYTAB, "")

    async def run() -> list[int]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, auth=auth) as client:
            first = await client.get(f"{ATLAS}/api/atlas/admin/version")
            second = await client.get(f"{ATLAS}/open")  # no challenge, no token
            return [first.status_code, second.status_code]

    try:
        assert asyncio.run(run()) == [200, 200]
    finally:
        auth.close()
    assert auths == ["", "Negotiate " + base64.b64encode(b"token-1").decode(), ""]
