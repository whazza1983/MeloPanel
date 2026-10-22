import asyncio
import logging
import random

import discord
from discord import app_commands

from .config import Config
from .db import Database
from .i18n import BotTranslator, bt, lang_of, translate_error
from .navidrome import Navidrome, NavidromeError, radio_track
from .player import Player
from .scheduler import Scheduler

log = logging.getLogger("bot")

SETTINGS_KEY = "bot_settings"

# Groups for display in the panel
COMMAND_CATEGORIES = {
    "play": "play", "album": "play", "playlist": "play", "favorites": "play", "radio": "play", "random": "play",
    "nonstop": "nonstop",
    "join": "control", "leave": "control", "skip": "control", "previous": "control", "pause": "control",
    "resume": "control", "stop": "control", "volume": "control", "loop": "control", "shuffle": "control",
    "seek": "control",
    "nowplaying": "info", "queue": "info", "panel": "info",
}

# Default: everything is DJ-only (or server admins, as long as no DJ role is set);
# only what is playing now and what comes next is open to everyone
PUBLIC_COMMANDS = ("nowplaying", "queue")
DEFAULT_DJ_COMMANDS = [name for name in COMMAND_CATEGORIES if name not in PUBLIC_COMMANDS]


def default_settings() -> dict:
    return {"disabled": [], "dj_role_id": None, "dj_commands": list(DEFAULT_DJ_COMMANDS), "channel_id": None,
            "normalize": True, "presence": True}


def presence_text(state: dict) -> str:
    """Text for the Discord status ("Listening to …")."""
    track = state.get("current")
    if not track:
        return "Navidrome"
    if track.get("radio"):
        text = f"📻 {track['title']}"
        if track.get("nowPlaying"):
            text += f" · {track['nowPlaying']}"
    else:
        text = f"{track.get('title', '?')} – {track.get('artist', '?')}"
    if state.get("paused"):
        text = f"⏸ {text}"
    return text if len(text) <= 128 else text[:125] + "..."


class GuardedTree(app_commands.CommandTree):
    """Checks the panel settings before every command."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.client.check_command_allowed(interaction)


def fmt_time(seconds: float) -> str:
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def track_line(track: dict) -> str:
    if track.get("radio"):
        return f"📻 **{track['title']}** `LIVE`"
    return f"**{track['title']}** – {track['artist']} `{fmt_time(track['duration'])}`"


def choice_name(text: str) -> str:
    return text if len(text) <= 100 else text[:97] + "..."


def L(interaction: discord.Interaction) -> str:
    """The user's language ('de' or 'en'), taken from their Discord client."""
    return lang_of(interaction.locale)


