import asyncio
from collections import defaultdict

_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)


def subscribe(call_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    _subscribers[call_id].add(q)
    return q


def unsubscribe(call_id: str, q: asyncio.Queue) -> None:
    _subscribers[call_id].discard(q)
    if not _subscribers[call_id]:
        del _subscribers[call_id]


def publish(call_id: str) -> None:
    for q in _subscribers.get(call_id, ()):
        if q.empty():
            q.put_nowait(True)
