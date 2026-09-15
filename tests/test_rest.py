import json

import httpx
import pytest
import respx

from ha_spark.ha.rest import HomeAssistantRest, HomeAssistantRestError

BASE = "http://ha.test/api"


@respx.mock
async def test_get_states_parses_entities() -> None:
    respx.get(f"{BASE}/states").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "entity_id": "light.kitchen",
                    "state": "on",
                    "attributes": {"friendly_name": "Kitchen Light", "brightness": 200},
                },
                {"entity_id": "sensor.temp", "state": "21.5", "attributes": {}},
            ],
        )
    )
    async with HomeAssistantRest(BASE, "token") as rest:
        states = await rest.get_states()

    assert len(states) == 2
    kitchen = states[0]
    assert kitchen.entity_id == "light.kitchen"
    assert kitchen.domain == "light"
    assert kitchen.friendly_name == "Kitchen Light"
    assert states[1].friendly_name == "sensor.temp"


@respx.mock
async def test_call_service_sends_bearer_and_returns_changed() -> None:
    route = respx.post(f"{BASE}/services/light/turn_on").mock(
        return_value=httpx.Response(
            200,
            json=[{"entity_id": "light.kitchen", "state": "on", "attributes": {}}],
        )
    )
    async with HomeAssistantRest(BASE, "secret") as rest:
        changed = await rest.call_service("light", "turn_on", {"entity_id": "light.kitchen"})

    assert changed[0].entity_id == "light.kitchen"
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer secret"


@respx.mock
async def test_set_state_posts_state_and_attributes() -> None:
    route = respx.post(f"{BASE}/states/sensor.ha_spark_target_soc").mock(
        return_value=httpx.Response(200, json={"entity_id": "sensor.ha_spark_target_soc"})
    )
    async with HomeAssistantRest(BASE, "secret") as rest:
        await rest.set_state(
            "sensor.ha_spark_target_soc", "90", attributes={"unit_of_measurement": "%"}
        )

    sent = route.calls.last.request
    assert json.loads(sent.content) == {
        "state": "90",
        "attributes": {"unit_of_measurement": "%"},
    }


@pytest.mark.parametrize("failure", ["transport", "status", "malformed"])
@respx.mock
async def test_rest_failures_never_expose_the_auth_token(failure: str, caplog) -> None:
    secret = "ha-auth-token-sentinel"
    route = respx.get(f"{BASE}/states/sensor.secret_probe")
    if failure == "transport":
        route.mock(side_effect=httpx.ConnectError(f"transport failed: {secret}"))
    elif failure == "status":
        route.mock(return_value=httpx.Response(503, text=f"upstream failed: {secret}"))
    else:
        route.mock(
            return_value=httpx.Response(
                200,
                json={
                    "entity_id": secret,
                    "state": "on",
                    "attributes": "not-an-object",
                },
            )
        )

    with caplog.at_level("WARNING"):
        async with HomeAssistantRest(BASE, secret) as rest:
            with pytest.raises(HomeAssistantRestError) as caught:
                await rest.get_state("sensor.secret_probe")

    assert secret not in caplog.text
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
