"""Glossary terms read by GUID from the recorded Atlas 2.1 and 2.4, under the allow list."""

import asyncio
import logging
import time

import httpx
import pytest
from akko_mcp_trino.context import (
    ProviderAnswerTooLarge,
    ProviderRefused,
    ProviderUnavailable,
    TableRef,
    Term,
    TermRef,
)
from atlas_replay import ATLAS, TERM, FakeAtlas, recording, transport

from akko_mcp_trino_atlas import AtlasProvider
from akko_mcp_trino_atlas.provider import _first_of
from akko_mcp_trino_atlas.settings import read_settings

BASE = {
    "ATLAS_URL": ATLAS,
    "ATLAS_USER": "reader",
    "ATLAS_PASSWORD": "reader-pass",
    "ATLAS_CLUSTERS": '{"lake": "primary"}',
    "ATLAS_GLOSSARIES": "finance",
}
INVOICE = "00000000-0000-4000-8000-000000000010"  # Invoice@finance, Atlas 2.4
STAFF = "00000000-0000-4000-8000-000000000012"  # Staff@hr, Atlas 2.4
PAYMENT = "00000000-0000-4000-8000-000000000028"  # Payment@finance, Atlas 2.4
BILL = "00000000-0000-4000-8000-000000000042"  # Bill@finance, Atlas 2.4
RECEIVABLE = "00000000-0000-4000-8000-000000000040"  # Receivable@finance, not read
TERM_05 = "00000000-0000-4000-8000-000000000133"  # Term 05@finance, Atlas 2.1
INVOICES = TableRef("lake", "ledger", "invoices")


def build(handler, **env: str) -> AtlasProvider:
    return AtlasProvider(read_settings({**BASE, **env}), transport=transport(handler))


def read(p: AtlasProvider, keys: list[str], seconds: float = 5.0):
    async def run():
        try:
            return await p.terms(keys, deadline=time.monotonic() + seconds)
        finally:
            await p.aclose()

    return asyncio.run(run())


def failure(p: AtlasProvider, keys: list[str], seconds: float = 5.0) -> BaseException:
    with pytest.raises(Exception) as raised:
        read(p, keys, seconds)
    return raised.value


def term_paths(atlas: FakeAtlas) -> list[str]:
    return [r.url.path.removeprefix(TERM) for r in atlas.requests if r.url.path.startswith(TERM)]


def test_the_glossary_comes_from_the_qualified_name_suffix():
    atlas = FakeAtlas("atlas-2.4")
    # The term names its glossary by GUID only (anchor); the suffix of its qualified name
    # is what names it, and a name that holds "@" is cut at the last one.
    assert "glossaryName" not in atlas.terms[INVOICE]["anchor"]
    atlas.terms[PAYMENT]["qualifiedName"] = "Pay@ment@finance"
    atlas.terms[BILL]["qualifiedName"] = "Bill"  # no suffix: no glossary, never served
    found = read(build(atlas), [INVOICE, PAYMENT, BILL])
    assert set(found) == {INVOICE, PAYMENT}
    assert (found[INVOICE].glossary, found[PAYMENT].glossary) == ("finance", "finance")
    assert found[INVOICE] == Term(
        key=INVOICE,
        name="Invoice",
        glossary="finance",
        definition="shortDescription of Invoice",
        status="",
        categories=("Billing",),
        relations=found[INVOICE].relations,
    )


def test_the_term_status_is_read_from_the_configured_attribute():
    atlas = FakeAtlas("atlas-2.4")
    found = read(build(atlas, ATLAS_TERM_STATUS_ATTRIBUTE="approvalStatus"), [INVOICE, BILL])
    assert (found[INVOICE].status, found[BILL].status) == ("APPROVED", "DEPRECATED")
    # Without the setting, no status: never the status of an assignment.
    assert read(build(FakeAtlas("atlas-2.4")), [INVOICE])[INVOICE].status == ""
    other = read(build(FakeAtlas("atlas-2.4"), ATLAS_TERM_STATUS_ATTRIBUTE="steward"), [INVOICE])
    assert other[INVOICE].status == ""


