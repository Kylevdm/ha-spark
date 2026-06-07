import httpx
import respx

from ha_agent.ha.rest import HomeAssistantRest

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