class MusicBot(discord.Client):
    def __init__(self, config: Config, navidrome: Navidrome, db: Database):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(intents=intents)
        self.config = config
        self.navidrome = navidrome
        self.db = db
        self.settings = {**default_settings(), **(db.get_setting(SETTINGS_KEY) or {})}
        self._sync_lock = asyncio.Lock()
        self.tree = GuardedTree(self)
        self.player = Player(self, navidrome, config.guild_id, db=db,
                             default_volume=config.default_volume,
                             idle_minutes=config.idle_disconnect_minutes)
        self.player.normalize = self.settings["normalize"]
        self.scheduler = Scheduler(db, self.player, navidrome, config.timezone)
        self._restored = False
        self._presence_shown: str | None = None
        self._presence_task: asyncio.Task | None = None
        self.player.add_listener(self._on_player_state)
        self.tree.on_error = self.on_command_error
        register_commands(self)

    async def setup_hook(self) -> None:
        await self.tree.set_translator(BotTranslator())
        await self.sync_commands()
        self.loop.create_task(self.player.keep_alive())
        self.loop.create_task(self.scheduler.run())
        self.loop.create_task(self._housekeeping())

    async def _housekeeping(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.db.prune)
            except Exception:
                log.exception("Database cleanup failed")
            await asyncio.sleep(24 * 3600)

    # ---------- Discord status ----------

    async def _on_player_state(self, state: dict) -> None:
        # Coalesce several changes in quick succession (Discord rate-limits status updates)
        if self._presence_task is None or self._presence_task.done():
            self._presence_task = asyncio.create_task(self._update_presence())

    async def _update_presence(self, force: bool = False) -> None:
        await asyncio.sleep(0 if force else 2)
        if not self.is_ready():
            return
        text = presence_text(self.player.state()) if self.settings["presence"] else "Navidrome"
        if text == self._presence_shown and not force:
            return
        self._presence_shown = text
        try:
            await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=text))
        except Exception as exc:
            log.debug("Discord status not updated: %s", exc)

    # ---------- Commands & settings ----------

    async def sync_commands(self) -> int:
        """Register the commands with Discord, excluding those disabled in the panel."""
        async with self._sync_lock:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            for name in self.settings["disabled"]:
                self.tree.remove_command(name, guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Registered %d slash commands for the server.", len(synced))
            return len(synced)

    def command_names(self) -> list[str]:
        return [cmd.name for cmd in self.tree.get_commands()]

    async def update_settings(self, new: dict) -> None:
        resync = set(new["disabled"]) != set(self.settings["disabled"])
        presence_changed = new["presence"] != self.settings["presence"]
        self.settings = new
        self.db.set_setting(SETTINGS_KEY, new)
        self.player.normalize = new["normalize"]
        if presence_changed:
            await self._update_presence(force=True)
        if resync:
            await self.sync_commands()

    async def check_command_allowed(self, interaction: discord.Interaction) -> bool:
        cmd = interaction.command
        if cmd is None or interaction.type is discord.InteractionType.autocomplete:
            return True
        name = (cmd.root_parent or cmd).name
        s = self.settings
        lang = L(interaction)

        async def deny(message: str) -> bool:
            if not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            return False

        # Disabled applies to everyone, in case a Discord client still has the old command list
        if name in s["disabled"]:
            return await deny(bt(lang, "cmd_disabled"))

        member = interaction.user
        if isinstance(member, discord.Member) and member.guild_permissions.manage_guild:
            return True   # server admins are always allowed everything

        if s["channel_id"] and str(interaction.channel_id) != str(s["channel_id"]):
            return await deny(bt(lang, "wrong_channel", channel=s["channel_id"]))

        if name in s["dj_commands"]:
            # Without a DJ role, "DJ only" commands are reserved for server admins
            if not s["dj_role_id"]:
                return await deny(bt(lang, "admin_only"))
            roles = {str(r.id) for r in getattr(member, "roles", [])}
            if str(s["dj_role_id"]) not in roles:
                return await deny(bt(lang, "dj_only", role=s["dj_role_id"]))
        return True

    async def close(self) -> None:
        # On shutdown the bot disconnects itself from the voice channel. That must not overwrite
        # the saved state, otherwise it would not rejoin after a restart.
        self.player.freeze_state()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)
        if not self.get_guild(self.config.guild_id):
            log.error("The bot is not a member of server %s! Please add it via the invite link.",
                      self.config.guild_id)
        # on_ready also fires after connection drops; only restore the first time
        if not self._restored:
            self._restored = True
            await self.player.restore()
        await self._update_presence(force=True)

    async def on_app_command_completion(self, interaction: discord.Interaction, command) -> None:
        # For the panel's activity log: who used which command with which options
        options = ", ".join(f"{k}={v}" for k, v in vars(interaction.namespace).items())
        try:
            self.db.log_activity(interaction.user.display_name, "discord", f"cmd:{command.qualified_name}",
                                 options)
        except Exception:
            log.exception("Could not log the command")

    async def on_voice_state_update(self, member, before, after) -> None:
        # If the bot is moved or kicked from the channel, record it and update the panel
        if member.id == self.user.id and before.channel != after.channel:
            self.player.voice_moved(after.channel.id if after.channel else None)

    async def on_command_error(self, interaction: discord.Interaction,
                               error: app_commands.AppCommandError) -> None:
        lang = L(interaction)
        original = getattr(error, "original", error)
        if isinstance(original, NavidromeError):
            message = bt(lang, "navidrome_error", error=translate_error(str(original), lang))
        elif isinstance(original, ValueError):
            message = translate_error(str(original), lang)
        else:
            log.exception("Command failed", exc_info=original)
            message = bt(lang, "generic_error")
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def ensure_voice(bot: MusicBot, interaction: discord.Interaction) -> bool:
    """Connect the bot to the user's voice channel if it is not connected anywhere yet."""
    vc = bot.player.voice
    if vc and vc.is_connected():
        return True
    member = interaction.user
    if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
        await interaction.followup.send(bt(L(interaction), "not_in_voice"), ephemeral=True)
        return False
    await bot.player.connect(member.voice.channel)
    return True


