"""Test helpers for owner-only ingress routes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from ha_spark.api.server import AppState, build_app

# What HA's ingress proxy and ha-spark's own page send with an owner's config save.
OWNER_CONFIG_HEADERS = {"X-Remote-User-Id": "owner", "X-HA-Spark-Request": "config"}


def owner_client(state: AppState) -> TestClient:
    """A client on the ingress app (peer = TestClient's ``testclient``) saving as an owner."""
    return TestClient(build_app(state, ingress_peer="testclient"), headers=OWNER_CONFIG_HEADERS)
