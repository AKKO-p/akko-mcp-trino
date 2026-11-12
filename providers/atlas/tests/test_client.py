"""HTTP to Atlas: addresses in turn, redirects, errors, headers, session and secrets."""

import asyncio
import base64
import http.server
import logging
import socket
import ssl
import threading
import time

import certifi
import httpx
import pytest
from akko_mcp_trino.context import ProviderRefused, ProviderUnavailable, TableRef
from atlas_replay import ATLAS, FakeAtlas, transport
from fake_spnego import install

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
    assert options["timeout"] == httpx.Timeout(5.0, connect=1.0)
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


def test_a_knox_token_is_sent_as_a_bearer_and_kerberos_sends_no_password(tmp_path, monkeypatch):
    install(monkeypatch)  # a stand-in for the kerberos extra
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
    keytab = tmp_path / "reader.keytab"
    keytab.write_bytes(b"keytab")
    kerberos = {
        **env,
        "ATLAS_AUTH": "kerberos",
        "ATLAS_PRINCIPAL": "r",
        "ATLAS_KEYTAB": str(keytab),
    }
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


def test_a_silent_first_address_gives_way_to_the_next_within_the_deadline():
    atlas = FakeAtlas("atlas-2.4")
    connects: list[float] = []

    async def silent_first(request: httpx.Request) -> httpx.Response:
        if request.url.host == "atlas-a":
            # A node that drops packets: the connection never opens, httpx gives up after
            # the connect timeout it was given.
            connects.append(request.extensions["timeout"]["connect"])
            await asyncio.sleep(request.extensions["timeout"]["connect"])
            raise httpx.ConnectTimeout("silent")
        return atlas(request)

    started = time.monotonic()
    first, second = calls(build(silent_first, ATLAS_CONNECT_TIMEOUT_SECONDS="0.2"), count=2)
    assert INVOICES in first and first == second
    assert connects == [0.2]  # the next calls start at the address that answered
    assert time.monotonic() - started < 2


def test_an_address_that_refuses_connections_is_not_tried_first_again():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "atlas-a":
            raise httpx.ConnectError("refused")
        return httpx.Response(503)  # an answer, though an outage: b keeps its place

    first, second = calls(build(handler), count=2)
    assert isinstance(first, ProviderUnavailable) and isinstance(second, ProviderUnavailable)
    # The first call tried a, then b; b is tried first from then on.
    assert seen[:2] == ["atlas-a", "atlas-b"] and seen[2] == "atlas-b"


