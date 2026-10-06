"""The one place Router app reads go through before reaching the network.

Every tab reads through ``RouterService``, whose ``@cached`` reads land here:
a read is reused for ``ttl`` seconds, concurrent callers asking for the same
read share one request, and any write (``@mutation``) clears everything.
``prefetch`` warms reads in the background so a tab opens on cached data.
"""

from __future__ import annotations

import copy
import functools
import inspect
from collections.abc import Callable
from concurrent.futures import Future
from threading import Lock, Thread
from time import monotonic

# Seconds a read is reused, so switching tabs doesn't refetch unchanged data.
CACHE_TTL = 60.0


class ReadCache:
    def __init__(self, ttl: float = CACHE_TTL):
        self.ttl = ttl
        self._entries: dict[tuple, tuple[float, object]] = {}
        self._in_flight: dict[tuple, Future] = {}
        self._lock = Lock()
        self._generation = 0
        self._closed = False

    def get(self, key: tuple, read: Callable[[], object]):
        """Return a copy of ``read()``, reusing a fresh or in-flight result.

        Failures are never stored, and a read that overlaps a write isn't kept.
        A read must not call itself with the same arguments: it would wait on
        its own in-flight request.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and monotonic() - entry[0] < self.ttl:
                return copy.deepcopy(entry[1])
            generation = self._generation
            # A write starts a new generation, so reads after it never join
            # a request that began before it.
            slot = (generation, key)
            flight = self._in_flight.get(slot)
            owner = flight is None
            if owner:
                flight = self._in_flight[slot] = Future()
        if not owner:
            # Callers own what they get; edits never leak back into the cache.
            return copy.deepcopy(flight.result())
        try:
            value = read()
        except BaseException as error:
            with self._lock:
                self._in_flight.pop(slot, None)
            flight.set_exception(error)
            raise
        with self._lock:
            self._in_flight.pop(slot, None)
            if generation == self._generation:
                self._entries[key] = (monotonic(), value)
        flight.set_result(value)
        return copy.deepcopy(value)

    def invalidate(self):
        with self._lock:
            self._entries.clear()
            self._generation += 1

    def prefetch(self, *reads: Callable[[], object]) -> None:
        """Run reads in the background to warm the cache; never blocks or raises.

        Each read gets a daemon thread, so quitting never waits on a preload.
        A read that fails is simply not cached; its tab loads it normally.
        """
        with self._lock:
            if self._closed:
                return
        for read in reads:
            Thread(
                target=_quietly, args=(read,), name="router-prefetch", daemon=True
            ).start()

    def close(self) -> None:
        """Start no more preloads; ones already running finish unseen."""
        with self._lock:
            self._closed = True


def _quietly(read: Callable[[], object]) -> None:
    try:
        read()
    except Exception:
        pass


def cached(method):
    """Read through the service's ``ReadCache``, keyed by method and arguments."""
    signature = inspect.signature(method)

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        key = (method.__name__, *list(bound.arguments.items())[1:])
        return self.cache.get(key, lambda: method(self, *args, **kwargs))

    return wrapper


def mutation(method):
    """Writes read fresh state to validate against and leave none stale behind."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        self.invalidate()
        try:
            return method(self, *args, **kwargs)
        finally:
            self.invalidate()

    return wrapper
