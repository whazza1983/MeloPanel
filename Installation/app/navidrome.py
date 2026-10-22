import asyncio
import hashlib
import re
import secrets
from urllib.parse import urlencode

import aiohttp

API_VERSION = "1.16.1"
CLIENT_NAME = "navidrome-discord"


class NavidromeError(Exception):
    pass


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def song_info(s: dict) -> dict:
    """Convert a Subsonic song into a compact, uniform format."""
    return {
        "id": s["id"],
        "title": s.get("title") or "Unknown",
        "artist": s.get("artist") or "Unknown artist",
        "artistId": s.get("artistId"),
        "album": s.get("album") or "",
        "albumId": s.get("albumId"),
        "duration": int(s.get("duration") or 0),
        "coverArt": s.get("coverArt"),
        "track": s.get("track"),
        "year": s.get("year"),
    }


def album_info(a: dict) -> dict:
    return {
        "id": a["id"],
        "name": a.get("name") or a.get("title") or "Unknown",
        "artist": a.get("artist") or "",
        "artistId": a.get("artistId"),
        "coverArt": a.get("coverArt"),
        "songCount": a.get("songCount", 0),
        "duration": int(a.get("duration") or 0),
        "year": a.get("year"),
    }


def artist_info(a: dict) -> dict:
    return {
        "id": a["id"],
        "name": a.get("name") or "Unknown",
        "albumCount": a.get("albumCount", 0),
        "coverArt": a.get("coverArt"),
    }


def radio_info(r: dict) -> dict:
    return {
        "id": r["id"],
        "name": r.get("name") or "Radio",
        "streamUrl": r.get("streamUrl") or "",
        "homePageUrl": r.get("homePageUrl") or "",
        "coverArt": r.get("coverArt") or None,
    }


def radio_track(r: dict) -> dict:
    """A radio station as a queue entry."""
    return {
        "id": f"radio:{r['id']}",
        "radioId": r["id"],
        "radio": True,
        "url": r["streamUrl"],
        "title": r["name"],
        "artist": "Live-Radio",
        "artistId": None,
        "album": "",
        "albumId": None,
        "duration": 0,
        "coverArt": r.get("coverArt"),
        "nowPlaying": None,
    }


