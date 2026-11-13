"""Bounded asynchronous forwarding of push bodies over HTTP.

One class serves both hops out of a service: the gateway relays pushes to
the core (HTTPS + API key) and the core forwards to the Home Assistant
webhook.  A fixed set of worker threads drains a bounded queue: a slow or
unreachable downstream delays pushes (up to the queue depth) instead of
growing memory without bound, and pushes are dropped with a warning once
the queue is full.
"""

import logging
import queue
import threading
import urllib.error
import urllib.request

from nvr_common.helpers import redact_url

LOG = logging.getLogger(__name__)

FORWARD_TIMEOUT_S = 5.0
"""How long one downstream request may take before it is abandoned."""

FORWARD_WORKERS = 2
"""Worker threads draining the forward queue (fixed, small)."""

FORWARD_QUEUE_MAX = 16
"""Maximum queued pushes; beyond that pushes are dropped with a warning."""


class BridgeState:
    """Thread-safe forwarding status (dropped-push notes and test hooks)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_forward_error: str | None = None

    @property
    def last_forward_error(self) -> str | None:
        with self._lock:
            return self._last_forward_error

    def note_forward(self, ok: bool, error: str | None = None) -> None:
        with self._lock:
            self._last_forward_error = None if ok else error


class HttpForwarder:
    """Background forwarding of push bodies to one HTTP endpoint."""

    def __init__(
        self,
        url: str | None,
        state: BridgeState | None = None,
        *,
        headers: dict[str, str] | None = None,
        maxsize: int = FORWARD_QUEUE_MAX,
        workers: int = FORWARD_WORKERS,
        timeout: float = FORWARD_TIMEOUT_S,
    ) -> None:
        self._url = url
        self._state = state or BridgeState()
        self._headers = headers or {}
        self._timeout = timeout
        self._queue: queue.Queue[tuple[bytes, str] | None] = queue.Queue(maxsize=maxsize)
        self._workers: list[threading.Thread] = []
        if url:
            self._workers = [
                threading.Thread(target=self._work, name="forward", daemon=True)
                for _ in range(workers)
            ]
            for thread in self._workers:
                thread.start()

    @property
    def state(self) -> BridgeState:
        return self._state

    def submit(self, body: bytes, content_type: str) -> None:
        """Queue a push for forwarding; drop it (noted) when the queue is full."""
        if not self._url:
            return
        try:
            self._queue.put_nowait((body, content_type))
        except queue.Full:
            self._state.note_forward(ok=False, error="forward queue full; push dropped")
            LOG.warning("forward queue full; dropped push (%d bytes)", len(body))

    def shutdown(self) -> None:
        """Stop workers once the queue drains (best effort)."""
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                break
        for thread in self._workers:
            thread.join(timeout=self._timeout + 1.0)

    def _work(self) -> None:
        url = self._url
        if not url:
            return
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                body, content_type = item
                self._forward(url, body, content_type)
            finally:
                self._queue.task_done()

    def _forward(self, url: str, body: bytes, content_type: str) -> None:
        """POST a push body downstream (never raises)."""
        try:
            request = urllib.request.Request(
                url,
                data=body,
                headers={**self._headers, "Content-Type": content_type},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                response.read()
            self._state.note_forward(ok=True)
            LOG.debug("forwarded %d bytes to %s", len(body), redact_url(url))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            self._state.note_forward(ok=False, error=repr(exc))
            LOG.warning("forward to %s failed: %r", redact_url(url), exc)
