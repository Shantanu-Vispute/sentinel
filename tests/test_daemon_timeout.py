import signal
import time

import pytest

from digest.daemon import _TelegramTimeout, _telegram_timeout_handler


def test_timeout_handler_raises_telegram_timeout():
    with pytest.raises(_TelegramTimeout):
        _telegram_timeout_handler(signal.SIGALRM, None)


def test_alarm_interrupts_a_genuine_hang():
    """A blocking call (Telethon network I/O, stood in for here by
    time.sleep) that never returns on its own should still be interrupted."""
    signal.signal(signal.SIGALRM, _telegram_timeout_handler)
    signal.alarm(1)
    try:
        with pytest.raises(_TelegramTimeout):
            time.sleep(5)
    finally:
        signal.alarm(0)


def test_alarm_does_not_fire_when_work_finishes_in_time():
    signal.signal(signal.SIGALRM, _telegram_timeout_handler)
    signal.alarm(2)
    try:
        time.sleep(0.1)
    finally:
        signal.alarm(0)
    # No exception means the alarm correctly didn't fire early.
