"""Driver registry: driver name -> Device class, populated by @register."""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

_REGISTRY: dict[str, type] = {}
T = TypeVar("T")


def register(name: str) -> Callable[[type[T]], type[T]]:
    def deco(cls: type[T]) -> type[T]:
        if name in _REGISTRY:
            raise ValueError(f"driver {name!r} already registered")
        _REGISTRY[name] = cls
        return cls

    return deco


def lookup(driver: str) -> type:
    try:
        return _REGISTRY[driver]
    except KeyError:
        raise ValueError(
            f"unknown driver {driver!r}; registered: {sorted(_REGISTRY)}"
        ) from None
