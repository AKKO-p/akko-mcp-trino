"""akko_mcp_trino.auth.JwksKeys: the issuer's keys read with a timeout, kept for a lifetime,
read again at most once per interval whatever the tokens name, and never read on the event
loop.

The last test serves the guard with an identity provider that accepts the connection and
never answers, sends tokens naming unknown keys, and checks that a request without a token is
still answered at once."""

import socket
import threading
import time

import httpx
import jwt as pyjwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from akko_mcp_trino import auth
from akko_mcp_trino.config import Config
from akko_mcp_trino.metrics import Metrics
from akko_mcp_trino.middleware import AuthIdentityMiddleware

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ISS = "https://idp.example/realms/r"
_AUD = "mcp"


def _jwk(key, kid, **extra):
    jwk = pyjwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, **extra}


def _token(kid, key=_KEY):
    claims = {"iss": _ISS, "aud": _AUD, "exp": int(time.time()) + 300, "sub": "alice"}
    return pyjwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _Issuer:
    """A JWKS endpoint double: serves ``keys``, or raises while ``down``; counts reads."""

    def __init__(self, *keys):
        self.keys = list(keys)
        self.down = False
        self.reads = 0

    def __call__(self):
        self.reads += 1
        if self.down:
            raise ConnectionError("unreachable")
        return {"keys": self.keys}


def _keys(issuer, clock, seen=None):
    return auth.JwksKeys(
        issuer,
        cache_seconds=300,
        min_refresh_seconds=30,
        on_fetch=None if seen is None else seen.append,
        clock=clock,
    )


def test_a_known_key_is_read_once_and_kept():
    issuer, clock, seen = _Issuer(_jwk(_KEY, "k1")), _Clock(), []
    keys = _keys(issuer, clock, seen)
    for _ in range(5):
        assert keys.get_signing_key_from_jwt(_token("k1")).key_id == "k1"
    assert (issuer.reads, seen) == (1, [True])


def test_an_unknown_kid_is_refused_without_a_read_within_the_interval():
    issuer, clock = _Issuer(_jwk(_KEY, "k1")), _Clock()
    keys = _keys(issuer, clock)
    keys.get_signing_key_from_jwt(_token("k1"))
    for n in range(50):
        with pytest.raises(pyjwt.PyJWKClientError):
            keys.get_signing_key_from_jwt(_token(f"forged-{n}"))
    assert issuer.reads == 1


def test_a_rotated_key_is_found_once_the_interval_has_passed():
    issuer, clock = _Issuer(_jwk(_KEY, "k1")), _Clock()
    keys = _keys(issuer, clock)
    keys.get_signing_key_from_jwt(_token("k1"))
    issuer.keys.append(_jwk(_OTHER, "k2"))
    with pytest.raises(pyjwt.PyJWKClientError):
        keys.get_signing_key_from_jwt(_token("k2", _OTHER))
    clock.now += 30
    assert keys.get_signing_key_from_jwt(_token("k2", _OTHER)).key_id == "k2"
    assert issuer.reads == 2


def test_a_failed_read_is_not_retried_before_the_interval_and_is_heard():
    issuer, clock, seen = _Issuer(_jwk(_KEY, "k1")), _Clock(), []
    issuer.down = True
    keys = _keys(issuer, clock, seen)
    for _ in range(20):
        with pytest.raises(pyjwt.PyJWKClientError):
            keys.get_signing_key_from_jwt(_token("k1"))
    assert (issuer.reads, seen) == (1, [False])
    issuer.down = False
    clock.now += 30
    assert keys.get_signing_key_from_jwt(_token("k1")).key_id == "k1"
    assert seen == [False, True]


def test_a_failed_read_keeps_the_keys_held():
    issuer, clock = _Issuer(_jwk(_KEY, "k1")), _Clock()
    keys = _keys(issuer, clock)
    keys.get_signing_key_from_jwt(_token("k1"))
    issuer.down = True
    clock.now += 301  # past the lifetime: the keys are read again, and that read fails
    assert keys.get_signing_key_from_jwt(_token("k1")).key_id == "k1"
    assert issuer.reads == 2
    assert keys.get_signing_key_from_jwt(_token("k1")).key_id == "k1"
    assert issuer.reads == 2


def test_keys_past_their_lifetime_are_read_again():
    issuer, clock = _Issuer(_jwk(_KEY, "k1")), _Clock()
    keys = _keys(issuer, clock)
    keys.get_signing_key_from_jwt(_token("k1"))
    clock.now += 299
    keys.get_signing_key_from_jwt(_token("k1"))
    assert issuer.reads == 1
    clock.now += 1
    keys.get_signing_key_from_jwt(_token("k1"))
    assert issuer.reads == 2


@pytest.mark.parametrize(
    "document",
    [
        {"keys": []},
        {"keys": [{**_jwk(_KEY, "k1"), "use": "enc"}]},
        {"keys": [{k: v for k, v in _jwk(_KEY, "k1").items() if k != "kid"}]},
        "not a JWKS",
    ],
)
def test_a_jwks_without_a_signing_key_with_a_kid_is_a_failed_read(document):
    seen = []
    keys = auth.JwksKeys(
        lambda: document, cache_seconds=300, min_refresh_seconds=30, on_fetch=seen.append
    )
    with pytest.raises(pyjwt.PyJWKClientError):
        keys.get_signing_key_from_jwt(_token("k1"))
    assert seen == [False]