def test_the_definition_is_the_short_description_else_the_long_one():
    atlas = FakeAtlas("atlas-2.4")
    atlas.terms[INVOICE]["shortDescription"] = " "
    del atlas.terms[BILL]["shortDescription"]
    del atlas.terms[BILL]["longDescription"]
    found = read(build(atlas), [INVOICE, BILL])
    assert found[INVOICE].definition == "longDescription of Invoice"
    assert found[BILL].definition == ""


def test_relations_and_categories_come_from_the_term_without_another_request():
    atlas = FakeAtlas("atlas-2.4")
    invoice = atlas.terms[INVOICE]
    header = {"displayText": "Due", "qualifiedName": "Due@finance", "termGuid": "g-due"}
    invoice["preferredTerms"] = [header]
    invoice["preferredToTerms"] = [{**header, "termGuid": "g-due-2", "displayText": "Owed"}]
    invoice["seeAlso"].append({"displayText": "Unnamed", "termGuid": "g-unnamed"})
    invoice["seeAlso"].append("not an object")
    invoice["categories"].append({"categoryGuid": "g-c"})  # a category without a name

    found = read(build(atlas), [INVOICE, BILL, INVOICE, ""])

    assert term_paths(atlas) == [INVOICE, BILL]  # one read per distinct key, nothing more
    assert found[INVOICE].categories == ("Billing",)
    assert found[INVOICE].relations == (
        TermRef(BILL, "Bill", "finance", "synonym"),
        TermRef("g-due", "Due", "finance", "preferred"),
        TermRef("g-due-2", "Owed", "finance", "preferred_to"),
        # Staff@hr is outside ATLAS_GLOSSARIES, and a header without a qualified name
        # names no glossary: both are left out, unread.
        TermRef(PAYMENT, "Payment", "finance", "see_also"),
        TermRef(RECEIVABLE, "Receivable", "finance", "is_a"),
    )  # replacementTerms is not one of the six relations read
    assert found[BILL].relations == (
        TermRef(INVOICE, "Invoice", "finance", "synonym"),
        TermRef(INVOICE, "Invoice", "finance", "replaced_by"),
    )

    older = FakeAtlas("atlas-2.1")
    (term,) = read(build(older), [TERM_05]).values()
    assert [(r.name, r.relation) for r in term.relations] == [
        ("Term 09", "see_also"),
        ("Term 10", "see_also"),
        ("Term 08", "is_a"),
    ]  # classifies is not read
    assert term.categories == ("Category 04",)
    assert term_paths(older) == [TERM_05]


@pytest.mark.parametrize("version", ["atlas-2.1", "atlas-2.4"])
def test_a_term_of_a_glossary_outside_the_allow_list_is_never_read(version):
    atlas = FakeAtlas(version)
    p = build(atlas)

    async def run():
        try:
            tables = await p.describe([INVOICES], deadline=time.monotonic() + 5)
            keys = {key for ctx in tables.values() for key in ctx.terms}
            keys |= {
                key for ctx in tables.values() for c in ctx.columns.values() for key in c.terms
            }
            return keys, await p.terms(sorted(keys), deadline=time.monotonic() + 5)
        finally:
            await p.aclose()

    keys, found = asyncio.run(run())
    assert keys and set(found) == keys
    assert all(term.glossary == "finance" for term in found.values())
    assert sorted(term_paths(atlas)) == sorted(keys)
    assert STAFF not in term_paths(atlas)  # attached to the table in 2.4, glossary hr


def test_a_term_read_outside_the_allow_list_is_never_served():
    atlas = FakeAtlas("atlas-2.4")
    del atlas.terms[PAYMENT]["qualifiedName"]
    found = read(build(atlas), [STAFF, PAYMENT, INVOICE])
    assert set(found) == {INVOICE}
    assert read(build(FakeAtlas("atlas-2.4"), ATLAS_GLOSSARIES=""), [INVOICE]) == {}