def test_a_passive_redirect_makes_the_active_server_the_first_address():
    atlas = FakeAtlas("atlas-2.4")
    seen: list[str] = []

    def passive(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "atlas-a":
            target = request.url.copy_with(host="atlas-b")
            return httpx.Response(303, headers={"location": str(target)})
        return atlas(request)

    first, second = calls(build(passive, ATLAS_TABLE_TYPES="hive_table"), count=2)
    assert INVOICES in first and first == second
    assert seen == ["atlas-a", "atlas-b", "atlas-b"]


def objects_refused() -> httpx.Response:
    """Atlas's answer when the account may not read one of the objects asked.

    Constructed in the format of the recorded error answers, with the code of Atlas's
    UNAUTHORIZED_ACCESS. To be replaced by the recording of that answer from Atlas.
    """
    return httpx.Response(
        403,
        json={
            "errorCode": "ATLAS-403-00-001",
            "errorMessage": "reader is not authorized to perform read entity",
        },
    )


def test_an_object_atlas_refuses_to_the_account_is_nothing_and_said_once(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    seen: list[str] = []

    def refuses_objects(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return objects_refused()

    first, second = calls(build(refuses_objects, ATLAS_URL=ATLAS), count=2)
    # Nothing for the batch, and the account stays open: the second call reads again.
    assert first == second == {}
    assert len(seen) == 4  # both types, in each call
    messages = [r.getMessage() for r in caplog.records]
    refused = (
        "atlas refused some objects to the account; an account with no read grant serves nothing"
    )
    assert messages == [refused] * 2  # once a call
    assert "reader" not in caplog.text and "invoices" not in caplog.text


def test_any_other_403_is_a_refusal_of_the_account():
    others = [
        httpx.Response(403, headers={"content-type": "text/html"}, text="<h1>Forbidden</h1>"),
        httpx.Response(403, json={"errorCode": "ATLAS-403-00-002"}),
        httpx.Response(403, json=["ATLAS-403-00-001"]),
    ]
    for answer in others:
        (error,) = calls(build(lambda request, a=answer: a, ATLAS_URL=ATLAS))
        assert isinstance(error, ProviderRefused)


def test_a_body_that_cannot_be_decoded_is_an_outage(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")

    def garbled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            content=b"not gzip at all",
        )

    (error,) = calls(build(garbled, ATLAS_URL=ATLAS))
    assert isinstance(error, ProviderUnavailable)
    assert [r.getMessage() for r in caplog.records] == ["atlas unreachable"]


# An address that accepts the connection and then never answers, and Atlas servers on real
# sockets: what a frozen server does to the client is only seen on the network, never through
# a transport that stands in for httpx.
LEFT = "did not answer in time"


class _Hung:
    """A listener that accepts every connection, keeps it open and never reads from it."""

    def __init__(self) -> None:
        self._server = socket.create_server(("127.0.0.1", 0), backlog=128)
        self.url = f"http://127.0.0.1:{self._server.getsockname()[1]}"
        self.accepted: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            self.accepted.append(connection)

    def close(self) -> None:
        self._server.close()
        for connection in self.accepted:
            connection.close()


class _Server(http.server.ThreadingHTTPServer):
    # The default backlog, 5, drops the connections of a burst: they would time out connecting.
    request_queue_size = 128


class _Atlas:
    """The recorded Atlas 2.4 on a real socket, counting the requests it answers;
    ``answer`` may answer a path first (and may sleep before it does)."""

    def __init__(self, answer=None) -> None:
        atlas, served = FakeAtlas("atlas-2.4"), self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
                request = httpx.Request("GET", served.url + self.path)
                answer = (served.answer and served.answer(request)) or atlas(request)
                served.requests += 1
                self.send_response(answer.status_code)
                for name in ("content-type", "location"):
                    if name in answer.headers:
                        self.send_header(name, answer.headers[name])
                self.send_header("content-length", str(len(answer.content)))
                self.end_headers()
                self.wfile.write(answer.content)

            def log_message(self, *args: object) -> None:
                pass

        self.requests = 0
        self.answer = answer
        self._server = _Server(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def network():
    """A hung address and two Atlas servers, ``ATLAS_URL`` in that order."""
    hung, a, b = _Hung(), _Atlas(), _Atlas()
    yield hung, a, b
    for server in (hung, a, b):
        server.close()


def on_network(*urls: str, **env: str) -> AtlasProvider:
    return AtlasProvider(read_settings({**BASE, "ATLAS_URL": ",".join(urls), **env}))


def left_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if LEFT in r.getMessage()]


def test_a_hung_first_address_costs_one_call_with_the_default_timeouts(network, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")  # as the server sets it
    hung, a, b = network
    # ATLAS_TIMEOUT_SECONDS and the deadline both at 5 s: the deadline cuts the wait on the
    # hung address, so no address answers the first call.
    first, *later = calls(on_network(hung.url, a.url, b.url), count=4, seconds=5.0)
    assert isinstance(first, ProviderUnavailable)
    assert all(INVOICES in found for found in later)
    assert a.requests > 0 and b.requests == 0
    assert len(hung.accepted) == 1  # left after the first call
    assert left_lines(caplog) == ["atlas address 1 of 3 in ATLAS_URL did not answer in time"]
    # The position, never the address, a user or a secret.
    for forbidden in (hung.url, "127.0.0.1", "reader", PASSWORD):
        assert forbidden not in caplog.text


def test_a_read_timeout_below_the_deadline_lets_the_next_address_answer(network, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    hung, a, b = network
    p = on_network(
        hung.url, a.url, b.url, ATLAS_TIMEOUT_SECONDS="0.5", ATLAS_CONNECT_TIMEOUT_SECONDS="0.2"
    )
    found = calls(p, count=3, seconds=5.0)
    assert all(INVOICES in each for each in found)  # the first call included
    assert a.requests > 0 and b.requests == 0
    assert len(hung.accepted) == 1
    assert len(left_lines(caplog)) == 1


@pytest.mark.parametrize(
    ("timeout", "seconds", "connect"),
    [("5", 0.5, "0.2"), ("1", 10.0, "0.5")],
    ids=["cancelled", "read-timeout"],
)
def test_a_burst_on_a_hung_address_says_it_once(network, caplog, timeout, seconds, connect):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    hung, a, b = network
    p = on_network(
        hung.url, a.url, b.url, ATLAS_TIMEOUT_SECONDS=timeout, ATLAS_CONNECT_TIMEOUT_SECONDS=connect
    )

    async def burst() -> object:
        try:
            await asyncio.gather(
                *(p.describe([INVOICES], deadline=time.monotonic() + seconds) for _ in range(50)),
                return_exceptions=True,
            )
            return await p.describe([INVOICES], deadline=time.monotonic() + 5.0)
        finally:
            await p.aclose()

    after = asyncio.run(burst())
    assert len(left_lines(caplog)) == 1
    assert INVOICES in after  # type: ignore[operator]
    assert b.requests == 0


def test_a_single_address_is_kept_after_a_cancelled_wait(network, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    hung, _, _ = network
    p = on_network(hung.url, ATLAS_CONNECT_TIMEOUT_SECONDS="0.1")
    first, second = calls(p, count=2, seconds=0.3)
    assert isinstance(first, ProviderUnavailable) and isinstance(second, ProviderUnavailable)
    assert len(hung.accepted) == 2  # the same address, asked again
    # Nowhere else to go, but the operator is told, once until it answers.
    assert left_lines(caplog) == ["atlas address 1 of 1 in ATLAS_URL did not answer in time"]


@pytest.mark.parametrize(
    "error",
    [httpx.PoolTimeout("busy"), httpx.ReadError("reset"), httpx.RemoteProtocolError("closed")],
    ids=["pool", "read-error", "protocol-error"],
)
def test_no_free_connection_or_a_closed_connection_leaves_no_address(caplog, error):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    handler = by_host({"atlas-a": error, "atlas-b": httpx.Response(503)})
    first, second = calls(build(handler), count=2)
    assert isinstance(first, ProviderUnavailable) and isinstance(second, ProviderUnavailable)
    # The pool is the client's own wait, and a connection closed under a call is not a frozen
    # server: the call tries b, and each call starts at a again.
    assert [r.url.host for r in handler.seen] == ["atlas-a", "atlas-b"] * 2
    assert left_lines(caplog) == []


def test_two_addresses_a_read_timeout_just_below_the_deadline_costs_one_call(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")

    def slow(request: httpx.Request) -> None:
        time.sleep(0.2)  # a healthy Atlas, a fresh negotiation included

    hung, a = _Hung(), _Atlas(slow)
    try:
        p = on_network(
            hung.url, a.url, ATLAS_TIMEOUT_SECONDS="0.5", ATLAS_CONNECT_TIMEOUT_SECONDS="0.2"
        )
        # 0.5 s on the hung address, then the deadline cuts the call on a: that call is lost,
        # and a, which never failed, is not left for it.
        first, *later = calls(p, count=4, seconds=0.6)
    finally:
        hung.close()
        a.close()
    assert isinstance(first, ProviderUnavailable)
    assert all(INVOICES in found for found in later)
    assert len(hung.accepted) == 1
    assert left_lines(caplog) == ["atlas address 1 of 2 in ATLAS_URL did not answer in time"]


def test_a_sibling_cancelled_by_a_refusal_leaves_no_address(network, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    _, a, b = network
    guids = sorted(FakeAtlas("atlas-2.4").terms)

    def refuse_one(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/refused"):
            return httpx.Response(401)
        if request.url.path.endswith(tuple(guids[1:])):
            time.sleep(0.5)  # in flight on a when the refusal cancels them
        return None

    a.answer = refuse_one
    p = on_network(a.url, b.url)

    async def run() -> tuple[object, object]:
        try:
            try:
                await p.terms([guids[0], *guids[1:], "refused"], deadline=time.monotonic() + 5)
            except ProviderRefused as refused:
                error: object = refused
            a.answer = None
            return error, await p.describe([INVOICES], deadline=time.monotonic() + 5)
        finally:
            await p.aclose()

    error, after = asyncio.run(run())
    assert isinstance(error, ProviderRefused)
    assert INVOICES in after  # type: ignore[operator]
    assert b.requests == 0 and left_lines(caplog) == []


def test_a_passive_redirect_to_a_hung_active_server_is_said_once(network, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    hung, passive, _ = network

    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(303, headers={"location": hung.url + request.url.raw_path.decode()})

    passive.answer = redirect
    p = on_network(passive.url, hung.url, ATLAS_CONNECT_TIMEOUT_SECONDS="0.2")
    results = calls(p, count=3, seconds=0.5)
    assert all(isinstance(r, ProviderUnavailable) for r in results)
    assert len(hung.accepted) == 3  # the passive server keeps sending the calls there
    assert left_lines(caplog) == ["atlas address 2 of 2 in ATLAS_URL did not answer in time"]


def test_two_hung_addresses_are_said_once_each(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    one, two = _Hung(), _Hung()
    try:
        p = on_network(one.url, two.url, ATLAS_CONNECT_TIMEOUT_SECONDS="0.1")
        results = calls(p, count=4, seconds=0.3)
    finally:
        one.close()
        two.close()
    assert all(isinstance(r, ProviderUnavailable) for r in results)
    assert len(one.accepted) == len(two.accepted) == 2  # they take turns
    assert sorted(left_lines(caplog)) == [
        "atlas address 1 of 2 in ATLAS_URL did not answer in time",
        "atlas address 2 of 2 in ATLAS_URL did not answer in time",
    ]


def test_an_address_that_answers_again_is_said_again_when_it_hangs_again(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    atlas = FakeAtlas("atlas-2.4")
    # One request a call: each address in turn times out, then answers.
    script = iter(["atlas-a", "atlas-b", "atlas-b", "atlas-a", "atlas-a", "atlas-b"])
    late = {0, 2, 4}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == next(script)
        seen.append(request.url.host)
        if len(seen) - 1 in late:
            raise httpx.ReadTimeout("slow")
        return atlas(request)

    found = calls(build(handler, ATLAS_TABLE_TYPES="hive_table"), count=3)
    assert all(INVOICES in each for each in found)
    assert left_lines(caplog) == [
        "atlas address 1 of 2 in ATLAS_URL did not answer in time",
        "atlas address 2 of 2 in ATLAS_URL did not answer in time",
        "atlas address 1 of 2 in ATLAS_URL did not answer in time",
    ]


def test_only_the_address_a_call_started_with_is_said_late(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    handler = by_host(
        {
            "atlas-a": httpx.ReadTimeout("slow"),
            "atlas-b": httpx.ReadTimeout("slow"),
            "atlas-c": "replay",
        }
    )
    urls = f"{ATLAS},{OTHER},http://atlas-c:21000"
    (found,) = calls(build(handler, ATLAS_URL=urls, ATLAS_TABLE_TYPES="hive_table"))
    assert INVOICES in found
    # b timed out too, but the call reached it after leaving a: only a is said.
    assert left_lines(caplog) == ["atlas address 1 of 3 in ATLAS_URL did not answer in time"]


def test_terms_cut_by_the_tool_calls_deadline_after_describe_leave_no_address(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")

    def slow(request: httpx.Request) -> None:
        time.sleep(0.2)  # a healthy Atlas

    guids = sorted(FakeAtlas("atlas-2.4").terms)
    hung, a = _Hung(), _Atlas(slow)
    p = on_network(
        hung.url,
        a.url,
        ATLAS_TIMEOUT_SECONDS="0.5",
        ATLAS_CONNECT_TIMEOUT_SECONDS="0.4",
        ATLAS_TABLE_TYPES="hive_table",
        ATLAS_GLOSSARIES="finance",
    )

    async def tool_calls() -> list[tuple[bool, object]]:
        # As a tool call does: describe, then the terms, on one deadline.
        out: list[tuple[bool, object]] = []
        try:
            for _ in range(4):
                deadline = time.monotonic() + 1.0
                described = await p.describe([INVOICES], deadline=deadline)
                try:
                    read: object = await p.terms(guids, deadline=deadline)
                except ProviderUnavailable as unavailable:
                    read = unavailable
                out.append((INVOICES in described, read))
        finally:
            await p.aclose()
        return out

    try:
        out = asyncio.run(tool_calls())
    finally:
        hung.close()
        a.close()
    # The first tool call spends 0.5 s on the hung address and 0.2 s on a: its terms are cut
    # after a short wait on a, which says nothing of a. From then on a serves everything.
    assert all(described for described, _ in out)
    assert isinstance(out[0][1], ProviderUnavailable)
    assert all(isinstance(read, dict) and read for _, read in out[1:])
    assert len(hung.accepted) == 1
    assert left_lines(caplog) == ["atlas address 1 of 2 in ATLAS_URL did not answer in time"]
