"""HTTP to Atlas: addresses in turn, redirects, errors, headers, session and secrets."""

import asyncio
import base64
import logging
import ssl
import time

import certifi
import httpx
import pytest
from akko_mcp_trino.context import ProviderRefused, ProviderUnavailable, TableRef
from atlas_replay import ATLAS, FakeAtlas, transport

import akko_mcp_trino_atlas
from akko_mcp_trino_atlas import AtlasProvider
from akko_mcp_trino_atlas.client import SESSION_COOKIE, USER_AGENT, client_options
from akko_mcp_trino_atlas.settings import read_settings

OTHER = "http://atlas-b:21000"
PASSWORD = 'reader-"pass\\é'
BASE = {
    "ATLAS_URL": f"{ATLAS},{OTHER}",
    "ATLAS_USER": "reader",
    "ATLAS_PASSWORD": PASSWORD,
    "ATLAS_CLUSTERS": '{"lake": "primary"}',
}
INVOICES = TableRef("lake", "ledger", "invoices")


def build(handler, **env: str) -> AtlasProvider:
    return AtlasProvider(read_settings({**BASE, **env}), transport=transport(handler))


async def _calls(p: AtlasProvider, count: int, seconds: float):
    results = []
    try:
        for _ in range(count):
            try:
                results.append(await p.describe([INVOICES], deadline=time.monotonic() + seconds))
            except Exception as exc:  # noqa: BLE001 - the test looks at the error
                results.append(exc)
    finally:
        await p.aclose()
    return results


def calls(p: AtlasProvider, count: int = 1, seconds: float = 5.0) -> list:
    return asyncio.run(_calls(p, count, seconds))


def by_host(answers: dict[str, object], atlas: FakeAtlas | None = None):
    """A handler that answers each host as ``answers`` says: a response, an exception, or
    ``"replay"`` for the recorded Atlas 2.4; every request is kept in ``seen``."""
    atlas = atlas or FakeAtlas("atlas-2.4")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = answers[request.url.host]
        if answer == "replay":
            return atlas(request)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, httpx.Response)
        return answer

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def test_a_knox_login_page_is_unavailable_and_a_foreign_redirect_is_refused():
    page = httpx.Response(200, headers={"content-type": "text/html"}, text="<form>login</form>")
    handler = by_host({"atlas-a": page, "atlas-b": "replay"})
    (error,) = calls(build(handler))
    assert isinstance(error, ProviderUnavailable)
    assert len(handler.seen) == 1  # a login page is not an outage: no other address

    sso = httpx.Response(302, headers={"location": "https://sso.example:8443/knoxsso/login"})
    handler = by_host({"atlas-a": sso, "atlas-b": "replay"})
    first, second = calls(build(handler), count=2)
    assert isinstance(first, ProviderRefused) and isinstance(second, ProviderRefused)
    # One request per call, never the SSO host, never the other address: nothing loops.
    assert [r.url.host for r in handler.seen] == ["atlas-a", "atlas-a"]


def test_the_next_address_is_tried_when_the_first_is_down():
    handler = by_host({"atlas-a": httpx.ConnectError("refused"), "atlas-b": "replay"})
    first, second = calls(build(handler), count=2)
    assert INVOICES in first and first == second
    hosts = [r.url.host for r in handler.seen]
    # The first call finds atlas-b; the next calls start there.
    assert hosts[:2] == ["atlas-a", "atlas-b"] and set(hosts[2:]) == {"atlas-b"}

    for down in (httpx.Response(503), httpx.ReadTimeout("slow")):
        handler = by_host({"atlas-a": down, "atlas-b": "replay"})
        (found,) = calls(build(handler))
        assert INVOICES in found


def test_no_address_answering_is_unavailable(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")  # as the server sets it
    handler = by_host({"atlas-a": httpx.Response(500), "atlas-b": httpx.ConnectError("no")})
    (error,) = calls(build(handler))
    assert isinstance(error, ProviderUnavailable)
    assert [r.message for r in caplog.records] == ["atlas unreachable"]


def test_a_passive_redirect_is_followed_only_to_an_address_of_atlas_url():
    atlas = FakeAtlas("atlas-2.4")

    def passive(request: httpx.Request) -> httpx.Response:
        if request.url.host == "atlas-a":
            target = request.url.copy_with(host="atlas-b")
            return httpx.Response(303, headers={"location": str(target)})
        return atlas(request)

    (found,) = calls(build(passive))
    assert INVOICES in found
    assert [r.url.host for r in atlas.requests] == ["atlas-b"]  # found under hive_table

    # Under a path: the redirect must stay under the address, its path included.
    gateway = "https://knox:8443/gateway/cdp/atlas"

    def elsewhere(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "https://knox:8443/gateway/other/x"})

    (error,) = calls(build(elsewhere, ATLAS_URL=gateway))
    assert isinstance(error, ProviderRefused)

    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": request.url.path})

    (error,) = calls(build(loop, ATLAS_URL=ATLAS))
    assert isinstance(error, ProviderUnavailable)

    def to_gateway(request: httpx.Request) -> httpx.Response:
        if "/gateway/cdp/atlas/api/" in request.url.path:
            path = request.url.path.removeprefix("/gateway/cdp/atlas")
            return atlas(httpx.Request("GET", request.url.copy_with(path=path)))
        # Atlas's passive server keeps the query string in the address it redirects to.
        location = gateway + request.url.raw_path.decode()
        return httpx.Response(307, headers={"location": location})

    (found,) = calls(build(to_gateway, ATLAS_URL=f"http://atlas-a:21000,{gateway}"))
    assert INVOICES in found


