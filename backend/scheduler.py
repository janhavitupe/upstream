"""One background timer for everything that runs on its own: payroll and invoice chasing.

While the app is awake it ticks every minute; on a sleeping free host the daily GitHub
Action calls POST /api/tick. Every job is safe to run any number of times.
"""
from __future__ import annotations

import threading

from backend import chasing, payroll


def tick_all() -> dict:
    out = {}
    for name, job in (("payroll", payroll.tick), ("chasing", chasing.tick)):
        try:
            out[name] = job()
        except Exception as e:  # one job failing must not stop the other
            out[name] = [f"error: {e}"]
            print(f"[scheduler] {name} failed: {e}")
    return out


def start(interval_seconds: int = 60) -> threading.Event:
    stop = threading.Event()

    def loop():
        delay = 5  # first run soon after startup
        while not stop.wait(delay):
            delay = interval_seconds
            tick_all()
    threading.Thread(target=loop, name="upstream-scheduler", daemon=True).start()
    return stop
