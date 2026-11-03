"""Exact native read-back, including the shipped sustained-zero confirmation rule."""
from collections.abc import Awaitable, Callable


async def confirm(
    target: list[int], read: Callable[[], Awaitable[list[int] | None]],
    sleep: Callable[[float], Awaitable[None]], *, changed: bool,
    mismatch: Callable[[], None], sustained: bool = False,
) -> bool:
    """Exact match within three tries; a sustained target adds five further reads.

    ``sustained`` lets a block whose discharge half is zero carry the all-zero
    rule without making the whole eight-word target zero.
    """
    for attempt in range(3):
        if changed or attempt:
            await sleep(1)
        if await read() == target:
            break
        mismatch()
    else:
        return False
    if not any(target) or sustained:
        for _ in range(5):
            await sleep(1)
            if await read() != target:
                mismatch()
                return False
    return True