def test_a_401_or_a_403_is_refused_without_another_address():
    for status in (401, 403):
        handler = by_host({"atlas-a": httpx.Response(status), "atlas-b": "replay"})
        (error,) = calls(build(handler))
        assert isinstance(error, ProviderRefused)
        assert [r.url.host for r in handler.seen] == ["atlas-a"]


def test_an_error_answer_never_reaches_the_exception():
    secret_body = {"error": f"credential {PASSWORD} rejected at {ATLAS}"}
    answers = [
        httpx.Response(500, json=secret_body),
        httpx.Response(429, json=secret_body),
        httpx.Response(302),  # a redirect without a location
        httpx.Response(200, headers={"content-type": "application/json"}, content=b'{"a": [1'),
    ]
    for answer in answers:
        (error,) = calls(build(lambda request, a=answer: a, ATLAS_URL=ATLAS))
        assert isinstance(error, ProviderUnavailable)
        text = repr(error) + repr(error.__cause__) + repr(error.__context__)
        assert PASSWORD not in text and "atlas-a" not in text and "rejected" not in text


def test_only_atlas_instance_not_found_is_nothing(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errorCode": "ATLAS-404-00-009"})

    (found,) = calls(build(missing, ATLAS_URL=ATLAS))
    assert found == {} and not caplog.records

    # Any other 404 or 400 is a wrong path or request (a Knox topology without the role,
    # a wrong prefix in ATLAS_URL): unavailable, never "nothing known".
    others = [
        httpx.Response(404, headers={"content-type": "text/html"}, text="<h1>Not Found</h1>"),
        httpx.Response(404, json={"errorCode": "ATLAS-404-00-001"}),
        httpx.Response(404, json=["ATLAS-404-00-009"]),
        httpx.Response(404, headers={"content-type": "application/json"}, content=b"{"),
        httpx.Response(400, json={"errorCode": "ATLAS-400-00-001"}),
        httpx.Response(400, text="bad request"),
    ]
    for answer in others:
        caplog.clear()
        (error,) = calls(build(lambda request, a=answer: a, ATLAS_URL=ATLAS))
        assert isinstance(error, ProviderUnavailable)
        assert [r.getMessage() for r in caplog.records] == ["atlas unreachable"]


def test_a_default_port_written_or_not_is_the_same_address():
    atlas = FakeAtlas("atlas-2.4")
    gateway = "https://knox/gateway/cdp/atlas"

    def explicit_port(request: httpx.Request) -> httpx.Response:
        if request.url.host == "atlas-a":
            path = "https://knox:443/gateway/cdp/atlas" + request.url.raw_path.decode()
            return httpx.Response(307, headers={"location": path})
        path = request.url.path.removeprefix("/gateway/cdp/atlas")
        return atlas(httpx.Request("GET", request.url.copy_with(path=path)))

    (found,) = calls(build(explicit_port, ATLAS_URL=f"{ATLAS},{gateway}"))
    assert INVOICES in found


def test_a_json_media_type_with_a_suffix_is_json():
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/vnd.atlas+json"}, json={})

    (found,) = calls(build(answer, ATLAS_URL=ATLAS))
    assert found == {}


def test_no_url_reaches_the_log_at_info(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")  # as the server sets it
    down = by_host({"atlas-a": httpx.ConnectError("x"), "atlas-b": httpx.Response(502)})
    refused = by_host({"atlas-a": httpx.Response(401), "atlas-b": "replay"})
    ok = by_host({"atlas-a": "replay", "atlas-b": "replay"})
    for handler in (down, refused, ok):
        calls(build(handler))
    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["atlas unreachable"]
    for forbidden in ("atlas-a", "atlas-b", "21000", "http", "reader", "ledger", "invoices"):
        assert forbidden not in caplog.text


def test_the_deadline_cuts_a_slow_atlas_without_an_outage_line(caplog):
    caplog.set_level(logging.INFO)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json={})

    started = time.monotonic()
    (error,) = calls(build(slow), seconds=0.3)
    assert isinstance(error, ProviderUnavailable)
    assert time.monotonic() - started < 2
    assert "atlas unreachable" not in caplog.text


def test_the_client_ignores_the_environment_and_sends_its_headers(tmp_path):
    env = {**BASE, "ATLAS_CA_BUNDLE": certifi.where(), "ATLAS_PROXY": "http://proxy:3128"}
    settings = read_settings(env)
    options = client_options(settings)
    assert options["trust_env"] is False and options["follow_redirects"] is False
    assert isinstance(options["verify"], ssl.SSLContext)
    assert options["proxy"] == "http://proxy:3128"
    assert options["timeout"] == 5.0
    assert client_options(read_settings(BASE))["proxy"] is None
    assert client_options(read_settings(BASE))["verify"] is True
    assert USER_AGENT == f"akko-mcp-trino-atlas/{akko_mcp_trino_atlas.__version__}"
    # Built for the network, the provider opens its client without sending anything.
    asyncio.run(AtlasProvider(settings).aclose())

    atlas = FakeAtlas("atlas-2.4")

    def session(request: httpx.Request) -> httpx.Response:
        response = atlas(request)
        response.headers["set-cookie"] = f"{SESSION_COOKIE}=s1; Path=/; HttpOnly"
        return response

    calls(build(session, ATLAS_URL=ATLAS), count=2)
    first, *rest = atlas.requests
    token = base64.b64encode(f"reader:{PASSWORD}".encode()).decode()
    for request in atlas.requests:
        assert request.headers["x-xsrf-header"] == ""
        assert request.headers["user-agent"] == USER_AGENT
        assert request.headers["authorization"] == f"Basic {token}"
    assert "cookie" not in first.headers
    assert all(r.headers["cookie"] == f"{SESSION_COOKIE}=s1" for r in rest)


def test_a_refusal_drops_the_session():
    seen: list[httpx.Request] = []
    statuses = iter([200, 401, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = next(statuses)
        headers = {"set-cookie": f"{SESSION_COOKIE}=s1; Path=/"} if status == 200 else {}
        return httpx.Response(status, headers=headers, json={})

    calls(build(handler, ATLAS_URL=ATLAS, ATLAS_TABLE_TYPES="hive_table"), count=3)
    assert ["cookie" in r.headers for r in seen] == [False, True, False]


def test_a_knox_token_is_sent_as_a_bearer_and_kerberos_sends_no_password(tmp_path):
    token = tmp_path / "token"
    token.write_text("knox-token-1\n", encoding="utf-8")
    atlas = FakeAtlas("atlas-2.4")
    env = {"ATLAS_URL": ATLAS, "ATLAS_CLUSTERS": BASE["ATLAS_CLUSTERS"]}
    knox = AtlasProvider(
        read_settings({**env, "ATLAS_KNOX_TOKEN_FILE": str(token)}), transport=transport(atlas)
    )
    calls(knox)
    assert {r.headers["authorization"] for r in atlas.requests} == {"Bearer knox-token-1"}

    atlas.requests.clear()
    kerberos = {**env, "ATLAS_AUTH": "kerberos", "ATLAS_PRINCIPAL": "r", "ATLAS_KEYTAB": "/k"}
    calls(AtlasProvider(read_settings(kerberos), transport=transport(atlas)))
    assert atlas.requests and not any("authorization" in r.headers for r in atlas.requests)


def test_the_password_file_comes_first_and_the_stamp_follows_it(tmp_path):
    secret = tmp_path / "password"
    secret.write_text("from-the-file\n", encoding="utf-8")
    atlas = FakeAtlas("atlas-2.4")
    env = {**BASE, "ATLAS_URL": ATLAS, "ATLAS_PASSWORD_FILE": str(secret)}
    p = AtlasProvider(read_settings(env), transport=transport(atlas))
    before = p.credentials_stamp()
    assert before and "from-the-file" not in before and "from-the-file" not in repr(p.settings)

    async def run():
        await p.describe([INVOICES], deadline=time.monotonic() + 5)
        secret.write_text("rotated-password\n", encoding="utf-8")
        stamp = p.credentials_stamp()
        await p.describe([INVOICES], deadline=time.monotonic() + 5)
        await p.aclose()
        return stamp

    after = asyncio.run(run())
    assert after != before
    used = {base64.b64decode(r.headers["authorization"][6:]).decode() for r in atlas.requests}
    assert used == {"reader:from-the-file", "reader:rotated-password"}
    plain = AtlasProvider(read_settings(BASE), transport=transport(atlas))
    assert plain.credentials_stamp() == ""
    asyncio.run(plain.aclose())


@pytest.mark.parametrize(
    ("setting", "value", "says"),
    [
        ("ATLAS_CA_BUNDLE", "/no/such/bundle.pem", "ATLAS_CA_BUNDLE"),
        ("ATLAS_PROXY", "socks5://proxy:1080", "ATLAS_PROXY"),
        ("ATLAS_PROXY", "proxy:3128", "ATLAS_PROXY"),
    ],
)
def test_network_settings_are_checked_at_start(setting, value, says):
    with pytest.raises(ValueError, match=says) as raised:
        read_settings({**BASE, setting: value})
    assert PASSWORD not in str(raised.value) and value not in str(raised.value)
