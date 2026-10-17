"""The checker must never be able to take the machine down.

One check holds several Chromium processes for minutes and the box has about 1 GB
free, so checks are serialised, rate limited per user and capped per hour.
"""

import asyncio

import pytest

from engine import check_limits


def test_first_check_is_allowed():
    limiter = check_limits.CheckLimiter()
    limiter.check(700)
    limiter.enter(700)
    limiter.leave()


def test_second_check_by_same_user_is_throttled():
    limiter = check_limits.CheckLimiter()
    limiter.check(700, now=1000)
    limiter.enter(700)
    limiter.leave()
    with pytest.raises(check_limits.Throttle) as exc:
        limiter.check(700, now=1010)
    assert "wait" in str(exc.value).lower()


def test_cooldown_expires():
    limiter = check_limits.CheckLimiter(per_user_cooldown=900)
    limiter.check(700, now=1000)
    limiter.enter(700)
    limiter.leave()
    limiter.check(700, now=1000 + 901)


def test_admin_cooldown_is_shorter():
    limiter = check_limits.CheckLimiter(per_user_cooldown=900, admin_cooldown=300)
    limiter.check(1, is_admin=True, now=1000)
    limiter.enter(1)
    limiter.leave()
    limiter.check(1, is_admin=True, now=1000 + 301)


def test_one_user_cannot_starve_another():
    limiter = check_limits.CheckLimiter()
    limiter.check(700, now=1000)
    limiter.enter(700)
    limiter.leave()
    limiter.check(701, now=1000)


def test_hourly_cap_refuses_everyone():
    limiter = check_limits.CheckLimiter(global_limit_per_hour=3, per_user_cooldown=0)
    for i in range(3):
        limiter.check(700 + i, now=1000)
        limiter.enter(700 + i)
        limiter.leave()
    with pytest.raises(check_limits.Throttle) as exc:
        limiter.check(999, now=1000)
    assert "hourly" in str(exc.value).lower()


def test_hourly_cap_window_slides():
    limiter = check_limits.CheckLimiter(global_limit_per_hour=2, per_user_cooldown=0)
    for i in range(2):
        limiter.check(700 + i, now=1000)
        limiter.enter(700 + i)
        limiter.leave()
    limiter.check(800, now=1000 + 3601)


def test_queue_depth_is_capped():
    limiter = check_limits.CheckLimiter(max_queue=2, per_user_cooldown=0)
    limiter._waiting = 2
    with pytest.raises(check_limits.Throttle) as exc:
        limiter.check(700, now=1000)
    assert "busy" in str(exc.value).lower()


def test_only_one_check_runs_at_a_time():
    """Serialising is what keeps Chromium count at one instead of unbounded."""
    limiter = check_limits.CheckLimiter(max_concurrent=1, per_user_cooldown=0,
                                        global_limit_per_hour=1000)
    running = []
    peak = []

    async def job(tag):
        running.append(tag)
        peak.append(len(running))
        await asyncio.sleep(0.05)
        running.remove(tag)
        return tag

    async def main():
        return await asyncio.gather(*[limiter.run(i, job, i) for i in range(6)])

    results = asyncio.run(main())
    assert sorted(results) == list(range(6))
    assert max(peak) == 1, f"checks overlapped, peak was {max(peak)}"


def test_waiting_count_returns_to_zero_after_failures():
    limiter = check_limits.CheckLimiter(per_user_cooldown=0)

    async def boom():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        asyncio.run(limiter.run(700, boom))
    assert limiter.stats()["waiting"] == 0


# --- the scroll budget has to actually reach the reader ----------------

def test_scroll_budget_is_passed_through_to_the_browser():
    """Config that is never forwarded is config that does nothing."""
    import inspect

    from engine.x_reader import comment_reader

    sig = inspect.signature(comment_reader.make_reader)
    assert sig.parameters["max_scrolls"].default == 60
    assert sig.parameters["idle_rounds"].default == 4

    src = inspect.getsource(comment_reader.make_reader)
    assert "max_scrolls=max_scrolls" in src
    assert "idle_rounds=idle_rounds" in src


def test_queue_bot_forwards_the_configured_budget():
    from pathlib import Path

    src = Path("services/queue_bot.py").read_text()
    assert "max_scrolls=xr.browser_max_scrolls" in src
    assert "idle_rounds=xr.browser_idle_rounds" in src


# --- the command must accept the correct spelling ----------------------

def test_both_spellings_of_the_check_command_are_accepted():
    from pathlib import Path

    src = Path("services/queue_bot.py").read_text()
    assert '("/check", "/chek")' in src, "both /check and /chek must dispatch"


def test_usage_message_uses_the_correct_spelling():
    from engine import text

    assert "/check " in text.USAGE_CHECK
    assert "/chek" not in text.USAGE_CHECK


def test_sync_reader_has_no_missing_helper():
    """The sync wrapper called _loop(), which no longer exists: a NameError waiting for
    the first caller that is not already on an event loop."""
    from engine.x_reader import comment_reader

    reader = comment_reader.make_reader("auth_token=x; ct0=y")
    assert hasattr(reader, "read_async")
    assert not hasattr(comment_reader, "_loop"), "the _loop helper is still defined"

    import inspect, re
    src = inspect.getsource(reader)
    # a bare _loop( call. "_loop()" alone is a substring of new_event_loop(), which is
    # the correct replacement, so match on a word boundary.
    assert not re.search(r"(?<![.\w])_loop\(", src), \
        "the sync wrapper still calls the deleted _loop helper"
