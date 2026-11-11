"""Read-only Kraken agreement fixtures for HTTP-mocked tests."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


def agreement_payload(values: tuple[float, ...] = (6.6, 6.6, 36.2828, 36.2828)) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {"data": {"account": {"electricityAgreements": [{
        "validFrom": (now - timedelta(days=30)).isoformat(),
        "validTo": None,
        "meterPoint": {"direction": "IMPORT"},
        "tariff": {"__typename": "HalfHourlyTariff", "unitRates": [
            {"validFrom": (now - timedelta(days=1)).isoformat(), "validTo": None, "value": v}
            for v in values
        ]},
    }]}}}


def token_response() -> httpx.Response:
    return httpx.Response(200, json={"data": {"obtainKrakenToken": {"token": "jwt-private"}}})


def agreement_failure(reason: str) -> httpx.Response:
    if reason == "http":
        return httpx.Response(500, text="sk_test jwt-private")
    if reason == "errors":
        return httpx.Response(200, json={"errors": [{"message": "sk_test jwt-private"}]})
    if reason == "shape":
        return httpx.Response(200, json={"data": {"account": "sk_test jwt-private"}})
    values = (1.0, 2.0, 3.0) if reason == "three" else (-1.0, 6.6)
    return httpx.Response(200, json=agreement_payload(values))
