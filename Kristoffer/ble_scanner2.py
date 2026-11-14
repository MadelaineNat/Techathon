#!/usr/bin/env python3
"""
Usage from another script:

    scanner = Scanner(on_enter=my_enter_fn, on_leave=my_leave_fn)
    await scanner.run()          # inside an asyncio program

"""

import asyncio
import inspect
import time
import traceback
import os

from dotenv import load_dotenv, dotenv_values 
from bleak import BleakScanner

load_dotenv()  # Load environment variables from .env file

MAX_ID = int(os.getenv('MAX_ID', 10))
NEAR_DBM = int(os.getenv('NEAR_DBM', -50))
LEAVE_DBM = int(os.getenv('LEAVE_DBM', -65))
WIN_MARGIN_DB = int(os.getenv('WIN_MARGIN_DB', 5))
EMA_ALPHA = float(os.getenv('EMA_ALPHA', 0.3))
STALE_S = float(os.getenv('STALE_S', 3.0))


class Scanner:

    def __init__(self, on_enter=None, on_leave=None, verbose=False):
        self.on_enter = on_enter      # called with installation id when entering a zone
        self.on_leave = on_leave      # called with installation id when leaving a zone
        self.verbose = verbose        # print the RSSI debug line twice a second

        self.active_id = 0
        self.beacons = {}
        self._tasks = set()           # keeps references to running async callbacks

    def on_advertisement(self, device, adv):
        """Called by bleak for every advertisement received."""
        name = adv.local_name or device.name
        if not name or not name.startswith("HB-"):
            return
        try:
            bid = int(name[3:])
        except ValueError:
            return
        if not 1 <= bid <= MAX_ID:
            return

        r = float(adv.rssi)
        b = self.beacons.get(bid)
        if b is None:
            self.beacons[bid] = {"rssi": r, "last": time.monotonic()}
        else:
            b["rssi"] = EMA_ALPHA * r + (1.0 - EMA_ALPHA) * b["rssi"]
            b["last"] = time.monotonic()

    def is_fresh(self, bid):
        b = self.beacons.get(bid)
        return b is not None and (time.monotonic() - b["last"]) < STALE_S

    # Callback dispatch
    def _fire(self, callback, bid):
        if callback is None:
            return
        try:
            result = callback(bid)
        except Exception:
            traceback.print_exc()
            return
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            self._tasks.add(task)
            task.add_done_callback(self._task_done)

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            traceback.print_exception(task.exception())

    # Main loop
    async def run(self):
        scanner = BleakScanner(detection_callback=self.on_advertisement)
        await scanner.start()
        print("Horn scanning... (Ctrl+C to quit)")

        last_print = 0.0
        try:
            while True:
                await asyncio.sleep(0.1)

                # Strongest and runner-up among fresh beacons
                fresh = sorted(
                    ((bid, self.beacons[bid]["rssi"]) for bid in self.beacons if self.is_fresh(bid)),
                    key=lambda x: x[1],
                    reverse=True,
                )
                best = fresh[0] if fresh else None
                second = fresh[1] if len(fresh) > 1 else None

                # Leaving the current zone (hysteresis)
                if self.active_id != 0:
                    if not self.is_fresh(self.active_id) or self.beacons[self.active_id]["rssi"] < LEAVE_DBM:
                        left = self.active_id
                        self.active_id = 0
                        self._fire(self.on_leave, left)

                # Entering a new zone
                if self.active_id == 0 and best and best[1] >= NEAR_DBM:
                    clear_winner = second is None or (best[1] - second[1] >= WIN_MARGIN_DB)
                    if clear_winner:
                        self.active_id = best[0]
                        self._fire(self.on_enter, best[0])

                # Optional debug print twice per second
                if self.verbose:
                    now = time.monotonic()
                    if now - last_print > 0.5:
                        last_print = now
                        print(" ".join(f"[HB-{bid}: {rssi:.0f} dBm]" for bid, rssi in sorted(fresh)))
        finally:
            await scanner.stop()
            for t in list(self._tasks):
                t.cancel()
            print("Scanner stopped.")