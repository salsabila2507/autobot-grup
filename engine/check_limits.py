"""Keep the engagement checker inside the machine's limits.

Reading a thread means opening it in a browser. One check can hold four Chromium
processes for a couple of minutes, and this box has about 1 GB free once everything
else is running. So checks are serialised, capped per hour, and rate limited per user:

* one browser check at a time, whatever the concurrency
* a per-user cooldown, so a member cannot queue twenty of them
* a global hourly cap, so even admins cannot take the box down
* a queue depth limit, so requests are refused rather than silently piling up
"""

import asyncio
import time

MAX_CONCURRENT = 1
MAX_QUEUE = 5
PER_USER_COOLDOWN = 900          # 15 minutes
GLOBAL_LIMIT_PER_HOUR = 20
DEFAULT_ADMIN_COOLDOWN = 300     # 5 minutes


class Throttle(Exception):
    """Raised when a check is refused instead of being started."""


class CheckLimiter:
    def __init__(self, per_user_cooldown=PER_USER_COOLDOWN,
                 admin_cooldown=DEFAULT_ADMIN_COOLDOWN,
                 global_limit_per_hour=GLOBAL_LIMIT_PER_HOUR,
                 max_queue=MAX_QUEUE, max_concurrent=MAX_CONCURRENT, clock=time.time):
        self.per_user_cooldown = per_user_cooldown
        self.admin_cooldown = admin_cooldown
        self.global_limit_per_hour = global_limit_per_hour
        self.max_queue = max_queue
        self.clock = clock
        self._lock = asyncio.Lock()
        self._gate = asyncio.Semaphore(max_concurrent)
        self._user_seen = {}
        self._global_seen = []
        self._waiting = 0
        self._now = None

    def _prune(self, now):
        cutoff = now - 3600
        self._global_seen = [t for t in self._global_seen if t > cutoff]

    def check(self, user_id, is_admin=False, now=None):
        """Raise Throttle when this request must wait or be refused."""
        now = self.clock() if now is None else now
        self._now = now
        self._prune(now)

        if len(self._global_seen) >= self.global_limit_per_hour:
            raise Throttle(f"hourly limit reached ({self.global_limit_per_hour}/hour), try later")

        cooldown = self.admin_cooldown if is_admin else self.per_user_cooldown
        last = self._user_seen.get(user_id)
        if last is not None:
            wait = cooldown - (now - last)
            if wait > 0:
                raise Throttle(f"wait {int(wait // 60) + 1} more minute(s) before checking again")

        if self._waiting >= self.max_queue:
            raise Throttle("checker is busy, try again in a few minutes")

    def enter(self, user_id, now=None):
        now = self._now if now is None and hasattr(self, "_now") else (now or self.clock())
        self._user_seen[user_id] = now
        self._global_seen.append(now)
        self._waiting += 1

    def leave(self):
        self._waiting = max(0, self._waiting - 1)

    async def run(self, user_id, func, *args, **kwargs):
        """Run one check at a time, holding the gate for the whole read."""
        self.enter(user_id)
        try:
            async with self._gate:
                return await func(*args, **kwargs)
        finally:
            self.leave()

    def stats(self):
        return {
            "checks_last_hour": len(self._global_seen),
            "hourly_limit": self.global_limit_per_hour,
            "waiting": self._waiting,
            "max_queue": self.max_queue,
            "users_tracked": len(self._user_seen),
        }