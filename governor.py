"""Keeps this app's traffic to Apple modest and backs off when Apple objects.

Windows can turn one user action into hundreds of file-system calls, and each
uncached call becomes a request to iCloud. Sustained bursts, and continuing to
send requests after Apple has refused them, are the behaviours most likely to
get an account temporarily locked.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)

# Sign-in endpoints answer with these codes for ordinary reasons (a mistyped
# verification code, say), so their replies are not treated as refusals.
SIGN_IN_HOSTS = ("idmsa.apple.com", "setup.icloud.com")
SESSION_EXPIRED = {401, 421, 450}
SLOW_DOWN = {429, 503}


def _describe(url: str) -> str:
    """Name a request for the log without recording file names or tokens."""
    parts = urlsplit(url or "")
    host = parts.hostname or "?"
    if host.endswith(".icloud.com"):
        # Apple's own service calls end in a fixed operation name.
        return f"{host} {parts.path.rstrip('/').rsplit('/', 1)[-1]}"
    return host


class ICloudPaused(RuntimeError):
    """Raised instead of contacting Apple while requests are paused."""


class RequestGovernor:
    RATE = 5.0          # sustained requests per second
    BURST = 30          # requests allowed in a burst before throttling starts
    FIRST_PAUSE = 30.0  # seconds; doubles with each refusal in a row
    LONGEST_PAUSE = 15 * 60.0
    FORBIDDEN_LIMIT = 3  # consecutive 403 replies before pausing
    CONNECT_TIMEOUT = 3.0   # seconds to wait for each server address to answer
    READ_TIMEOUT = 300.0    # seconds of silence tolerated once connected
    SLOW_REQUEST = 3.0      # requests at least this long are noted in the log

    def __init__(self, on_pause: Callable[[str, bool], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._on_pause, self._clock, self._sleep = on_pause, clock, sleep
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._tokens, self._refilled = float(self.BURST), self._clock()
            self._blocked_until, self._refusals, self._forbidden = 0.0, 0, 0
            self._reason = ""
            self.needs_sign_in = False

    @property
    def paused_for(self) -> float:
        return max(0.0, self._blocked_until - self._clock())

    def install(self, session) -> None:
        """Route every request the pyicloud session sends through this governor."""
        send = session.send

        def governed_send(request, **kwargs):
            self.before()
            if kwargs.get("timeout") is None:
                # Without a limit, one unresponsive Apple server address holds a
                # request for Windows' full 21-second connection timeout before
                # the next address is tried.
                kwargs["timeout"] = (self.CONNECT_TIMEOUT, self.READ_TIMEOUT)
            started = self._clock()
            try:
                response = send(request, **kwargs)
            except Exception as exc:
                LOGGER.warning("Request to %s failed after %.1fs: %s", _describe(request.url),
                               self._clock() - started, type(exc).__name__)
                raise
            elapsed = self._clock() - started
            if elapsed >= self.SLOW_REQUEST:
                LOGGER.info("Slow request: %s took %.1fs (HTTP %s)", _describe(request.url),
                            elapsed, response.status_code)
            self.after(response.status_code, request.url)
            return response

        session.send = governed_send

    def before(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._blocked_until:
                raise ICloudPaused(self._reason)
            self._tokens = min(float(self.BURST),
                               self._tokens + (now - self._refilled) * self.RATE)
            self._refilled = now
            self._tokens -= 1
            wait = -self._tokens / self.RATE if self._tokens < 0 else 0.0
        if wait > 0:
            self._sleep(wait)

    def after(self, status: int, url: str = "") -> None:
        if (urlsplit(url).hostname or "").endswith(SIGN_IN_HOSTS):
            return
        with self._lock:
            if status < 400:
                self._refusals = self._forbidden = 0
                return
            if status == 403:
                self._forbidden += 1
                if self._forbidden < self.FORBIDDEN_LIMIT:
                    return
            elif status not in SESSION_EXPIRED and status not in SLOW_DOWN:
                return
            self._refusals += 1
            self._forbidden = 0
            pause = min(self.LONGEST_PAUSE, self.FIRST_PAUSE * 2 ** (self._refusals - 1))
            if status in SESSION_EXPIRED:
                self.needs_sign_in = True
                pause = self.LONGEST_PAUSE
                self._reason = ("iCloud is asking you to sign in again. Requests are paused "
                                "until you do.")
            else:
                self._reason = (f"iCloud refused a request (HTTP {status}). Requests are "
                                f"paused for {int(pause)} seconds to protect your account.")
            self._blocked_until = self._clock() + pause
            reason, needs_sign_in = self._reason, self.needs_sign_in
        LOGGER.warning("Pausing iCloud requests for %.0fs after HTTP %s", pause, status)
        if self._on_pause:
            try:
                self._on_pause(reason, needs_sign_in)
            except Exception:
                LOGGER.exception("Could not report the pause")
