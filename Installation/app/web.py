import asyncio
import logging
import re
import time
from collections import defaultdict
from pathlib import Path

import discord
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .bot import COMMAND_CATEGORIES, MusicBot
from .config import Config
from .db import Database
from .i18n import COMMANDS_DE, translate_error
from .navidrome import NavidromeError, is_stream_url, radio_track

log = logging.getLogger("web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
LOGIN_WINDOW = 300      # seconds
LOGIN_MAX_FAILS = 5


# ---------- Request models ----------

class LoginBody(BaseModel):
    username: str
    password: str


class LangBody(BaseModel):
    lang: str = Field(pattern="^(de|en)$")


class PasswordChange(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8)


class NewUser(BaseModel):
    username: str
    password: str = Field(min_length=8)
    is_admin: bool = False


class PasswordReset(BaseModel):
    password: str = Field(min_length=8)


class AdminFlag(BaseModel):
    is_admin: bool


class JoinBody(BaseModel):
    channel_id: str


class RadioBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    streamUrl: str = Field(min_length=8, max_length=1000)
    homePageUrl: str = Field(default="", max_length=1000)


class FavListBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class FavSongsBody(BaseModel):
    type: str = Field(pattern="^(song|album|playlist|favlist)$")
    id: str | None = None
    ids: list[str] | None = None


class SongRef(BaseModel):
    song_id: str = Field(min_length=1, max_length=200)


class AddBody(BaseModel):
    type: str = Field(pattern="^(song|album|playlist|random|radio|favlist)$")
    id: str | None = None
    ids: list[str] | None = None
    mode: str = Field(default="end", pattern="^(end|next|now)$")
    count: int = Field(default=20, ge=1, le=100)


class BotSettingsBody(BaseModel):
    disabled: list[str] = []
    dj_role_id: str | None = None
    dj_commands: list[str] = []
    channel_id: str | None = None
    normalize: bool = True
    presence: bool = True


class ScheduleEntry(BaseModel):
    id: str | None = Field(default=None, max_length=16)
    enabled: bool = True
    days: list[int] = Field(min_length=1, max_length=7)
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    action: str = Field(pattern="^(favlist|radio|stop)$")
    target: str | None = Field(default=None, max_length=200)
    shuffle: bool = False


class ScheduleBody(BaseModel):
    entries: list[ScheduleEntry] = Field(max_length=100)


class ProgramBody(BaseModel):
    type: str = Field(pattern="^(favlist|radio)$")
    id: str = Field(min_length=1, max_length=200)
    shuffle: bool = False


class ShuffleBody(BaseModel):
    shuffle: bool


class ControlBody(BaseModel):
    action: str = Field(pattern="^(pause|resume|toggle|skip|previous|stop|shuffle|clear|clear_history|leave)$")


class VolumeBody(BaseModel):
    volume: int = Field(ge=0, le=100)


class LoopBody(BaseModel):
    mode: str = Field(pattern="^(off|track|queue)$")


class SeekBody(BaseModel):
    position: float = Field(ge=0)


class IndexBody(BaseModel):
    index: int = Field(ge=0)


class MoveBody(BaseModel):
    src: int = Field(ge=0)
    dst: int = Field(ge=0)


# ---------- App ----------

def create_app(config: Config, db: Database, bot: MusicBot) -> FastAPI:
    app = FastAPI(title="MeloPanel", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret_key,
        session_cookie="nd_panel",
        max_age=60 * 60 * 24 * 30,
        same_site="lax",
        https_only=config.cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next):
        # Make browsers revalidate panel files on every use (ETag) so updates take effect immediately
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/") or path in ("/", "/login"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    nav = bot.navidrome
    player = bot.player
    scheduler = bot.scheduler
    login_fails: dict[str, list[float]] = defaultdict(list)
    sockets: set[WebSocket] = set()

    def act(user: dict | str | None, action: str, detail: str = "", merge: int = 0) -> None:
        """Write an activity log entry (failures here must never break the actual action)."""
        name = user["username"] if isinstance(user, dict) else user
        try:
            db.log_activity(name, "panel", action, detail, merge_seconds=merge)
        except Exception:
            log.exception("Could not log the action")

    def track_label(track: dict | None) -> str:
        if not track:
            return ""
        if track.get("radio"):
            return f"📻 {track.get('title', '')}"
        return f"{track.get('artist', '?')} – {track.get('title', '?')}"

    # ---------- Auth ----------

    def current_user(request: Request) -> dict:
        user_id = request.session.get("user_id")
        user = db.get_user(user_id) if user_id else None
        if not user:
            raise HTTPException(401, "Not logged in")
        return user

    def admin_user(user: dict = Depends(current_user)) -> dict:
        if not user["is_admin"]:
            raise HTTPException(403, "Admins only")
        return user

    def request_lang(request: Request) -> str:
        # The panel sends the selected language; otherwise fall back to the browser language
        lang = request.headers.get("x-lang") or request.headers.get("accept-language", "")
        return "de" if lang.lower().startswith("de") else "en"

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return JSONResponse({"detail": translate_error(exc.detail, request_lang(request))},
                            status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(NavidromeError)
    async def navidrome_error(request: Request, exc: NavidromeError):
        return JSONResponse({"detail": translate_error(f"Navidrome: {exc}", request_lang(request))},
                            status_code=502)

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        if not request.session.get("user_id"):
            return RedirectResponse("/login")
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/login", include_in_schema=False)
    async def login_page():
        return FileResponse(STATIC_DIR / "login.html")

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request):
        ip = request.client.host if request.client else "?"
        now = time.time()
        fails = [t for t in login_fails[ip] if now - t < LOGIN_WINDOW]
        login_fails[ip] = fails
        if len(fails) >= LOGIN_MAX_FAILS:
            raise HTTPException(429, "Too many failed attempts. Please try again in a few minutes.")
        user = await asyncio.to_thread(db.verify, body.username.strip(), body.password)
        if not user:
            fails.append(now)
            act(body.username.strip()[:32], "login_failed", ip)
            raise HTTPException(401, "Wrong username or password")
        login_fails.pop(ip, None)
        request.session.clear()
        request.session["user_id"] = user["id"]
        db.touch_login(user["id"])
        log.info("Login: %s (%s)", user["username"], ip)
        act(user, "login", ip)
        return user

    @app.post("/api/logout")
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    async def me(user: dict = Depends(current_user)):
        return user

    @app.post("/api/me/lang")
    async def set_lang(body: LangBody, user: dict = Depends(current_user)):
        db.set_lang(user["id"], body.lang)
        return {"ok": True}

    @app.post("/api/me/password")
    async def change_password(body: PasswordChange, user: dict = Depends(current_user)):
        if not await asyncio.to_thread(db.verify, user["username"], body.old_password):
            raise HTTPException(400, "Current password is wrong")
        await asyncio.to_thread(db.set_password, user["id"], body.new_password)
        return {"ok": True}

    # ---------- User management (admin) ----------

    @app.get("/api/users")
    async def list_users(_: dict = Depends(admin_user)):
        return db.list_users()

    @app.post("/api/users")
    async def create_user(body: NewUser, admin: dict = Depends(admin_user)):
        username = body.username.strip()
        if not USERNAME_RE.match(username):
            raise HTTPException(400, "Username: 3–32 characters, only letters, digits, _ . -")
        if any(u["username"].lower() == username.lower() for u in db.list_users()):
            raise HTTPException(400, "Username is already taken")
        created = await asyncio.to_thread(db.create_user, username, body.password, body.is_admin)
        act(admin, "user_create", username + (" (Admin)" if body.is_admin else ""))
        return created

    @app.delete("/api/users/{user_id}")
    async def delete_user(user_id: int, admin: dict = Depends(admin_user)):
        target = db.get_user(user_id)
        if not target:
            raise HTTPException(404, "User not found")
        if target["id"] == admin["id"]:
            raise HTTPException(400, "You can't delete yourself")
        db.delete_user(user_id)
        act(admin, "user_delete", target["username"])
        return {"ok": True}

    @app.post("/api/users/{user_id}/password")
    async def reset_password(user_id: int, body: PasswordReset, admin: dict = Depends(admin_user)):
        target = db.get_user(user_id)
        if not target:
            raise HTTPException(404, "User not found")
        await asyncio.to_thread(db.set_password, user_id, body.password)
        act(admin, "user_password", target["username"])
        return {"ok": True}

    @app.post("/api/users/{user_id}/admin")
    async def set_admin(user_id: int, body: AdminFlag, admin: dict = Depends(admin_user)):
        target = db.get_user(user_id)
        if not target:
            raise HTTPException(404, "User not found")
        if target["id"] == admin["id"] and not body.is_admin:
            raise HTTPException(400, "You can't remove your own admin rights")
        db.set_admin(user_id, body.is_admin)
        act(admin, "user_admin_on" if body.is_admin else "user_admin_off", target["username"])
        return db.get_user(user_id)

    # ---------- Bot settings (admin) ----------

    def bot_guild() -> discord.Guild:
        guild = bot.get_guild(config.guild_id)
        if not guild:
            raise HTTPException(503, "The bot is not connected to the Discord server (yet)")
        return guild

    @app.get("/api/bot/settings")
    async def bot_settings(_: dict = Depends(admin_user)):
        guild = bot_guild()
        commands = [{
            "name": cmd.name,
            "name_de": COMMANDS_DE.get(cmd.name, cmd.name),
            "description": cmd.description,
            "description_de": COMMANDS_DE.get(cmd.description, cmd.description),
            "category": COMMAND_CATEGORIES.get(cmd.name, "info"),
        } for cmd in bot.tree.get_commands()]
        roles = [{"id": str(r.id), "name": r.name, "color": str(r.color) if r.color.value else None}
                 for r in sorted(guild.roles, key=lambda r: -r.position)
                 if not r.is_default() and not r.managed]
        channels = [{"id": str(c.id), "name": c.name} for c in guild.text_channels]
        return {"settings": bot.settings, "commands": commands, "roles": roles, "channels": channels}

    @app.put("/api/bot/settings")
    async def save_bot_settings(body: BotSettingsBody, admin: dict = Depends(admin_user)):
        guild = bot_guild()
        names = set(bot.command_names())
        if not set(body.disabled) <= names or not set(body.dj_commands) <= names:
            raise HTTPException(400, "Unknown command")
        if body.dj_role_id and not (body.dj_role_id.isdigit() and guild.get_role(int(body.dj_role_id))):
            raise HTTPException(400, "Role not found")
        if body.channel_id and not (body.channel_id.isdigit()
                                    and isinstance(guild.get_channel(int(body.channel_id)), discord.TextChannel)):
            raise HTTPException(400, "Text channel not found")
        new = {
            "disabled": sorted(set(body.disabled)),
            "dj_role_id": body.dj_role_id or None,
            "dj_commands": sorted(set(body.dj_commands)),
            "channel_id": body.channel_id or None,
            "normalize": body.normalize,
            "presence": body.presence,
        }
        old = bot.settings
        try:
            await bot.update_settings(new)
        except discord.HTTPException as exc:
            log.exception("Could not update the commands on Discord")
            raise HTTPException(502, f"Discord: {exc}")
        changed = [key for key in new if new[key] != old.get(key)]
        if changed:
            act(admin, "bot_settings", ", ".join(changed))
        return {"settings": bot.settings}

    # ---------- Schedule (admin) ----------

    @app.get("/api/schedule")
    async def get_schedule(_: dict = Depends(admin_user)):
        return {"entries": scheduler.entries(), "now": scheduler.now().strftime("%H:%M"),
                "timezone": config.timezone}

    @app.put("/api/schedule")
    async def save_schedule(body: ScheduleBody, admin: dict = Depends(admin_user)):
        favlists = {str(f["id"]): f["name"] for f in db.list_favlists()}
        radios = {r["id"]: r["name"] for r in await nav.radios()} if any(
            e.action == "radio" for e in body.entries) else {}
        entries = []
        for e in body.entries:
            days = sorted({d for d in e.days if 0 <= d <= 6})
            if not days:
                raise HTTPException(400, "Please choose at least one weekday")
            if e.action == "favlist" and e.target not in favlists:
                raise HTTPException(400, "Favorites list not found")
            if e.action == "radio" and e.target not in radios:
                raise HTTPException(400, "Radio station not found")
            name = favlists.get(e.target) if e.action == "favlist" else radios.get(e.target) if e.action == "radio" else ""
            entries.append({"id": e.id or None, "enabled": e.enabled, "days": days, "time": e.time,
                            "action": e.action, "target": e.target if e.action != "stop" else None,
                            "name": name or "", "shuffle": e.shuffle and e.action == "favlist"})
        for entry in entries:
            if not entry["id"]:
                del entry["id"]
        saved = scheduler.save(entries)
        act(admin, "schedule_save", str(len(saved)))
        return {"entries": saved}

    @app.post("/api/schedule/{entry_id}/run")
    async def run_schedule_entry(entry_id: str, admin: dict = Depends(admin_user)):
        entry = next((e for e in scheduler.entries() if e.get("id") == entry_id), None)
        if not entry:
            raise HTTPException(404, "Entry not found")
        act(admin, "schedule_run", entry.get("name") or entry["action"])
        await scheduler.execute(entry)
        return {"ok": True}

    # ---------- Statistics & activity log ----------

    @app.get("/api/stats")
    async def stats(days: int = 30, _: dict = Depends(current_user)):
        return await asyncio.to_thread(db.stats, max(0, min(days, 3650)), 10)

    @app.get("/api/activity")
    async def activity(before: int | None = None, _: dict = Depends(admin_user)):
        return await asyncio.to_thread(db.list_activity, 100, before)

    # ---------- Monitoring ----------

    @app.get("/api/health", include_in_schema=False)
    async def health():
        """Health check for uptime monitors: 200 = OK, 503 = bot offline, not in a channel, or playback stuck."""
        info = {"discord": bot.is_ready() and not bot.is_closed(), **player.health()}
        if not info["discord"]:
            info["problems"] = ["discord_offline", *info["problems"]]
        info["status"] = "problem" if info["problems"] else "ok"
        return JSONResponse(info, status_code=503 if info["problems"] else 200)

    # ---------- Library ----------

    @app.get("/api/search")
    async def search(q: str, _: dict = Depends(current_user)):
        if len(q.strip()) < 2:
            return {"songs": [], "albums": [], "artists": []}
        return await nav.search(q.strip())

    @app.get("/api/albums")
    async def albums(type: str = "newest", offset: int = 0, size: int = 60,
                     _: dict = Depends(current_user)):
        allowed = {"newest", "random", "frequent", "recent", "alphabeticalByName",
                   "alphabeticalByArtist", "starred", "highest"}
        if type not in allowed:
            raise HTTPException(400, "Invalid list")
        return await nav.album_list(type, min(max(size, 1), 200), max(offset, 0))

    @app.get("/api/albums/{album_id}")
    async def album(album_id: str, _: dict = Depends(current_user)):
        return await nav.album(album_id)

    @app.get("/api/artists")
    async def artists(_: dict = Depends(current_user)):
        return await nav.artists()

    @app.get("/api/artists/{artist_id}")
    async def artist(artist_id: str, _: dict = Depends(current_user)):
        return await nav.artist(artist_id)

    @app.get("/api/playlists")
    async def playlists(_: dict = Depends(current_user)):
        return await nav.playlists()

    @app.get("/api/playlists/{playlist_id}")
    async def playlist(playlist_id: str, _: dict = Depends(current_user)):
        return await nav.playlist(playlist_id)

    # ---------- Internet radio ----------

    def check_radio(body: RadioBody) -> None:
        if not is_stream_url(body.streamUrl.strip()):
            raise HTTPException(400, "The stream URL must start with http:// or https://")
        if body.homePageUrl.strip() and not is_stream_url(body.homePageUrl.strip()):
            raise HTTPException(400, "The homepage must start with http:// or https://")

    @app.get("/api/radios")
    async def radios(_: dict = Depends(current_user)):
        return await nav.radios()

    @app.post("/api/radios")
    async def create_radio(body: RadioBody, admin: dict = Depends(admin_user)):
        check_radio(body)
        await nav.create_radio(body.name.strip(), body.streamUrl.strip(), body.homePageUrl.strip())
        act(admin, "radio_create", body.name.strip())
        return {"ok": True}

    @app.put("/api/radios/{radio_id}")
    async def update_radio(radio_id: str, body: RadioBody, admin: dict = Depends(admin_user)):
        check_radio(body)
        await nav.update_radio(radio_id, body.name.strip(), body.streamUrl.strip(), body.homePageUrl.strip())
        act(admin, "radio_update", body.name.strip())
        return {"ok": True}

    @app.delete("/api/radios/{radio_id}")
    async def delete_radio(radio_id: str, admin: dict = Depends(admin_user)):
        names = {r["id"]: r["name"] for r in await nav.radios()}
        await nav.delete_radio(radio_id)
        act(admin, "radio_delete", names.get(radio_id, radio_id))
        return {"ok": True}

    @app.get("/api/cover/{cover_id}")
    async def cover(cover_id: str, size: int = 300, _: dict = Depends(current_user)):
        try:
            data, content_type = await nav.cover(cover_id, min(max(size, 32), 1000))
        except NavidromeError:
            return Response(status_code=404)
        return Response(data, media_type=content_type,
                        headers={"Cache-Control": "private, max-age=86400"})

    # ---------- Player ----------

    @app.get("/api/player")
    async def player_state(_: dict = Depends(current_user)):
        return player.state()

    @app.get("/api/channels")
    async def channels(_: dict = Depends(current_user)):
        guild = bot.get_guild(config.guild_id)
        if not guild:
            raise HTTPException(503, "The bot is not connected to the Discord server (yet)")
        me = guild.me
        result = []
        for ch in guild.voice_channels:
            perms = ch.permissions_for(me)
            if perms.connect and perms.speak:
                result.append({
                    "id": str(ch.id),
                    "name": ch.name,
                    "listeners": player.listeners(ch),
                })
        return result

    @app.post("/api/player/join")
    async def join(body: JoinBody, user: dict = Depends(current_user)):
        guild = bot.get_guild(config.guild_id)
        channel = guild.get_channel(int(body.channel_id)) if guild and body.channel_id.isdigit() else None
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            raise HTTPException(404, "Voice channel not found")
        try:
            await player.connect(channel)
        except (discord.ClientException, asyncio.TimeoutError) as exc:
            raise HTTPException(500, f"Could not join: {exc}")
        act(user, "join", channel.name)
        # If something is already queued, start playing right away
        if not player.current and (player.queue or player.program):
            await player.skip()
        return player.state()

    async def resolve_songs(kind: str, item_id: str | None, ids: list[str] | None) -> list[dict]:
        """Resolve songs, albums, playlists and favorites lists into a list of tracks."""
        if kind == "song":
            ids = ids or ([item_id] if item_id else [])
            if not ids:
                raise HTTPException(400, "No tracks given")
            return list(await asyncio.gather(*(nav.song(i) for i in ids[:500])))
        if kind == "album":
            return (await nav.album(item_id or ""))["songs"]
        if kind == "playlist":
            return (await nav.playlist(item_id or ""))["songs"]
        if kind == "favlist":
            fav = db.get_favlist(int(item_id)) if (item_id or "").isdigit() else None
            if not fav:
                raise HTTPException(404, "Favorites list not found")
            return fav["songs"]
        raise HTTPException(400, "Unknown type")

    @app.post("/api/player/add")
    async def add(body: AddBody, user: dict = Depends(current_user)):
        if body.type == "radio":
            tracks = [radio_track(await nav.radio(body.id or ""))]
        elif body.type == "random":
            tracks = await nav.random_songs(body.count)
        else:
            tracks = await resolve_songs(body.type, body.id, body.ids)
        await player.add(tracks, user["username"], body.mode)
        if len(tracks) == 1:
            detail = track_label(tracks[0])
        elif body.type == "album" and tracks:
            detail = f"{len(tracks)} × {tracks[0].get('album', '')}"
        else:
            detail = f"{len(tracks)} × {body.type}"
        act(user, f"add:{body.mode}", detail)
        vc = player.voice
        return {"added": len(tracks), "connected": bool(vc and vc.is_connected())}

    @app.post("/api/player/control")
    async def control(body: ControlBody, user: dict = Depends(current_user)):
        action = body.action
        if action == "toggle":
            action = "resume" if player.paused else "pause"
        act(user, f"control:{action}", track_label(player.current) if action in ("skip", "stop", "pause") else "")
        if action == "pause":
            player.pause()
        elif action == "resume":
            player.resume()
        elif action == "skip":
            await player.skip()
        elif action == "previous":
            await player.previous()
        elif action == "stop":
            await player.stop()
        elif action == "shuffle":
            player.shuffle()
        elif action == "clear":
            player.clear()
        elif action == "clear_history":
            player.clear_history()
        elif action == "leave":
            await player.disconnect()
        return player.state()

    @app.post("/api/player/program")
    async def start_program(body: ProgramBody, user: dict = Depends(current_user)):
        try:
            if body.type == "radio":
                track = radio_track(await nav.radio(body.id))
                await player.start_program("radio", body.id, user["username"], radio_track=track)
            else:
                await player.start_program("favlist", body.id, user["username"], body.shuffle)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        act(user, f"program:{body.type}", (player.program or {}).get("name", "")
            + (" (shuffle)" if body.type == "favlist" and body.shuffle else ""))
        vc = player.voice
        return {"connected": bool(vc and vc.is_connected()), "program": player.program_info()}

    @app.post("/api/player/program/shuffle")
    async def program_shuffle(body: ShuffleBody, user: dict = Depends(current_user)):
        player.set_program_shuffle(body.shuffle)
        act(user, "program_shuffle_on" if body.shuffle else "program_shuffle_off")
        return {"ok": True}

    @app.post("/api/player/program/stop")
    async def stop_program(user: dict = Depends(current_user)):
        act(user, "program_stop", (player.program or {}).get("name", ""))
        await player.stop_program()
        return {"ok": True}

    @app.post("/api/player/volume")
    async def volume(body: VolumeBody, user: dict = Depends(current_user)):
        player.set_volume(body.volume)
        act(user, "volume", f"{body.volume}%", merge=30)
        return {"ok": True}

    @app.post("/api/player/loop")
    async def loop(body: LoopBody, user: dict = Depends(current_user)):
        player.set_loop(body.mode)
        act(user, f"loop:{body.mode}")
        return {"ok": True}

    @app.post("/api/player/seek")
    async def seek(body: SeekBody, user: dict = Depends(current_user)):
        await player.seek(body.position)
        act(user, "seek", f"{int(body.position) // 60}:{int(body.position) % 60:02d}", merge=15)
        return {"ok": True}

    @app.post("/api/player/queue/remove")
    async def queue_remove(body: IndexBody, user: dict = Depends(current_user)):
        if body.index < len(player.queue):
            act(user, "queue_remove", track_label(player.queue[body.index]))
        player.remove(body.index)
        return {"ok": True}

    @app.post("/api/player/queue/move")
    async def queue_move(body: MoveBody, _: dict = Depends(current_user)):
        player.move(body.src, body.dst)
        return {"ok": True}

    @app.post("/api/player/queue/jump")
    async def queue_jump(body: IndexBody, user: dict = Depends(current_user)):
        if body.index < len(player.queue):
            act(user, "queue_jump", track_label(player.queue[body.index]))
        await player.jump(body.index)
        return {"ok": True}

    # ---------- Favorites lists ----------

    def editable_favlist(list_id: int, user: dict) -> dict:
        fav = db.get_favlist(list_id)
        if not fav:
            raise HTTPException(404, "Favorites list not found")
        if fav["user_id"] != user["id"] and not user["is_admin"]:
            raise HTTPException(403, "This list belongs to someone else")
        return fav

    def with_rights(fav: dict, user: dict) -> dict:
        return {**fav, "canEdit": fav["user_id"] == user["id"] or user["is_admin"]}

    @app.get("/api/favlists")
    async def favlists(user: dict = Depends(current_user)):
        return [with_rights(f, user) for f in db.list_favlists()]

    @app.post("/api/favlists")
    async def create_favlist(body: FavListBody, user: dict = Depends(current_user)):
        list_id = db.create_favlist(user["id"], body.name.strip())
        act(user, "favlist_create", body.name.strip())
        return with_rights(db.get_favlist(list_id), user)

    @app.get("/api/favlists/{list_id}")
    async def favlist(list_id: int, user: dict = Depends(current_user)):
        fav = db.get_favlist(list_id)
        if not fav:
            raise HTTPException(404, "Favorites list not found")
        return with_rights(fav, user)

    @app.put("/api/favlists/{list_id}")
    async def rename_favlist(list_id: int, body: FavListBody, user: dict = Depends(current_user)):
        fav = editable_favlist(list_id, user)
        db.rename_favlist(list_id, body.name.strip())
        act(user, "favlist_rename", f"{fav['name']} → {body.name.strip()}")
        return {"ok": True}

    @app.delete("/api/favlists/{list_id}")
    async def delete_favlist(list_id: int, user: dict = Depends(current_user)):
        fav = editable_favlist(list_id, user)
        db.delete_favlist(list_id)
        act(user, "favlist_delete", fav["name"])
        return {"ok": True}

    @app.post("/api/favlists/{list_id}/songs")
    async def add_favlist_songs(list_id: int, body: FavSongsBody, user: dict = Depends(current_user)):
        editable_favlist(list_id, user)
        songs = await resolve_songs(body.type, body.id, body.ids)
        added = db.add_favlist_songs(list_id, songs)
        return {"added": added, "skipped": len(songs) - added}

    @app.post("/api/favlists/{list_id}/remove")
    async def remove_favlist_song(list_id: int, body: SongRef, user: dict = Depends(current_user)):
        editable_favlist(list_id, user)
        db.remove_favlist_song(list_id, body.song_id)
        return {"ok": True}

    @app.post("/api/favlists/{list_id}/move")
    async def move_favlist_song(list_id: int, body: MoveBody, user: dict = Depends(current_user)):
        editable_favlist(list_id, user)
        db.move_favlist_song(list_id, body.src, body.dst)
        return {"ok": True}

    # ---------- Live updates ----------

    async def broadcast(state: dict) -> None:
        for ws in list(sockets):
            try:
                await ws.send_json(state)
            except Exception:
                sockets.discard(ws)

    player.add_listener(broadcast)

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        user_id = ws.session.get("user_id")
        if not user_id or not db.get_user(user_id):
            await ws.close(code=4401)
            return
        await ws.accept()
        sockets.add(ws)
        try:
            await ws.send_json(player.state())
            while True:
                # The client sends an occasional "ping"; reply with the current state
                await ws.receive_text()
                await ws.send_json(player.state())
        except WebSocketDisconnect:
            pass
        finally:
            sockets.discard(ws)

    return app