def test_a_term_atlas_does_not_hold_is_left_out(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    # Atlas 2.1's answer to a GUID it does not hold, recorded.
    recorded = recording("atlas-2.1", "glossary-term-not-found")
    assert recorded["response"]["status"] == 404
    assert recorded["response"]["body"]["errorCode"] == "ATLAS-404-00-005"
    unknown = recorded["request"]["path"].removeprefix(TERM)
    assert read(build(FakeAtlas("atlas-2.4")), [unknown, INVOICE]).keys() == {INVOICE}
    assert not caplog.records

    # Any other 404 stays an outage.
    def other(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errorCode": "ATLAS-404-00-001"})

    error = failure(build(other), [INVOICE])
    assert isinstance(error, ProviderUnavailable)
    assert [r.getMessage() for r in caplog.records] == ["atlas unreachable"]


def test_an_instance_not_found_by_unique_attribute_is_an_outage_for_a_term(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    answer = recording("atlas-2.4", "entity-deleted-table")["response"]
    assert answer["body"]["errorCode"] == "ATLAS-404-00-009"

    def by_attribute(request: httpx.Request) -> httpx.Response:
        return httpx.Response(answer["status"], json=answer["body"])

    assert isinstance(failure(build(by_attribute), [INVOICE]), ProviderUnavailable)
    assert [r.getMessage() for r in caplog.records] == ["atlas unreachable"]


def test_term_reads_run_in_parallel_within_the_bound():
    atlas = FakeAtlas("atlas-2.4")
    keys = [INVOICE, STAFF, PAYMENT, BILL] + [f"g-{i}" for i in range(6)]
    now = {"in_flight": 0, "most": 0}

    async def slow(request: httpx.Request) -> httpx.Response:
        now["in_flight"] += 1
        now["most"] = max(now["most"], now["in_flight"])
        await asyncio.sleep(0.05)
        now["in_flight"] -= 1
        return atlas(request)

    found = read(
        build(slow, ATLAS_MAX_CONCURRENT_REQUESTS="3", ATLAS_GLOSSARIES="finance,hr"), keys
    )
    assert set(found) == {INVOICE, STAFF, PAYMENT, BILL}
    assert now["most"] == 3  # never more than the bound in flight, and the bound is used
    assert term_paths(atlas)[0] == INVOICE


def test_the_first_term_read_goes_alone():
    atlas = FakeAtlas("atlas-2.4")
    started: list[str] = []
    others_started = asyncio.Event()
    seen = {}

    async def hold_the_first(request: httpx.Request) -> httpx.Response:
        started.append(request.url.path)
        if len(started) == 1:
            # Held open: if another read started now, it would release the first at once.
            try:
                await asyncio.wait_for(others_started.wait(), 0.2)
            except TimeoutError:
                pass
            seen["started_while_held"] = len(started)
        else:
            others_started.set()
        return atlas(request)

    found = read(build(hold_the_first), [INVOICE, BILL, PAYMENT])
    assert set(found) == {INVOICE, BILL, PAYMENT}
    assert seen["started_while_held"] == 1


def test_a_refused_account_costs_one_request():
    seen: list[httpx.Request] = []

    def refused(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401)

    error = failure(build(refused), [INVOICE, BILL, PAYMENT, STAFF])
    assert isinstance(error, ProviderRefused)
    assert len(seen) == 1


def test_a_failure_among_parallel_reads_is_the_error_of_the_call(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    atlas = FakeAtlas("atlas-2.4")

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(BILL):
            return httpx.Response(401)
        if request.url.path.endswith(PAYMENT):
            return httpx.Response(502)
        return atlas(request)

    error = failure(build(broken, ATLAS_MAX_CONCURRENT_REQUESTS="1"), [INVOICE, BILL, PAYMENT])
    assert isinstance(error, ProviderRefused)
    error = failure(build(broken), [INVOICE, PAYMENT])
    assert isinstance(error, ProviderUnavailable)
    assert error.__cause__ is None and error.__suppress_context__


def test_a_refusal_among_outages_is_the_error_of_the_call():
    refused, down, bug = ProviderRefused("401"), ProviderUnavailable("502"), RuntimeError("bug")
    assert _first_of(ExceptionGroup("reads", [down, bug, refused, down])) is refused
    assert _first_of(ExceptionGroup("reads", [bug, down])) is down
    assert _first_of(ExceptionGroup("reads", [bug])) is bug


def test_an_outage_among_answers_too_large_is_the_error_of_the_call():
    # The outage opens the retry pause; an answer too large would hide it.
    large, down = ProviderAnswerTooLarge("too large"), ProviderUnavailable("502")
    bug = RuntimeError("bug")
    assert _first_of(ExceptionGroup("reads", [large, bug, down])) is down
    assert _first_of(ExceptionGroup("reads", [bug, large])) is large


def test_an_unexpected_failure_among_parallel_reads_is_raised_as_it_is():
    atlas = FakeAtlas("atlas-2.4")

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(BILL):
            raise RuntimeError("a bug, not an outage")
        return atlas(request)

    error = failure(build(broken), [INVOICE, BILL])
    assert type(error) is RuntimeError


def test_the_deadline_covers_every_read_and_nothing_partial_is_returned(caplog):
    caplog.set_level(logging.INFO)
    atlas = FakeAtlas("atlas-2.4")
    in_flight = []

    async def slow_after_first(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(INVOICE):
            return atlas(request)
        in_flight.append(request)
        await asyncio.sleep(30)
        return atlas(request)

    started = time.monotonic()
    error = failure(build(slow_after_first), [INVOICE, BILL, PAYMENT], seconds=0.3)
    assert isinstance(error, ProviderUnavailable)
    assert time.monotonic() - started < 2
    assert len(in_flight) == 2
    assert "atlas unreachable" not in caplog.text


def test_no_term_name_glossary_or_guid_reaches_the_log(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    atlas = FakeAtlas("atlas-2.4")

    def down_after_first(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(INVOICE):
            return atlas(request)
        return httpx.Response(503)

    read(build(atlas), [INVOICE, BILL])
    failure(build(down_after_first), [INVOICE, BILL])
    for forbidden in ("Invoice", "Bill", "finance", INVOICE, BILL, "atlas-a"):
        assert forbidden not in caplog.text
    assert [r.getMessage() for r in caplog.records] == ["atlas unreachable"]


def test_no_key_asks_nothing():
    atlas = FakeAtlas("atlas-2.4")
    assert read(build(atlas), []) == {} and read(build(atlas), [""]) == {}
    assert atlas.requests == []


def test_a_malformed_term_serves_what_it_can():
    atlas = FakeAtlas("atlas-2.4")
    atlas.terms[INVOICE]["name"] = ["not", "text"]
    atlas.terms[BILL]["categories"] = "not a list"
    atlas.terms[BILL]["additionalAttributes"] = ["approvalStatus"]

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(PAYMENT):
            return httpx.Response(200, json=["not", "an", "object"])
        return atlas(request)

    found = read(
        build(answer, ATLAS_TERM_STATUS_ATTRIBUTE="approvalStatus"), [INVOICE, BILL, PAYMENT]
    )
    assert set(found) == {BILL}  # a term without a name is not served
    assert (found[BILL].categories, found[BILL].status) == ((), "")


@pytest.mark.parametrize("value", ["0", "33", "four", "1.5"])
def test_the_concurrency_bound_is_checked_at_start(value):
    says = "ATLAS_MAX_CONCURRENT_REQUESTS must be an integer between 1 and 32"
    with pytest.raises(ValueError, match=f"^{says}$"):
        read_settings({**BASE, "ATLAS_MAX_CONCURRENT_REQUESTS": value})


def test_the_concurrency_bound_has_a_default_and_is_printed():
    assert read_settings(BASE).max_concurrent_requests == 4
    assert (
        read_settings({**BASE, "ATLAS_MAX_CONCURRENT_REQUESTS": "32"}).max_concurrent_requests == 32
    )
    p = build(FakeAtlas("atlas-2.4"), ATLAS_MAX_CONCURRENT_REQUESTS="2")
    lines = p.check_lines()
    asyncio.run(p.aclose())
    assert "atlas: term reads: 2 at once at most" in lines


def test_a_term_atlas_refuses_to_the_account_is_left_out_and_said_once(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    atlas = FakeAtlas("atlas-2.4")

    def refuses_some(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith((BILL, PAYMENT)):
            # Constructed as Atlas's UNAUTHORIZED_ACCESS: to be replaced by a recording.
            return httpx.Response(
                403,
                json={
                    "errorCode": "ATLAS-403-00-001",
                    "errorMessage": "reader is not authorized to perform read entity",
                },
            )
        return atlas(request)

    found = read(build(refuses_some), [INVOICE, BILL, PAYMENT])
    assert set(found) == {INVOICE}
    assert [r.getMessage() for r in caplog.records] == [
        "atlas refused some objects to the account; an account with no read grant serves nothing"
    ]
