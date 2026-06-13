"""Tests for shared context extraction (model, LLM parsing, recording)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ha_spark.config import Settings
from ha_spark.context_intent import (
    ExtractedContext,
    build_extraction_messages,
    effect_factor,
    parse_llm_extraction,
    record_context,
)
from ha_spark.energy.context import ContextStore


def test_extracted_context_rejects_reversed_range() -> None:
    with pytest.raises(ValueError, match="before the start"):
        ExtractedContext(kind="away", start=date(2026, 7, 5), end=date(2026, 7, 1))


def test_effect_factor_uses_config_for_away_and_guests() -> None:
    s = Settings(away_load_factor=0.3, guests_load_factor=1.4)
    away = ExtractedContext(kind="away", start=date(2026, 7, 1), end=date(2026, 7, 1))
    guests = ExtractedContext(kind="guests", start=date(2026, 7, 1), end=date(2026, 7, 1))
    assert effect_factor(away, s) == 0.3
    assert effect_factor(guests, s) == 1.4


def test_effect_factor_defaults_usage_kinds() -> None:
    s = Settings()
    hi = ExtractedContext(kind="high_usage", start=date(2026, 7, 1), end=date(2026, 7, 1))
    lo = ExtractedContext(kind="low_usage", start=date(2026, 7, 1), end=date(2026, 7, 1))
    explicit = ExtractedContext(
        kind="high_usage", start=date(2026, 7, 1), end=date(2026, 7, 1), factor=2.0
    )
    assert effect_factor(hi, s) == 1.3
    assert effect_factor(lo, s) == 0.6
    assert effect_factor(explicit, s) == 2.0


@pytest.mark.parametrize(
    "raw,kind",
    [
        ('{"kind": "away", "start": "2026-07-01", "end": "2026-07-14"}', "away"),
        ('```json\n{"kind":"guests","start":"2026-07-01","end":"2026-07-02"}\n```', "guests"),
        ('Sure: {"kind": "away", "start": "2026-07-01", "end": "2026-07-01"} done', "away"),
    ],
)
def test_parse_llm_extraction_tolerates_wrapping(raw: str, kind: str) -> None:
    extracted = parse_llm_extraction(raw)
    assert extracted is not None
    assert extracted.kind == kind


@pytest.mark.parametrize("raw", ["null", "  null  ", "I don't know", "{not json}", ""])
def test_parse_llm_extraction_rejects_non_facts(raw: str) -> None:
    assert parse_llm_extraction(raw) is None


def test_parse_llm_extraction_rejects_bad_kind() -> None:
    raw = '{"kind": "party", "start": "2026-07-01", "end": "2026-07-02"}'
    assert parse_llm_extraction(raw) is None


def test_build_extraction_messages_embeds_today() -> None:
    msgs = build_extraction_messages("I'm away", date(2026, 6, 13))
    assert msgs[0]["role"] == "system"
    assert "2026-06-13" in msgs[0]["content"]
    assert "Saturday" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "I'm away"}


async def test_record_context_writes_and_confirms(tmp_path: Path) -> None:
    s = Settings(db_path=str(tmp_path / "test.db"), away_load_factor=0.4)
    extracted = ExtractedContext(
        kind="away", start=date(2026, 7, 1), end=date(2026, 7, 14), note="Italy"
    )
    confirmation = await record_context(s, extracted, source="ollama")

    assert "away" in confirmation
    assert "Italy" in confirmation
    assert "~40%" in confirmation
    assert "context remove 1" in confirmation

    async with ContextStore(s.db_path) as store:
        active = await store.active_on(date(2026, 7, 7))
    assert len(active) == 1
    assert active[0].kind == "away"
    assert active[0].source == "ollama"


async def test_record_context_persists_usage_factor(tmp_path: Path) -> None:
    s = Settings(db_path=str(tmp_path / "test.db"))
    extracted = ExtractedContext(
        kind="high_usage", start=date(2026, 7, 1), end=date(2026, 7, 1), factor=1.8
    )
    await record_context(s, extracted, source="ollama")
    async with ContextStore(s.db_path) as store:
        e = (await store.list_all())[0]
    assert e.factor(s) == 1.8