def register_commands(bot: MusicBot) -> None:
    tree = bot.tree
    player = bot.player
    nav = bot.navidrome

    # ---------- Autocomplete ----------

    async def song_autocomplete(_: discord.Interaction, current: str):
        if len(current) < 2:
            return []
        try:
            songs = (await nav.search(current, songs=25, albums=0, artists=0))["songs"]
        except NavidromeError:
            return []
        return [app_commands.Choice(name=choice_name(f"{s['artist']} – {s['title']}"),
                                    value=f"song:{s['id']}") for s in songs]

    async def album_autocomplete(_: discord.Interaction, current: str):
        if len(current) < 2:
            return []
        try:
            albums = (await nav.search(current, songs=0, albums=25, artists=0))["albums"]
        except NavidromeError:
            return []
        return [app_commands.Choice(name=choice_name(f"{a['artist']} – {a['name']}"),
                                    value=f"album:{a['id']}") for a in albums]

    async def playlist_autocomplete(_: discord.Interaction, current: str):
        try:
            lists = await nav.playlists()
        except NavidromeError:
            return []
        lists = [p for p in lists if current.lower() in p["name"].lower()][:25]
        return [app_commands.Choice(name=choice_name(p["name"]), value=f"playlist:{p['id']}")
                for p in lists]

    async def radio_autocomplete(_: discord.Interaction, current: str):
        try:
            stations = await nav.radios()
        except NavidromeError:
            return []
        stations = [r for r in stations if current.lower() in r["name"].lower()][:25]
        return [app_commands.Choice(name=choice_name(r["name"]), value=f"radio:{r['id']}")
                for r in stations]

    async def favlist_autocomplete(interaction: discord.Interaction, current: str):
        lang = L(interaction)
        lists = [f for f in bot.db.list_favlists() if current.lower() in f["name"].lower()][:25]
        return [app_commands.Choice(
            name=choice_name(bt(lang, "favlist_choice", name=f["name"], n=f["songCount"], owner=f["owner"])),
            value=f"fav:{f['id']}") for f in lists]

    def find_favlist(query: str) -> dict | None:
        if query.startswith("fav:") and query[4:].isdigit():
            return bot.db.get_favlist(int(query[4:]))
        matches = [f for f in bot.db.list_favlists() if query.lower() in f["name"].lower()]
        return bot.db.get_favlist(matches[0]["id"]) if matches else None

    async def find_station(query: str) -> dict | None:
        if query.startswith("radio:"):
            return await nav.radio(query[6:])
        matches = [r for r in await nav.radios() if query.lower() in r["name"].lower()]
        return matches[0] if matches else None

    # ---------- Playback ----------

    @tree.command(name="play", description="Search for a track and play it")
    @app_commands.describe(title="Title or artist", now="Play immediately instead of queueing")
    @app_commands.autocomplete(title=song_autocomplete)
    async def play(interaction: discord.Interaction, title: str, now: bool = False):
        lang = L(interaction)
        await interaction.response.defer()
        if title.startswith("song:"):
            song = await nav.song(title[5:])
        else:
            songs = (await nav.search(title, songs=1, albums=0, artists=0))["songs"]
            if not songs:
                await interaction.followup.send(bt(lang, "nothing_found", q=title))
                return
            song = songs[0]
        if not await ensure_voice(bot, interaction):
            return
        playing = player.current is not None
        await player.add([song], interaction.user.display_name, "now" if now else "end")
        key = "added" if playing and not now else "playing_now"
        await interaction.followup.send(bt(lang, key, track=track_line(song)))

    @tree.command(name="album", description="Play a whole album")
    @app_commands.describe(album="Album name")
    @app_commands.autocomplete(album=album_autocomplete)
    async def album_cmd(interaction: discord.Interaction, album: str):
        lang = L(interaction)
        await interaction.response.defer()
        if album.startswith("album:"):
            album_id = album[6:]
        else:
            albums = (await nav.search(album, songs=0, albums=1, artists=0))["albums"]
            if not albums:
                await interaction.followup.send(bt(lang, "no_album", q=album))
                return
            album_id = albums[0]["id"]
        data = await nav.album(album_id)
        if not await ensure_voice(bot, interaction):
            return
        await player.add(data["songs"], interaction.user.display_name)
        await interaction.followup.send(
            bt(lang, "album_added", name=data["name"], artist=data["artist"], n=len(data["songs"])))

    @tree.command(name="playlist", description="Play a Navidrome playlist")
    @app_commands.describe(playlist="Playlist name")
    @app_commands.autocomplete(playlist=playlist_autocomplete)
    async def playlist_cmd(interaction: discord.Interaction, playlist: str):
        lang = L(interaction)
        await interaction.response.defer()
        if playlist.startswith("playlist:"):
            playlist_id = playlist[9:]
        else:
            matches = [p for p in await nav.playlists() if playlist.lower() in p["name"].lower()]
            if not matches:
                await interaction.followup.send(bt(lang, "no_playlist", q=playlist))
                return
            playlist_id = matches[0]["id"]
        data = await nav.playlist(playlist_id)
        if not await ensure_voice(bot, interaction):
            return
        await player.add(data["songs"], interaction.user.display_name)
        await interaction.followup.send(bt(lang, "playlist_added", name=data["name"], n=len(data["songs"])))

    @tree.command(name="favorites", description="Play a favorites list from the web panel")
    @app_commands.describe(list="Name of the favorites list", shuffle="Shuffle the order")
    @app_commands.autocomplete(list=favlist_autocomplete)
    async def favorites(interaction: discord.Interaction, list: str, shuffle: bool = False):
        lang = L(interaction)
        await interaction.response.defer()
        fav = find_favlist(list)
        if not fav:
            await interaction.followup.send(bt(lang, "no_favlist", q=list))
            return
        if not fav["songs"]:
            await interaction.followup.send(bt(lang, "favlist_empty", name=fav["name"]))
            return
        if not await ensure_voice(bot, interaction):
            return
        songs = [*fav["songs"]]
        if shuffle:
            random.shuffle(songs)
        await player.add(songs, interaction.user.display_name)
        await interaction.followup.send(bt(lang, "favlist_added", name=fav["name"], n=len(songs)))

    @tree.command(name="radio", description="Listen to a radio station")
    @app_commands.describe(station="Station name")
    @app_commands.autocomplete(station=radio_autocomplete)
    async def radio_cmd(interaction: discord.Interaction, station: str):
        lang = L(interaction)
        await interaction.response.defer()
        found = await find_station(station)
        if not found:
            await interaction.followup.send(bt(lang, "no_radio", q=station))
            return
        if not await ensure_voice(bot, interaction):
            return
        await player.add([radio_track(found)], interaction.user.display_name, "now")
        await interaction.followup.send(bt(lang, "radio_live", name=found["name"]))

    @tree.command(name="random", description="Play random tracks from the library")
    @app_commands.describe(count="How many tracks (1–100)")
    async def random_cmd(interaction: discord.Interaction, count: app_commands.Range[int, 1, 100] = 20):
        await interaction.response.defer()
        songs = await nav.random_songs(count)
        if not await ensure_voice(bot, interaction):
            return
        await player.add(songs, interaction.user.display_name)
        await interaction.followup.send(bt(L(interaction), "random_added", n=len(songs)))

    # ---------- Controls ----------

    @tree.command(name="join", description="Bring the bot into your voice channel")
    async def join(interaction: discord.Interaction):
        lang = L(interaction)
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await interaction.response.send_message(bt(lang, "not_in_voice"), ephemeral=True)
            return
        await interaction.response.defer()
        await player.connect(member.voice.channel)
        if not player.current and (player.queue or player.program):
            await player.skip()   # resume the queue or nonstop loop
        await interaction.followup.send(bt(lang, "joined", channel=member.voice.channel.name))

    @tree.command(name="leave", description="Leave the voice channel")
    async def leave(interaction: discord.Interaction):
        await player.disconnect()
        await interaction.response.send_message(bt(L(interaction), "bye"))

    @tree.command(name="skip", description="Skip to the next track")
    async def skip(interaction: discord.Interaction):
        lang = L(interaction)
        if not player.current:
            await interaction.response.send_message(bt(lang, "nothing_playing"), ephemeral=True)
            return
        await player.skip()
        await interaction.response.send_message(bt(lang, "skipped"))

    @tree.command(name="previous", description="Go to the previous track")
    async def previous(interaction: discord.Interaction):
        await player.previous()
        await interaction.response.send_message(bt(L(interaction), "previous"))

    @tree.command(name="pause", description="Pause playback")
    async def pause(interaction: discord.Interaction):
        player.pause()
        await interaction.response.send_message(bt(L(interaction), "paused"))

    @tree.command(name="resume", description="Resume playback")
    async def resume(interaction: discord.Interaction):
        player.resume()
        await interaction.response.send_message(bt(L(interaction), "resumed"))

    @tree.command(name="stop", description="Stop everything: playback, queue and nonstop")
    async def stop(interaction: discord.Interaction):
        await player.stop()
        await interaction.response.send_message(bt(L(interaction), "stopped"))

    # ---------- Nonstop loop ----------

    nonstop = app_commands.Group(name="nonstop", description="Favorites list or radio on nonstop repeat")

    @nonstop.command(name="list", description="Play a favorites list over and over")
    @app_commands.describe(list="Name of the favorites list", mode="In order or shuffled")
    @app_commands.autocomplete(list=favlist_autocomplete)
    @app_commands.choices(mode=[
        app_commands.Choice(name="In order", value="order"),
        app_commands.Choice(name="Shuffled", value="shuffle"),
    ])
    async def nonstop_list(interaction: discord.Interaction, list: str,
                           mode: app_commands.Choice[str] | None = None):
        lang = L(interaction)
        await interaction.response.defer()
        fav = find_favlist(list)
        if not fav:
            await interaction.followup.send(bt(lang, "no_favlist", q=list))
            return
        if not await ensure_voice(bot, interaction):
            return
        shuffle = bool(mode and mode.value == "shuffle")
        await player.start_program("favlist", str(fav["id"]), interaction.user.display_name, shuffle)
        await interaction.followup.send(bt(lang, "program_list", name=player.program["name"],
                                           mode=bt(lang, "mode_shuffle" if shuffle else "mode_order")))

    @nonstop.command(name="radio", description="Keep a radio station running")
    @app_commands.describe(station="Station name")
    @app_commands.autocomplete(station=radio_autocomplete)
    async def nonstop_radio(interaction: discord.Interaction, station: str):
        lang = L(interaction)
        await interaction.response.defer()
        found = await find_station(station)
        if not found:
            await interaction.followup.send(bt(lang, "no_radio", q=station))
            return
        if not await ensure_voice(bot, interaction):
            return
        await player.start_program("radio", found["id"], interaction.user.display_name,
                                   radio_track=radio_track(found))
        await interaction.followup.send(bt(lang, "program_radio", name=found["name"]))

    @nonstop.command(name="stop", description="Stop nonstop playback")
    async def nonstop_stop(interaction: discord.Interaction):
        lang = L(interaction)
        if not player.program:
            await interaction.response.send_message(bt(lang, "no_program"), ephemeral=True)
            return
        name = player.program["name"]
        await player.stop_program()
        await interaction.response.send_message(bt(lang, "program_stopped", name=name))

    tree.add_command(nonstop)

    @tree.command(name="volume", description="Set the volume")
    @app_commands.describe(percent="Volume in percent (0–100)")
    async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]):
        player.set_volume(percent)
        await interaction.response.send_message(bt(L(interaction), "volume_set", n=percent))

    @tree.command(name="loop", description="Set repeat mode")
    @app_commands.describe(mode="What to repeat")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Track", value="track"),
        app_commands.Choice(name="Queue", value="queue"),
    ])
    async def loop_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
        lang = L(interaction)
        player.set_loop(mode.value)
        await interaction.response.send_message(bt(lang, "loop_set", mode=bt(lang, f"loop_{mode.value}")))

    @tree.command(name="shuffle", description="Shuffle the queue")
    async def shuffle_cmd(interaction: discord.Interaction):
        player.shuffle()
        await interaction.response.send_message(bt(L(interaction), "shuffled"))

    @tree.command(name="seek", description="Jump to a position in the track")
    @app_commands.describe(time="e.g. 1:30 or 90")
    async def seek(interaction: discord.Interaction, time: str):
        lang = L(interaction)
        try:
            parts = [int(p) for p in time.split(":")]
        except ValueError:
            await interaction.response.send_message(bt(lang, "seek_format"), ephemeral=True)
            return
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        await player.seek(seconds)
        await interaction.response.send_message(bt(lang, "seeked", time=fmt_time(seconds)))

    # ---------- Display ----------

    @tree.command(name="nowplaying", description="Show the current track")
    async def nowplaying(interaction: discord.Interaction):
        lang = L(interaction)
        track = player.current
        if not track:
            await interaction.response.send_message(bt(lang, "nothing_playing"), ephemeral=True)
            return
        if track.get("radio"):
            embed = discord.Embed(title=f"📻 {track['title']}",
                                  description=track.get("nowPlaying") or bt(lang, "live_radio"), color=0x6C5CE7)
            embed.add_field(name=bt(lang, "playing_for"), value=fmt_time(player.position()))
        else:
            embed = discord.Embed(title=track["title"], description=f"{track['artist']}\n*{track['album']}*",
                                  color=0x6C5CE7)
            embed.add_field(name=bt(lang, "position"),
                            value=f"{fmt_time(player.position())} / {fmt_time(track['duration'])}")
        embed.add_field(name=bt(lang, "volume"), value=f"{player.volume}%")
        embed.add_field(name=bt(lang, "repeat"), value=bt(lang, f"loop_{player.loop_mode}"))
        embed.set_footer(text=bt(lang, "added_by", who=track.get("added_by", "?")))
        await interaction.response.send_message(embed=embed)

    @tree.command(name="queue", description="Show the queue")
    async def queue(interaction: discord.Interaction):
        lang = L(interaction)
        lines = []
        if player.current:
            lines.append(f"▶️ {track_line(player.current)}\n")
        for i, track in enumerate(player.queue[:15], start=1):
            lines.append(f"`{i}.` {track_line(track)}")
        if len(player.queue) > 15:
            lines.append(bt(lang, "queue_more", n=len(player.queue) - 15))
        program = player.program_info()
        if program:
            mode = ""
            if program["type"] == "favlist":
                mode = (f" · {bt(lang, 'mode_shuffle' if program['shuffle'] else 'order')}"
                        f" · {bt(lang, 'round', n=program['round'])}")
            lines.append(bt(lang, "queue_program", name=program["name"], mode=mode))
        if not lines:
            lines.append(bt(lang, "queue_empty"))
        embed = discord.Embed(title=bt(lang, "queue_title"), description="\n".join(lines), color=0x6C5CE7)
        await interaction.response.send_message(embed=embed)

    @tree.command(name="panel", description="Link to the web panel")
    async def panel(interaction: discord.Interaction):
        lang = L(interaction)
        url = bot.config.panel_url or bt(lang, "no_panel_url")
        await interaction.response.send_message(bt(lang, "panel", url=url), ephemeral=True)
