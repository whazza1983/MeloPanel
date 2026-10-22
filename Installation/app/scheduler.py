import asyncio
import logging
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from .db import Database
from .navidrome import Navidrome, radio_track
from .player import Player

log = logging.getLogger("schedule")

SCHEDULE_KEY = "schedule"
ACTIONS = ("favlist", "radio", "stop")
CHECK_INTERVAL = 20   # seconds


class Scheduler:
    """Schedule: at fixed times, starts a favorites list or a radio station
    on continuous loop, or stops playback.

    An entry: {"id", "enabled", "days": [0..6] (0 = Monday), "time": "HH:MM",
    "action": "favlist" | "radio" | "stop", "target": id, "name": display name, "shuffle": bool}
    """

    def __init__(self, db: Database, player: Player, navidrome: Navidrome, timezone: str):
        self.db = db
        self.player = player
        self.navidrome = navidrome
        self.tz = ZoneInfo(timezone)
        self._done: set[str] = set()   # entries already executed in the current minute

    def entries(self) -> list[dict]:
        return self.db.get_setting(SCHEDULE_KEY) or []

    def save(self, entries: list[dict]) -> list[dict]:
        for entry in entries:
            entry.setdefault("id", secrets.token_hex(4))
        entries.sort(key=lambda e: (e["time"], min(e["days"] or [7])))
        self.db.set_setting(SCHEDULE_KEY, entries)
        return entries

    def now(self) -> datetime:
        return datetime.now(self.tz)

    async def run(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("Scheduler error")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _tick(self) -> None:
        now = self.now()
        minute = now.strftime("%Y-%m-%d %H:%M")
        self._done = {key for key in self._done if key.startswith(minute)}
        for entry in self.entries():
            key = f"{minute} {entry.get('id')}"
            if (not entry.get("enabled", True) or key in self._done
                    or now.weekday() not in entry.get("days", []) or entry.get("time") != now.strftime("%H:%M")):
                continue
            self._done.add(key)
            await self.execute(entry)

    async def execute(self, entry: dict) -> None:
        action = entry.get("action")
        name = entry.get("name") or ""
        try:
            if action == "favlist":
                await self.player.start_program("favlist", str(entry["target"]), "Schedule",
                                                bool(entry.get("shuffle")))
            elif action == "radio":
                station = await self.navidrome.radio(str(entry["target"]))
                await self.player.start_program("radio", station["id"], "Schedule",
                                                radio_track=radio_track(station))
                name = station["name"]
            elif action == "stop":
                await self.player.stop()
            else:
                return
        except Exception as exc:
            log.warning("Schedule entry '%s' (%s) failed: %s", name, action, exc)
            self.db.log_activity("Schedule", "system", f"schedule_failed:{action}", f"{name} – {exc}")
            return
        log.info("Schedule: %s %s", action, name)
        self.db.log_activity("Schedule", "system", f"schedule:{action}", name)