def is_stream_url(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


async def read_icy_title(session: aiohttp.ClientSession, url: str) -> str | None:
    """Read the current song title (ICY metadata) from a radio stream."""
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.get(url, headers={"Icy-MetaData": "1"}, timeout=timeout) as resp:
            metaint = int(resp.headers.get("icy-metaint", "0") or 0)
            if resp.status != 200 or metaint <= 0 or metaint > 1_000_000:
                return None
            await resp.content.readexactly(metaint)
            length = (await resp.content.readexactly(1))[0] * 16
            if not length:
                return None
            raw = (await resp.content.readexactly(length)).rstrip(b"\0")
    except (aiohttp.ClientError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
        return None
    try:
        meta = raw.decode("utf-8")
    except UnicodeDecodeError:
        meta = raw.decode("latin-1")
    match = re.search(r"StreamTitle='(.*?)';", meta)
    title = match.group(1).strip() if match else ""
    return title or None


class Navidrome:
    """Minimal client for the Navidrome Subsonic API."""

    def __init__(self, url: str, user: str, password: str, max_bitrate: int = 0):
        self.url = url
        self.user = user
        self.password = password
        self.max_bitrate = max_bitrate
        self._session: aiohttp.ClientSession | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _auth(self) -> dict:
        salt = secrets.token_hex(8)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return {"u": self.user, "t": token, "s": salt, "v": API_VERSION, "c": CLIENT_NAME}

    async def _get(self, endpoint: str, **params) -> dict:
        query = {**self._auth(), "f": "json", **{k: v for k, v in params.items() if v is not None}}
        http = await self._http()
        try:
            async with http.get(f"{self.url}/rest/{endpoint}", params=query) as resp:
                if resp.status != 200:
                    raise NavidromeError(f"Navidrome responded with HTTP {resp.status}")
                data = (await resp.json(content_type=None))["subsonic-response"]
        except aiohttp.ClientError as exc:
            raise NavidromeError(f"Navidrome not reachable: {exc}") from exc
        if data.get("status") != "ok":
            error = data.get("error", {})
            raise NavidromeError(error.get("message", "Unknown error from Navidrome"))
        return data

    # ---------- Library ----------

    async def ping(self) -> None:
        await self._get("ping")

    async def search(self, query: str, songs: int = 50, albums: int = 20, artists: int = 10) -> dict:
        data = (await self._get(
            "search3", query=query, songCount=songs, albumCount=albums, artistCount=artists
        )).get("searchResult3", {})
        return {
            "songs": [song_info(s) for s in _as_list(data.get("song"))],
            "albums": [album_info(a) for a in _as_list(data.get("album"))],
            "artists": [artist_info(a) for a in _as_list(data.get("artist"))],
        }

    async def album_list(self, list_type: str = "newest", size: int = 50, offset: int = 0) -> list[dict]:
        data = await self._get("getAlbumList2", type=list_type, size=size, offset=offset)
        return [album_info(a) for a in _as_list(data.get("albumList2", {}).get("album"))]

    async def album(self, album_id: str) -> dict:
        data = (await self._get("getAlbum", id=album_id))["album"]
        info = album_info(data)
        info["songs"] = [song_info(s) for s in _as_list(data.get("song"))]
        return info

    async def artists(self) -> list[dict]:
        data = await self._get("getArtists")
        result = []
        for index in _as_list(data.get("artists", {}).get("index")):
            result.extend(artist_info(a) for a in _as_list(index.get("artist")))
        return result

    async def artist(self, artist_id: str) -> dict:
        data = (await self._get("getArtist", id=artist_id))["artist"]
        info = artist_info(data)
        info["albums"] = [album_info(a) for a in _as_list(data.get("album"))]
        return info

    async def playlists(self) -> list[dict]:
        data = await self._get("getPlaylists")
        return [
            {
                "id": p["id"],
                "name": p.get("name", "Playlist"),
                "songCount": p.get("songCount", 0),
                "duration": int(p.get("duration") or 0),
                "coverArt": p.get("coverArt"),
                "owner": p.get("owner"),
            }
            for p in _as_list(data.get("playlists", {}).get("playlist"))
        ]

    async def playlist(self, playlist_id: str) -> dict:
        data = (await self._get("getPlaylist", id=playlist_id))["playlist"]
        return {
            "id": data["id"],
            "name": data.get("name", "Playlist"),
            "coverArt": data.get("coverArt"),
            "songs": [song_info(s) for s in _as_list(data.get("entry"))],
        }

    async def song(self, song_id: str) -> dict:
        return song_info((await self._get("getSong", id=song_id))["song"])

    async def random_songs(self, size: int = 20) -> list[dict]:
        data = await self._get("getRandomSongs", size=size)
        return [song_info(s) for s in _as_list(data.get("randomSongs", {}).get("song"))]

    # ---------- Internet radio ----------

    async def radios(self) -> list[dict]:
        data = await self._get("getInternetRadioStations")
        stations = _as_list(data.get("internetRadioStations", {}).get("internetRadioStation"))
        return sorted((radio_info(r) for r in stations), key=lambda r: r["name"].lower())

    async def radio(self, radio_id: str) -> dict:
        for station in await self.radios():
            if station["id"] == radio_id:
                return station
        raise NavidromeError("Radio station not found")

    async def create_radio(self, name: str, stream_url: str, homepage: str = "") -> None:
        await self._get("createInternetRadioStation", name=name, streamUrl=stream_url,
                        homepageUrl=homepage or None)

    async def update_radio(self, radio_id: str, name: str, stream_url: str, homepage: str = "") -> None:
        await self._get("updateInternetRadioStation", id=radio_id, name=name, streamUrl=stream_url,
                        homepageUrl=homepage or None)

    async def delete_radio(self, radio_id: str) -> None:
        await self._get("deleteInternetRadioStation", id=radio_id)

    async def icy_title(self, url: str) -> str | None:
        return await read_icy_title(await self._http(), url)

    # ---------- Media ----------

    def stream_url(self, song_id: str) -> str:
        params = {**self._auth(), "id": song_id}
        if self.max_bitrate:
            params["maxBitRate"] = self.max_bitrate
        return f"{self.url}/rest/stream?{urlencode(params)}"

    async def cover(self, cover_id: str, size: int = 300) -> tuple[bytes, str]:
        params = {**self._auth(), "id": cover_id, "size": size}
        http = await self._http()
        try:
            async with http.get(f"{self.url}/rest/getCoverArt", params=params) as resp:
                content_type = resp.headers.get("Content-Type", "image/jpeg")
                if resp.status != 200 or not content_type.startswith("image/"):
                    raise NavidromeError("Cover not found")
                return await resp.read(), content_type
        except aiohttp.ClientError as exc:
            raise NavidromeError(f"Navidrome not reachable: {exc}") from exc
