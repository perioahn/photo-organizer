"""SSE 브로커: 백그라운드 스레드 → asyncio 큐 → 클라이언트."""
import asyncio
import json
import threading
import time

_loop: asyncio.AbstractEventLoop | None = None
_subscribers: set[asyncio.Queue] = set()
_lock = threading.Lock()
_last_disconnect = time.monotonic()


def client_count() -> int:
    with _lock:
        return len(_subscribers)


def idle_seconds() -> float:
    """마지막 클라이언트가 떠난 뒤 경과 시간 (클라이언트 있으면 0)."""
    with _lock:
        if _subscribers:
            return 0.0
        return time.monotonic() - _last_disconnect


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def publish(event: str, data: dict) -> None:
    """스레드 어디서든 호출 가능."""
    if _loop is None:
        return
    msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    with _lock:
        subs = list(_subscribers)

    def _put():
        for q in subs:
            if q.qsize() < 500:  # 느린 클라이언트 보호
                q.put_nowait(msg)

    _loop.call_soon_threadsafe(_put)


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    global _last_disconnect
    with _lock:
        _subscribers.discard(q)
        if not _subscribers:
            _last_disconnect = time.monotonic()
