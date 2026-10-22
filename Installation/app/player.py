import asyncio
import logging
import random
import time
from typing import Awaitable, Callable

import discord

from .db import Database
from .navidrome import Navidrome, is_stream_url

log = logging.getLogger("player")

LOOP_MODES = ("off", "track", "queue")
HISTORY_LIMIT = 50
ICY_INTERVAL = 20         # seconds between two polls of the radio song title
RADIO_RETRY_DELAY = 15    # delay before a dropped radio stream is restarted
STATE_KEY = "player_state"
# Evens out loud and quiet tracks in real time (EBU R128, -16 LUFS like streaming services)
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"


class Player:
    """Queue and playback for one Discord server.

    All methods run in the asyncio event loop; the bot and the web panel share the same instance.

    Besides the queue there is an optional nonstop program (``program``): a
    favorites list or a radio station that loops continuously whenever the queue
    is empty. Tracks in the queue always take precedence.
    """

    def __init__(self, client: discord.Client, navidrome: Navidrome, guild_id: int,
                 db: Database | None = None, default_volume: int = 50, idle_minutes: int = 0):
        self.client = client
        self.navidrome = navidrome
        self.guild_id = guild_id
        self.db = db
        self.idle_minutes = idle_minutes

        self.queue: list[dict] = []
        self.history: list[dict] = []
        self.current: dict | None = None
        self.program: dict | None = None
        self.volume = default_volume
        self.loop_mode = "off"
        self.normalize = True       # loudness normalization (applies from the next track)

        self._channel_id: int | None = None   # channel the bot is supposed to be in
        self._source: discord.PCMVolumeTransformer | None = None
        self._play_id = 0
        self._skip_requested = False
        self._offset = 0.0          # start position of the current stream (when seeking)
        self._started_at = 0.0      # time.monotonic() at start (adjusted for pauses)
        self._paused_at: float | None = None
        self._idle_since: float | None = None
        self._listeners: list[Callable[[dict], Awaitable[None]]] = []
        self._lock = asyncio.Lock()
        self._icy_task: asyncio.Task | None = None
        self._persist_enabled = False   # only save after restoring
        self._frozen = False            # never save again after shutdown
        self._missing_checks = 0        # how often the watchdog found the bot missing from the channel
        self._stall_checks = 0          # how often the watchdog found it "connected but silent"

    # ---------- Helpers ----------

    @property
    def guild(self) -> discord.Guild | None:
        return self.client.get_guild(self.guild_id)

    @property
    def voice(self) -> discord.VoiceClient | None:
        guild = self.guild
        return guild.voice_client if guild else None  # type: ignore[return-value]

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def listeners(self, channel=None) -> int:
        """Number of humans in a voice channel (excluding bots).

        Counts via voice states instead of channel.members: without the privileged
        members intent the bot does not know all members, but it does know the voice states.
        """
        if channel is None:
            vc = self.voice
            channel = vc.channel if vc and vc.is_connected() else None
        if channel is None:
            return 0
        own_id = self.client.user.id if self.client.user else None
        count = 0
        for user_id in channel.voice_states:
            if user_id == own_id:
                continue
            member = channel.guild.get_member(user_id)
            if member is None or not member.bot:
                count += 1
        return count

    def position(self) -> float:
        if not self.current:
            return 0.0
        now = self._paused_at if self._paused_at is not None else time.monotonic()
        return max(0.0, self._offset + now - self._started_at)

    def add_listener(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def notify(self) -> None:
        state = self.state()
        for callback in list(self._listeners):
            asyncio.create_task(callback(state))
        self._persist()

    def program_info(self) -> dict | None:
        p = self.program
        if not p:
            return None
        info = {"type": p["type"], "id": p["id"], "name": p["name"], "by": p["by"]}
        if p["type"] == "favlist":
            info.update(shuffle=p["shuffle"], round=p["round"], total=len(p["order"]),
                        index=p["pos"] if self.current and self.current.get("fromProgram") else None)
        return info

    def state(self) -> dict:
        vc = self.voice
        channel = vc.channel if vc and vc.is_connected() else None
        return {
            "connected": channel is not None,
            "channel": {"id": str(channel.id), "name": channel.name} if channel else None,
            "current": self.current,
            "position": round(self.position(), 1),
            "paused": self.paused,
            "volume": self.volume,
            "loop": self.loop_mode,
            "queue": self.queue,
            "program": self.program_info(),
            "history": self.history[::-1],   # newest first
        }

    # ---------- Save & restore ----------

    def _persist(self) -> None:
        if not self._persist_enabled or not self.db:
            return
        current = self.current
        if current and current.get("fromProgram"):
            current = None   # restored from the nonstop program instead
        data = {
            "channel_id": self._channel_id,
            "volume": self.volume,
            "loop": self.loop_mode,
            "program": self.program,
            "program_current": bool(self.current and self.current.get("fromProgram")),
            "queue": ([current] if current else []) + self.queue,
        }
        try:
            self.db.set_setting(STATE_KEY, data)
        except Exception:
            log.exception("Could not save the player state")

    def freeze_state(self) -> None:
        """Save the last state and stop saving afterwards (on shutdown)."""
        if self._persist_enabled:
            self._persist()
        self._persist_enabled = False
        self._frozen = True

    async def restore(self) -> None:
        """Restore channel, nonstop program and queue after a restart."""
        if self._persist_enabled or self._frozen:
            return
        try:
            data = self.db.get_setting(STATE_KEY) if self.db else None
            if not data:
                return
            self.volume = int(data.get("volume", self.volume))
            self.loop_mode = data.get("loop", "off") if data.get("loop") in LOOP_MODES else "off"
            self.queue = [t for t in data.get("queue") or [] if isinstance(t, dict)]
            self.program = data.get("program")
            if self.program and self.program.get("type") == "favlist" and data.get("program_current"):
                # Replay the track that was playing at restart from the beginning
                self.program["pos"] = max(0, self.program.get("pos", 1) - 1)

            channel_id = data.get("channel_id")
            if not channel_id:
                return
            # From here on this is the target channel; if connecting fails now,
            # the watchdog (keep_alive) retries every 30 seconds.
            self._channel_id = channel_id
            log.info("Rejoining the voice channel after restart.")
            await self._reconnect()
        except Exception:
            log.exception("Restoring after restart failed")
        finally:
            if not self._frozen:
                self._persist_enabled = True
                self.notify()

    # ---------- Voice channel ----------

    async def connect(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        vc = self.voice
        if vc and vc.is_connected():
            if vc.channel.id != channel.id:
                await vc.move_to(channel)
        else:
            await channel.connect(self_deaf=True)
        self._channel_id = channel.id
        self._idle_since = time.monotonic() if not self.current else None
        self.notify()

    async def disconnect(self) -> None:
        self._channel_id = None
        vc = self.voice
        self._play_id += 1
        if vc:
            vc.stop()
            await vc.disconnect(force=True)
        if self.current:
            self._push_history(self.current)
        self.current = None
        self._source = None
        self._paused_at = None
        self.notify()

    def voice_moved(self, channel_id: int | None) -> None:
        """Called when the bot's voice channel is changed externally.

        Moved: the new channel becomes the target channel. Disconnected (network issue, Discord
        outage or kick): the target channel stays and the watchdog reconnects. Only "Leave" in
        the panel or /leave (disconnect()) removes the bot permanently.
        """
        if channel_id is not None and self._channel_id is not None:
            self._channel_id = channel_id
        elif channel_id is None and self._channel_id is not None:
            log.warning("Bot was disconnected from the voice channel – the watchdog will reconnect shortly.")
        self.notify()

    # ---------- Queue ----------

    async def add(self, tracks: list[dict], added_by: str, mode: str = "end") -> None:
        """mode: 'end' = append, 'next' = play next, 'now' = play immediately."""
        tracks = [{k: v for k, v in t.items() if k not in ("fromProgram", "played_at")} | {"added_by": added_by}
                  for t in tracks]
        if not tracks:
            return
        if mode in ("next", "now"):
            self.queue[0:0] = tracks
        else:
            self.queue.extend(tracks)

        # A radio station never ends on its own: queued tracks interrupt it immediately
        # (as a nonstop program it resumes afterwards, otherwise it is over)
        radio_playing = bool(self.current and self.current.get("radio"))
        if self.current and (mode == "now" or radio_playing):
            await self.skip()
        elif not self.current:
            await self._play_next()
        else:
            self.notify()

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.queue):
            self.queue.pop(index)
            self.notify()

    def move(self, src: int, dst: int) -> None:
        if 0 <= src < len(self.queue) and 0 <= dst < len(self.queue):
            self.queue.insert(dst, self.queue.pop(src))
            self.notify()

    def clear(self) -> None:
        self.queue.clear()
        self.notify()

    def shuffle(self) -> None:
        random.shuffle(self.queue)
        self.notify()

    async def jump(self, index: int) -> None:
        """Jump to a track in the queue (tracks before it are skipped)."""
        if 0 <= index < len(self.queue):
            skipped = self.queue[:index]
            del self.queue[:index]
            if self.loop_mode == "queue":
                self.queue.extend(skipped)
            await self.skip()

    # ---------- Nonstop program ----------

    async def start_program(self, kind: str, item_id: str, by: str, shuffle: bool = False,
                            radio_track: dict | None = None) -> None:
        """Start a favorites list or a radio station on continuous loop."""
        if kind == "favlist":
            fav = self.db.get_favlist(int(item_id)) if self.db else None
            if not fav or not fav["songs"]:
                raise ValueError("The favorites list is empty or doesn't exist")
            order = [s["id"] for s in fav["songs"]]
            if shuffle:
                random.shuffle(order)
            self.program = {"type": "favlist", "id": str(fav["id"]), "name": fav["name"], "by": by,
                            "shuffle": shuffle, "order": order, "pos": 0, "round": 1, "last": None}
        elif kind == "radio" and radio_track:
            self.program = {"type": "radio", "id": radio_track["radioId"], "name": radio_track["title"],
                            "by": by, "track": radio_track}
        else:
            raise ValueError("Unknown nonstop program")
        log.info("Nonstop program started: %s (%s)", self.program["name"], kind)

        track = self._program_next()
        if track:
            await self._start(track)
        else:
            self.notify()

    async def stop_program(self) -> None:
        if not self.program:
            return
        log.info("Nonstop program stopped: %s", self.program["name"])
        self.program = None
        # A playing radio station stops immediately; a song may finish playing
        if self.current and self.current.get("fromProgram") and self.current.get("radio"):
            await self.skip()
        else:
            self.notify()

    def set_program_shuffle(self, shuffle: bool) -> None:
        p = self.program
        if not p or p["type"] != "favlist" or p["shuffle"] == shuffle:
            return
        p["shuffle"] = shuffle
        played, rest = p["order"][:p["pos"]], p["order"][p["pos"]:]
        if shuffle:
            random.shuffle(rest)
        else:
            # Back to the saved order, excluding tracks already played this round
            fav = self.db.get_favlist(int(p["id"])) if self.db else None
            done = set(played)
            rest = [s["id"] for s in fav["songs"] if s["id"] not in done] if fav else rest
        p["order"] = played + rest
        self.notify()

    def _program_next(self) -> dict | None:
        p = self.program
        if not p:
            return None
        if p["type"] == "radio":
            return {**p["track"], "fromProgram": True, "added_by": p["by"], "nowPlaying": None}

        fav = self.db.get_favlist(int(p["id"])) if self.db else None
        if not fav or not fav["songs"]:
            log.info("Favorites list '%s' is empty or deleted – nonstop playback stopped.", p["name"])
            self.program = None
            return None
        p["name"] = fav["name"]
        songs = {s["id"]: s for s in fav["songs"]}

        for _ in range(2):   # rest of the current round, otherwise a new round
            while p["pos"] < len(p["order"]):
                song_id = p["order"][p["pos"]]
                p["pos"] += 1
                if song_id in songs:   # skip tracks removed in the meantime
                    p["last"] = song_id
                    return {**songs[song_id], "fromProgram": True, "added_by": p["by"]}
            # New round, with all tracks currently in the list
            order = list(songs)
            if p["shuffle"]:
                random.shuffle(order)
                if len(order) > 1 and order[0] == p.get("last"):
                    order.append(order.pop(0))   # avoid the same track twice in a row
            p["order"], p["pos"] = order, 0
            p["round"] += 1
        return None

    # ---------- Controls ----------

    def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, int(volume)))
        if self._source:
            self._source.volume = self.volume / 100
        self.notify()

    def set_loop(self, mode: str) -> None:
        if mode in LOOP_MODES:
            self.loop_mode = mode
            self.notify()

    def pause(self) -> None:
        vc = self.voice
        if vc and vc.is_playing():
            vc.pause()
            self._paused_at = time.monotonic()
            self.notify()

    def resume(self) -> None:
        vc = self.voice
        if vc and vc.is_paused() and self._paused_at is not None:
            vc.resume()
            self._started_at += time.monotonic() - self._paused_at
            self._paused_at = None
            self.notify()

    async def skip(self) -> None:
        vc = self.voice
        if self.current and vc and (vc.is_playing() or vc.is_paused()):
            self._skip_requested = True
            vc.stop()  # triggers _after -> next track
        else:
            self._skip_requested = True
            await self._play_next()

    async def previous(self) -> None:
        if not self.history:
            if self.current:
                await self.seek(0)
            return
        # Within the first 5 seconds go to the previous track, otherwise restart the track
        if self.current and self.position() > 5:
            await self.seek(0)
            return
        track = {k: v for k, v in self.history.pop().items() if k != "played_at"}
        if self.current and not self.current.get("fromProgram"):
            self.queue.insert(0, self.current)
        await self._start(track)

    async def seek(self, seconds: float) -> None:
        if not self.current or self.current.get("radio"):
            return
        duration = self.current.get("duration") or 0
        seconds = max(0.0, float(seconds))
        if duration:
            seconds = min(seconds, max(0.0, duration - 1))
        await self._start(self.current, seconds)

    async def stop(self) -> None:
        """Stop everything: playback, queue and nonstop program."""
        self.queue.clear()
        self.program = None
        self._play_id += 1
        vc = self.voice
        if vc:
            vc.stop()
        if self.current:
            self._push_history(self.current)
        self.current = None
        self._source = None
        self._paused_at = None
        self._idle_since = time.monotonic()
        self.notify()

    # ---------- Playback internals ----------

    def _push_history(self, track: dict) -> None:
        # Each track appears only once in the history; the newest entry wins
        self.history = [t for t in self.history if t["id"] != track["id"]]
        entry = {k: v for k, v in track.items() if k != "fromProgram"}
        self.history.append({**entry, "played_at": int(time.time())})
        del self.history[:-HISTORY_LIMIT]

    def clear_history(self) -> None:
        self.history.clear()
        self.notify()

    def _set_idle(self) -> None:
        self.current = None
        self._source = None
        self._paused_at = None
        self._idle_since = time.monotonic()
        self.notify()

    async def _play_next(self) -> None:
        async with self._lock:
            finished = self.current
            skipped = self._skip_requested
            self._skip_requested = False

            if finished and self.loop_mode == "track" and not skipped:
                await self._start(finished)
                return
            if finished:
                self._push_history(finished)
                if self.loop_mode == "queue" and not finished.get("fromProgram"):
                    self.queue.append(finished)

            if self.queue:
                await self._start(self.queue.pop(0))
                return

            if not self.program:
                self._set_idle()
                return

            # Radio stream dropped (not skipped) after only a short time:
            # reconnect after a delay instead of hammering the station every second
            if (finished and finished.get("radio") and finished.get("fromProgram") and not skipped
                    and time.monotonic() - self._started_at < 30):
                log.warning("Radio stream '%s' dropped – retrying in %ss.",
                            finished.get("title"), RADIO_RETRY_DELAY)
                self._set_idle()
                token = self._play_id
                asyncio.get_running_loop().call_later(RADIO_RETRY_DELAY, self._retry_program, token)
                return

            track = self._program_next()
            if track:
                await self._start(track)
            else:
                self._set_idle()

    def _retry_program(self, token: int) -> None:
        # Only if nothing else has been started since
        if token == self._play_id and self.current is None and self.program:
            asyncio.create_task(self._play_next())

    async def _start(self, track: dict, seek: float = 0.0) -> None:
        vc = self.voice
        if not vc or not vc.is_connected():
            # No voice channel: requests stay at the front of the queue,
            # tracks from the nonstop program are taken from it again next time
            if track.get("fromProgram"):
                if self.program and self.program["type"] == "favlist":
                    self.program["pos"] = max(0, self.program["pos"] - 1)
            else:
                self.queue.insert(0, track)
            self.current = None
            self.notify()
            return

        self._play_id += 1
        play_id = self._play_id
        if vc.is_playing() or vc.is_paused():
            vc.stop()

        before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
        if track.get("radio"):
            url = track.get("url", "")
            if not is_stream_url(url):
                log.error("Invalid stream URL for %s", track.get("title"))
                if track.get("fromProgram"):
                    self.program = None   # otherwise the program would retry it endlessly
                # Treat as skipped so that "repeat track" does not restart it endlessly
                self.current = track
                self._skip_requested = True
                asyncio.get_running_loop().call_soon(self._on_track_end, play_id)
                return
            before += " -reconnect_on_network_error 1"
            track["nowPlaying"] = None
        else:
            url = self.navidrome.stream_url(track["id"])
            if seek > 0:
                before += f" -ss {seek:.2f}"
        options = f"-vn -af {LOUDNORM_FILTER}" if self.normalize else "-vn"
        audio = discord.FFmpegPCMAudio(url, before_options=before, options=options)
        self._source = discord.PCMVolumeTransformer(audio, volume=self.volume / 100)

        # Only count real starts, not seeking or resuming after a reconnect
        if track is not self.current and self.db:
            try:
                self.db.record_play(track)
            except Exception:
                log.exception("Could not record the play for the statistics")

        loop = asyncio.get_running_loop()

        def after(error: Exception | None) -> None:
            if error:
                log.error("Playback error for %s: %s", track.get("title"), error)
            loop.call_soon_threadsafe(self._on_track_end, play_id)

        self.current = track
        self._offset = seek
        self._started_at = time.monotonic()
        self._paused_at = None
        self._idle_since = None
        vc.play(self._source, after=after)
        log.info("Playing: %s – %s", track.get("artist"), track.get("title"))
        self._watch_radio(track, play_id)
        self.notify()

    def _watch_radio(self, track: dict, play_id: int) -> None:
        if self._icy_task and not self._icy_task.done():
            self._icy_task.cancel()
        self._icy_task = None
        if track.get("radio"):
            self._icy_task = asyncio.create_task(self._icy_loop(track, play_id))

    async def _icy_loop(self, track: dict, play_id: int) -> None:
        """Periodically poll which song is currently playing on the radio."""
        while play_id == self._play_id and self.current is track:
            title = await self.navidrome.icy_title(track["url"])
            if play_id != self._play_id or self.current is not track:
                return
            if title != track.get("nowPlaying"):
                track["nowPlaying"] = title
                self.notify()
            await asyncio.sleep(ICY_INTERVAL)

    def _on_track_end(self, play_id: int) -> None:
        # Ignore stale callbacks (after seek/previous/stop)
        if play_id != self._play_id:
            return
        asyncio.create_task(self._play_next())

    # ---------- Background ----------

    async def _reconnect(self) -> bool:
        """Connect to the target channel and resume the queue or continuous loop."""
        guild = self.guild
        channel = guild.get_channel(self._channel_id) if guild and self._channel_id else None
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            if guild:   # server known but channel gone -> don't keep retrying forever
                log.warning("Voice channel %s no longer exists.", self._channel_id)
                self._channel_id = None
                self.notify()
            return False
        # Clean up a half-established or hanging connection first, otherwise
        # discord.py reports "Already connected to a voice channel"
        vc = self.voice
        if vc and not vc.is_connected():
            log.info("Cleaning up stale voice connection.")
            await self._drop_voice(vc)
        try:
            await self.connect(channel)
        except Exception as exc:
            log.warning("Connecting to '%s' failed (%s) – retrying in 30 seconds.",
                        channel.name, exc)
            vc = self.voice
            if vc and not vc.is_connected():
                await self._drop_voice(vc)
            return False
        log.info("Connected to '%s'.", channel.name)
        vc = self.voice
        if self.current and vc and not (vc.is_playing() or vc.is_paused()):
            # Resume the interrupted track at the same position (radio: restart)
            track = self.current
            await self._start(track, 0.0 if track.get("radio") else self.position())
        elif not self.current and (self.queue or self.program):
            await self._play_next()
        return True

    async def _drop_voice(self, vc: discord.VoiceClient) -> None:
        try:
            await vc.disconnect(force=True)
        except Exception as exc:
            log.debug("Voice connection cleanup: %s", exc)
        await asyncio.sleep(1)

    def _is_stalled(self, vc: discord.VoiceClient) -> bool:
        """Connected, but nothing is playing although something should be."""
        if self.paused or not self._persist_enabled:
            return False
        if self.current:
            return not (vc.is_playing() or vc.is_paused())
        return bool(self.queue or self.program)

    def health(self) -> dict:
        """Status for external monitoring (e.g. Uptime Kuma). Problems only count once the watchdog
        has seen them at least once, so brief Discord reconnects do not trigger an alert."""
        vc = self.voice
        connected = bool(vc and vc.is_connected())
        problems = []
        if self._channel_id and not connected and self._missing_checks >= 1:
            problems.append("not_in_voice")
        if connected and self._stall_checks >= 1:
            problems.append("playback_stalled")
        return {
            "connected": connected,
            "should_be_connected": bool(self._channel_id),
            "playing": bool(self.current and vc and vc.is_playing()),
            "paused": self.paused,
            "problems": problems,
        }

    async def keep_alive(self) -> None:
        """Watchdog: keeps the bot in the target channel, restarts stalled playback
        and optionally leaves the channel when idle."""
        while True:
            await asyncio.sleep(30)
            try:
                await self._watch_once()
            except Exception:
                log.exception("Error in background watchdog")

    async def _watch_once(self) -> None:
        vc = self.voice
        if self._channel_id and self._persist_enabled and not (vc and vc.is_connected()):
            # Only act on the second check: discord.py often reconnects by itself
            self._stall_checks = 0
            self._missing_checks += 1
            if self._missing_checks >= 2:
                log.warning("Bot is not in the voice channel although it should be – reconnecting.")
                if await self._reconnect():
                    self._missing_checks = 0
            return
        self._missing_checks = 0

        if vc and vc.is_connected() and self._is_stalled(vc):
            self._stall_checks += 1
            if self._stall_checks >= 2:
                log.warning("Playback is stuck – restarting it.")
                self._stall_checks = 0
                self._skip_requested = True   # don't treat as "repeat track"
                await self._play_next()
        else:
            self._stall_checks = 0

        await self._idle_check()

    async def _idle_check(self) -> None:
        """Leave the voice channel after a period without music (only if configured)."""
        if self.idle_minutes <= 0:
            return
        vc = self.voice
        if not vc or not vc.is_connected():
            return
        # Being alone in the channel also counts as idle
        alone = self.listeners() == 0
        if self.current and not self.paused and not alone:
            self._idle_since = None
            return
        if self._idle_since is None:
            self._idle_since = time.monotonic()
        elif time.monotonic() - self._idle_since > self.idle_minutes * 60:
            log.info("No activity – leaving the voice channel.")
            self._idle_since = None
            await self.disconnect()
