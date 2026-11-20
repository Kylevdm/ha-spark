"""The Happy Hour booking reader: settings, request shape, reply validation, result states."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from ha_spark.config import Settings, field_metadata
from ha_spark.energy.octopus import (
    HappyHourBooking,
    fetch_happy_hours,
    happy_hour_source,
)
from tests.octopus_helpers import token_response

API = "http://backend.test/v1"
GRAPHQL = f"{API}/graphql/"
# The one read-only query the reader may send; anything else is a bug.
QUERY = """
query HappyHours($account: String!) {
  savingSessions { account(accountNumber: $account) {
    joinedEvents { eventId eventType startAt endAt } } }
}
"""
NOW = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


def _settings(**kw: Any) -> Settings:
    base = dict(
        octopus_backend_api_url=API,
        octopus_api_key="sk_test",
        octopus_account_number="A-1234ABCD",
    )
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _event(
    event_id: Any, start: Any, end: Any, kind: str = "WEEKEND_HAPPY_HOUR"
) -> dict[str, Any]:
    return {"eventId": event_id, "eventType": kind, "startAt": start, "endAt": end}


def _reply(events: list[Any]) -> httpx.Response:
    return httpx.Response(200, json={"data": {"savingSessions": {"account": {
        "joinedEvents": events,
    }}}})


def _booking(event_id: str, start: str, end: str) -> HappyHourBooking:
    return HappyHourBooking(
        event_id,
        datetime.fromisoformat(start).astimezone(UTC),
        datetime.fromisoformat(end).astimezone(UTC),
    )


BOOKED = ("2026-11-08T11:00:00+00:00", "2026-11-08T12:00:00+00:00")


@respx.mock
async def test_one_booked_hour_is_read() -> None:
    route = respx.post(GRAPHQL).mock(side_effect=[
        token_response(),
        _reply([
            _event(8066, *BOOKED),
            # Other event types are ignored whatever else they hold.
            _event("junk", "not-a-time", "either", kind="TURN_DOWN"),
        ]),
    ])
    reading = await fetch_happy_hours(_settings(), now=NOW)
    assert reading.bookings == (_booking("8066", *BOOKED),)
    assert reading.read_at == NOW
    # Exactly the one read query, with only the account number as a variable.
    assert route.call_count == 2
    mint = json.loads(route.calls[0].request.content)
    assert "obtainKrakenToken" in mint["query"]  # the shared helper's token exchange
    assert set(mint["variables"]) == {"apiKey"}
    body = json.loads(route.calls[1].request.content)
    assert body == {"query": QUERY, "variables": {"account": "A-1234ABCD"}}
    assert "mutation" not in body["query"]
    assert "A-1234ABCD" not in body["query"]
    assert route.calls[1].request.headers["Authorization"] == "JWT jwt-private"


@respx.mock
async def test_adjacent_booked_hours_stay_separate_bookings() -> None:
    respx.post(GRAPHQL).mock(side_effect=[
        token_response(),
        _reply([
            _event(8067, "2026-11-08T12:00:00+00:00", "2026-11-08T13:00:00+00:00"),
            _event(8066, *BOOKED),
        ]),
    ])
    reading = await fetch_happy_hours(_settings(), now=NOW)
    assert reading.bookings == (
        _booking("8066", *BOOKED),
        _booking("8067", "2026-11-08T12:00:00+00:00", "2026-11-08T13:00:00+00:00"),
    )


@respx.mock
async def test_other_event_types_mean_no_booking() -> None:
    respx.post(GRAPHQL).mock(side_effect=[
        token_response(),
        _reply([_event("t", *BOOKED, kind="TURN_DOWN")]),
    ])
    reading = await fetch_happy_hours(_settings(), now=NOW)
    assert reading.bookings == ()


@respx.mock
async def test_offered_slots_are_never_returned_as_bookings() -> None:
    # Offered (`events`) slots must never become bookings, even if echoed back.
    offered = {"data": {"savingSessions": {
        "events": [_event(9999, "2026-11-08T14:00:00+00:00", "2026-11-08T15:00:00+00:00")],
        "account": {"joinedEvents": [_event("t", *BOOKED, kind="TURN_DOWN")]},
    }}}
    respx.post(GRAPHQL).mock(side_effect=[token_response(), httpx.Response(200, json=offered)])
    reading = await fetch_happy_hours(_settings(), now=NOW)
    assert reading.bookings == ()


@respx.mock
async def test_a_booking_under_way_is_kept() -> None:
    respx.post(GRAPHQL).mock(side_effect=[
        token_response(),
        _reply([_event(8068, "2026-11-01T09:30:00+00:00", "2026-11-01T11:00:00+00:00")]),
    ])
    reading = await fetch_happy_hours(_settings(), now=NOW)
    assert reading.bookings == (
        _booking("8068", "2026-11-01T09:30:00+00:00", "2026-11-01T11:00:00+00:00"),
    )


@respx.mock
async def test_past_booking_is_history_not_a_booking() -> None:
    respx.post(GRAPHQL).mock(side_effect=[
        token_response(),
        _reply([
            _event(7001, "2026-10-04T11:00:00+00:00", "2026-10-04T12:00:00+00:00"),
            _event(8066, *BOOKED),
        ]),
    ])
    reading = await fetch_happy_hours(_settings(), now=NOW)
    assert reading.bookings == (_booking("8066", *BOOKED),)


@respx.mock
async def test_one_entry_per_event_id() -> None:
    respx.post(GRAPHQL).mock(side_effect=[
        token_response(),
        _reply([
            _event(8066, *BOOKED),
            _event("8066", "2026-11-15T11:00:00+00:00", "2026-11-15T12:00:00+00:00"),
        ]),
    ])
    reading = await fetch_happy_hours(_settings(), now=NOW)
    assert reading.bookings == (_booking("8066", *BOOKED),)


def _booked_payload() -> dict[str, Any]:
    return {"data": {"savingSessions": {"account": {"joinedEvents": [
        _event(8066, *BOOKED),
    ]}}}}


# Marks a field removed from a reply event, for the schema-drift failures.
MISSING = object()


def _failures() -> dict[str, list[Any]]:
    echo = "sk_test jwt-private A-1234ABCD"
    payload = _booked_payload()
    graphql_errors = httpx.Response(200, json={
        "errors": [{"message": echo}], "data": payload["data"],
    })

    def whh(**changes: Any) -> dict[str, Any]:
        """A booked event with fields overridden (``MISSING`` removes a field)."""
        event: dict[str, Any] = {
            "eventId": 8066,
            "eventType": "WEEKEND_HAPPY_HOUR",
            "startAt": BOOKED[0],
            "endAt": BOOKED[1],
        }
        for key, value in changes.items():
            if value is MISSING:
                del event[key]
            else:
                event[key] = value
        return event

    bad_events: dict[str, Any] = {
        "event_not_object": [echo],
        "event_type_missing": [{"eventId": 8066, "startAt": BOOKED[0], "endAt": BOOKED[1]}],
        "event_type_not_string": [whh(eventType=7)],
        "whh_id_missing": [whh(eventId=MISSING)],
        "whh_id_bool": [whh(eventId=True)],
        "whh_id_empty": [whh(eventId="")],
        "whh_id_echo": [whh(eventId="sk_test jwt-private A-1234ABCD")],
        "whh_id_wrong_type": [whh(eventId=80.5)],
        "whh_start_missing": [whh(startAt=MISSING)],
        "whh_end_missing": [whh(endAt=MISSING)],
    }
    responses: dict[str, list[Any]] = {
        "http": [token_response(), httpx.Response(500, text=echo)],
        "auth_401": [token_response(), httpx.Response(401, text=echo)],
        "transport": [token_response(), httpx.ConnectError(echo)],
        "not_json": [token_response(), httpx.Response(200, text=echo)],
        "non_dict_reply": [token_response(), httpx.Response(200, json=[echo])],
        "graphql_errors": [token_response(), graphql_errors],
        "data_missing": [token_response(), httpx.Response(200, json={"nope": echo})],
        "data_null": [token_response(), httpx.Response(200, json={"data": None, "errors": None})],
        "saving_sessions_missing": [token_response(), httpx.Response(200, json={"data": {}})],
        "saving_sessions_null": [
            token_response(), httpx.Response(200, json={"data": {"savingSessions": None}}),
        ],
        "account_missing": [
            token_response(), httpx.Response(200, json={"data": {"savingSessions": {}}}),
        ],
        "joined_events_missing": [
            token_response(),
            httpx.Response(200, json={"data": {"savingSessions": {"account": {}}}}),
        ],
        "joined_events_not_list": [
            token_response(),
            httpx.Response(200, json={"data": {"savingSessions": {"account": {
                "joinedEvents": echo,
            }}}}),
        ],
    }
    for name, events in bad_events.items():
        body = _booked_payload()
        body["data"]["savingSessions"]["account"]["joinedEvents"] = events
        responses[name] = [token_response(), httpx.Response(200, json=body)]
    return responses


@pytest.mark.parametrize("failure", sorted(_failures()))
@respx.mock
async def test_bad_replies_fail_the_read_never_raise_never_book(failure: str) -> None:
    respx.post(GRAPHQL).mock(side_effect=_failures()[failure])
    reading = await fetch_happy_hours(_settings(), now=NOW)
    assert reading.bookings is None
    assert reading.read_at == NOW


@pytest.mark.parametrize("start, end", [
    ("2026-11-08T11:00:00", "2026-11-08T12:00:00+00:00"),  # naive: zone never guessed
    (1731050400, "2026-11-08T12:00:00+00:00"),  # numeric: never accepted as a time
    ("Sunday 11am", "2026-11-08T12:00:00+00:00"),
    ("2026-11-08T12:00:00+00:00", "2026-11-08T11:00:00+00:00"),  # end before start
    ("2026-11-08T11:00:00+00:00", "2026-11-08T11:00:00+00:00"),  # end equals start
    ({"iso": "2026-11-08T11:00:00+00:00"}, "2026-11-08T12:00:00+00:00"),
    ("0001-01-01T00:00:00+14:00", "2026-11-08T12:00:00+00:00"),  # before the calendar in UTC
    ("0001-01-01T00:00:00+00:00", "2026-11-08T12:00:00+00:00"),  # unshowable on a local clock
    ("2026-11-08T11:00:00+00:00", "9999-12-31T23:59:59-14:00"),  # past the calendar in UTC
])
@respx.mock
async def test_unusable_times_drop_only_that_booking(start: Any, end: Any) -> None:
    respx.post(GRAPHQL).mock(side_effect=[
        token_response(),
        _reply([
            _event(8066, start, end),
            _event(8067, "2026-11-08T13:00:00+00:00", "2026-11-08T14:00:00+00:00"),
        ]),
    ])
    reading = await fetch_happy_hours(_settings(), now=NOW)
    assert reading.bookings == (
        _booking("8067", "2026-11-08T13:00:00+00:00", "2026-11-08T14:00:00+00:00"),
    )


def test_source_never_depends_on_tariff_provider() -> None:
    assert happy_hour_source(_settings(tariff_provider="fixed")) == "active"
    assert happy_hour_source(_settings(happy_hour_enabled=False)) == "disabled"
    assert happy_hour_source(_settings(octopus_api_key="")) == "not_configured"
    assert happy_hour_source(_settings(octopus_account_number="")) == "not_configured"
    assert happy_hour_source(_settings(octopus_backend_api_url="  ")) == "not_configured"


@pytest.mark.parametrize("kw", [
    {"happy_hour_enabled": False},
    {"octopus_api_key": ""},
    {"octopus_account_number": ""},
    {"octopus_backend_api_url": ""},
])
@respx.mock
async def test_inactive_source_sends_nothing_and_fails_quietly(kw: dict[str, Any]) -> None:
    reading = await fetch_happy_hours(_settings(**kw), now=NOW)
    assert reading.bookings is None
    assert not respx.calls


@respx.mock
async def test_requests_go_only_to_the_backend_endpoint() -> None:
    public = respx.post("http://octo.test/graphql/").mock(
        side_effect=[token_response(), _reply([])]
    )
    backend = respx.post(GRAPHQL).mock(
        side_effect=[token_response(), _reply([_event(8066, *BOOKED)])]
    )
    reading = await fetch_happy_hours(_settings(), now=NOW)
    assert public.call_count == 0
    assert backend.call_count == 2
    assert reading.bookings == (_booking("8066", *BOOKED),)


@respx.mock
async def test_one_shared_token_serves_repeated_reads() -> None:
    respx.post(GRAPHQL).mock(side_effect=[
        token_response(), _reply([_event(8066, *BOOKED)]), _reply([_event(8066, *BOOKED)]),
    ])
    await fetch_happy_hours(_settings(), now=NOW)
    await fetch_happy_hours(_settings(), now=NOW)
    assert len(respx.calls) == 3  # one mint, two queries


@respx.mock
async def test_read_time_defaults_to_the_clock() -> None:
    respx.post(GRAPHQL).mock(side_effect=[token_response(), _reply([])])
    before = datetime.now(UTC)
    reading = await fetch_happy_hours(_settings())
    assert before <= reading.read_at <= datetime.now(UTC)


@respx.mock
async def test_a_read_time_with_no_offset_is_replaced_by_the_clock() -> None:
    respx.post(GRAPHQL).mock(side_effect=[token_response(), _reply([_event(8066, *BOOKED)])])
    reading = await fetch_happy_hours(_settings(), now=datetime(2020, 1, 1))
    assert reading.read_at.tzinfo is not None
    assert reading.bookings is not None


@respx.mock
async def test_a_failed_read_logs_one_fixed_sentence(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="ha_spark.energy.octopus")
    respx.post(GRAPHQL).mock(side_effect=[token_response(), httpx.Response(500, text="sk_test")])
    await fetch_happy_hours(_settings(), now=NOW)
    messages = [r.getMessage() for r in caplog.records if r.name == "ha_spark.energy.octopus"]
    assert messages == ["Happy Hour read failed; no Happy Hour is planned"]


@pytest.mark.parametrize(
    "failure",
    ["http", "transport", "not_json", "graphql_errors", "event_not_object", "whh_id_echo"],
)
@respx.mock
async def test_secrets_never_appear_anywhere(
    failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    respx.post(GRAPHQL).mock(side_effect=_failures()[failure])
    reading = await fetch_happy_hours(_settings(), now=NOW)
    exposed = caplog.text + repr(reading) + str(reading)
    assert "sk_test" not in exposed
    assert "jwt-private" not in exposed
    assert "A-1234ABCD" not in exposed


def test_new_settings_are_addon_owned_and_not_secret() -> None:
    metadata = field_metadata()
    assert metadata["octopus_backend_api_url"] == {
        "owner": "addon", "secret": False, "reload": "live", "display_only": False,
    }
    assert metadata["happy_hour_enabled"] == {
        "owner": "addon", "secret": False, "reload": "live", "display_only": False,
    }
    defaults = Settings()
    assert defaults.octopus_backend_api_url == "https://api.backend.octopus.energy/v1"
    assert defaults.happy_hour_enabled is True


@respx.mock
async def test_offered_slots_are_not_a_fallback_for_no_bookings() -> None:
    offered = {"data": {"savingSessions": {
        "events": [_event(9999, "2026-11-08T14:00:00+00:00", "2026-11-08T15:00:00+00:00")],
        "account": {"joinedEvents": []},
    }}}
    respx.post(GRAPHQL).mock(side_effect=[token_response(), httpx.Response(200, json=offered)])
    reading = await fetch_happy_hours(_settings(), now=NOW)
    assert reading.bookings == ()


@respx.mock
async def test_a_text_id_is_kept_only_when_it_is_digits() -> None:
    respx.post(GRAPHQL).mock(side_effect=[token_response(), _reply([_event("8066", *BOOKED)])])
    reading = await fetch_happy_hours(_settings(), now=NOW)
    assert reading.bookings == (_booking("8066", *BOOKED),)


@respx.mock
async def test_a_reply_nested_too_deeply_to_decode_fails_the_read() -> None:
    respx.post(GRAPHQL).mock(side_effect=[token_response(), RecursionError()])
    reading = await fetch_happy_hours(_settings(), now=NOW)
    assert reading.bookings is None