def test_a_token_without_a_kid_is_refused_without_a_read():
    issuer = _Issuer(_jwk(_KEY, "k1"))
    keys = _keys(issuer, _Clock())
    token = pyjwt.encode({"sub": "a"}, _KEY, algorithm="RS256")
    with pytest.raises(pyjwt.PyJWKClientError, match="names no key"):
        keys.get_signing_key_from_jwt(token)
    assert issuer.reads == 0


def test_nobody_waits_for_a_read_under_way():
    """While one thread reads, another finds the read under way and is answered at once."""
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return {"keys": [_jwk(_KEY, "k1")]}

    keys = auth.JwksKeys(slow, cache_seconds=300, min_refresh_seconds=30)
    reader = threading.Thread(target=keys.get_signing_key_from_jwt, args=(_token("k1"),))
    reader.start()
    assert started.wait(5)
    begin = time.monotonic()
    with pytest.raises(pyjwt.PyJWKClientError):
        keys.get_signing_key_from_jwt(_token("k1"))
    assert time.monotonic() - begin < 0.1
    release.set()
    reader.join(5)
    assert keys.get_signing_key_from_jwt(_token("k1")).key_id == "k1"


def test_a_read_finished_by_another_thread_is_not_repeated():
    """A thread that takes the lock after another's read does not read again."""
    issuer = _Issuer(_jwk(_KEY, "k1"))
    keys = _keys(issuer, _Clock())
    keys._attempted_at = keys._clock()  # another thread has just read
    keys._read()
    assert issuer.reads == 0


def test_a_read_under_way_is_not_joined():
    """The lock held by another reader: the caller returns without reading or waiting."""
    issuer = _Issuer(_jwk(_KEY, "k1"))
    keys = _keys(issuer, _Clock())
    keys._reading.acquire()
    try:
        keys._read()
    finally:
        keys._reading.release()
    assert issuer.reads == 0


def test_build_auth_reads_the_jwks_settings_and_reports_each_read():
    config = Config.from_env()
    config = type(config)(
        **{
            **config.__dict__,
            "auth_enabled": True,
            "jwks_url": "https://idp.example/jwks",
            "oidc_issuer": _ISS,
            "oidc_audience": _AUD,
            "jwks_timeout_seconds": 2.0,
            "jwks_cache_seconds": 120.0,
            "jwks_min_refresh_seconds": 15.0,
        }
    )
    metrics = Metrics()
    provider = auth.build_auth(config, on_fetch=metrics.jwks_fetched)
    keys = provider._client
    assert isinstance(keys, auth.JwksKeys)
    assert (keys._cache, keys._min_refresh) == (120.0, 15.0)
    assert keys._fetch.__self__.timeout == 2.0
    assert keys._fetch.__self__.jwk_set_cache is None  # this class is the cache
    keys._on_fetch(False)
    body = metrics.render()[0].decode()
    assert 'mcp_trino_jwks_fetches_total{result="failed"} 1.0' in body
    assert "mcp_trino_jwks_up 0.0" in body
    metrics.jwks_fetched(True)
    assert "mcp_trino_jwks_up 1.0" in metrics.render()[0].decode()


def test_a_provider_built_without_a_client_reads_with_the_default_bounds():
    provider = auth.JwksJwtAuth("https://idp.example/jwks", _ISS, _AUD)
    keys = provider._client
    assert (keys._cache, keys._min_refresh) == (300.0, 30.0)
    assert keys._fetch.__self__.timeout == 5.0


def test_a_valid_token_is_verified_end_to_end_through_the_keys():
    issuer = _Issuer(_jwk(_KEY, "k1", alg="RS256"))
    provider = auth.JwksJwtAuth("unused", _ISS, _AUD, jwks_client=_keys(issuer, _Clock()))
    principal = provider.verify({"authorization": f"Bearer {_token('k1')}"})
    assert principal is not None and principal.subject == "alice"


@pytest.fixture
def silent_issuer():
    """A JWKS URL whose server accepts the connection and never answers."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    yield f"http://127.0.0.1:{server.getsockname()[1]}/jwks"
    server.close()


def test_forged_tokens_never_stall_a_request_without_a_token(silent_issuer):
    """Verification runs in a worker thread and one read at most runs per interval, so with a
    JWKS that never answers, a request without a token is answered at once while tokens
    naming unknown keys are being verified."""
    seen: list[bool] = []
    keys = auth.JwksKeys.from_url(
        silent_issuer,
        timeout_seconds=1.0,
        cache_seconds=300,
        min_refresh_seconds=30,
        on_fetch=seen.append,
    )
    provider = auth.JwksJwtAuth(silent_issuer, _ISS, _AUD, jwks_client=keys)

    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok)])
    app.add_middleware(AuthIdentityMiddleware, auth_provider=provider, require_auth=True)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.01)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        with httpx.Client(timeout=10) as client:
            assert client.get(url).status_code == 401  # warm: the server answers
            forged = [
                threading.Thread(
                    target=client.get,
                    args=(url,),
                    kwargs={"headers": {"Authorization": f"Bearer {_token(f'f{n}')}"}},
                )
                for n in range(4)
            ]
            for thread in forged:
                thread.start()
            time.sleep(0.2)  # the forged tokens are in, the read of the JWKS under way
            begin = time.monotonic()
            plain = client.get(url)
            elapsed = time.monotonic() - begin
            reading = any(thread.is_alive() for thread in forged)
            for thread in forged:
                thread.join(10)
    finally:
        server.should_exit = True
    assert plain.status_code == 401 and reading
    assert elapsed < 0.1, f"a request without a token waited {elapsed:.3f} s"
    assert seen == [False]  # one read for four forged tokens, and it failed
