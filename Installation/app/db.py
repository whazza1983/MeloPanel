import json
import logging
import secrets
import sqlite3
import threading
import time
from pathlib import Path

import bcrypt

log = logging.getLogger("db")


class Database:
    """Small SQLite database for panel users and app data."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    is_admin      INTEGER NOT NULL DEFAULT 0,
                    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS favlists (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    name       TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS favlist_songs (
                    list_id  INTEGER NOT NULL REFERENCES favlists(id) ON DELETE CASCADE,
                    song_id  TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    song     TEXT NOT NULL,   -- JSON snapshot of the track info
                    added_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (list_id, song_id)
                );

                CREATE TABLE IF NOT EXISTS plays (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    played_at    INTEGER NOT NULL,   -- Unix time
                    song_id      TEXT NOT NULL,
                    title        TEXT,
                    artist       TEXT,
                    cover_art    TEXT,
                    duration     INTEGER NOT NULL DEFAULT 0,
                    radio        INTEGER NOT NULL DEFAULT 0,
                    from_program INTEGER NOT NULL DEFAULT 0,
                    added_by     TEXT
                );
                CREATE INDEX IF NOT EXISTS plays_time ON plays(played_at);

                CREATE TABLE IF NOT EXISTS activity (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    at      INTEGER NOT NULL,        -- Unix time
                    user    TEXT,
                    source  TEXT NOT NULL,           -- panel | discord | system
                    action  TEXT NOT NULL,
                    detail  TEXT
                );
                CREATE INDEX IF NOT EXISTS activity_time ON activity(at);

                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL      -- JSON
                );
                """
            )
            # Migrate older databases
            columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(users)")}
            if "lang" not in columns:
                self._conn.execute("ALTER TABLE users ADD COLUMN lang TEXT")
            if "last_login" not in columns:
                self._conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT")

    # ---------- Helpers ----------

    @staticmethod
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def _public(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"],
            "lang": row["lang"],
            "last_login": row["last_login"],
        }

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    # ---------- Admin account on first start ----------

    def ensure_admin(self, username: str, password: str) -> None:
        if self._query("SELECT 1 FROM users WHERE username = ?", (username,)):
            return
        generated = not password
        if generated:
            password = secrets.token_urlsafe(12)
        self.create_user(username, password, is_admin=True)
        if generated:
            log.warning(
                "Created admin account '%s'. Generated password: %s  (please change it after logging in)",
                username,
                password,
            )
        else:
            log.info("Created admin account '%s'.", username)

    # ---------- Users ----------

    def verify(self, username: str, password: str) -> dict | None:
        rows = self._query("SELECT * FROM users WHERE username = ?", (username,))
        if not rows:
            # Spend the same time as for existing users (avoids timing-based user enumeration)
            bcrypt.checkpw(b"x", bcrypt.hashpw(b"y", bcrypt.gensalt()))
            return None
        row = rows[0]
        if bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
            return self._public(row)
        return None

    def get_user(self, user_id: int) -> dict | None:
        rows = self._query("SELECT * FROM users WHERE id = ?", (user_id,))
        return self._public(rows[0]) if rows else None

    def list_users(self) -> list[dict]:
        return [self._public(r) for r in self._query("SELECT * FROM users ORDER BY username")]

    def create_user(self, username: str, password: str, is_admin: bool = False) -> dict:
        cur = self._execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            (username, self.hash_password(password), int(is_admin)),
        )
        return self.get_user(cur.lastrowid)

    def delete_user(self, user_id: int) -> None:
        self._execute("DELETE FROM users WHERE id = ?", (user_id,))

    def set_password(self, user_id: int, password: str) -> None:
        self._execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (self.hash_password(password), user_id),
        )

    def set_lang(self, user_id: int, lang: str) -> None:
        self._execute("UPDATE users SET lang = ? WHERE id = ?", (lang, user_id))

    def touch_login(self, user_id: int) -> None:
        self._execute("UPDATE users SET last_login = datetime('now') WHERE id = ?", (user_id,))

    def set_admin(self, user_id: int, is_admin: bool) -> None:
        self._execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))

    def admin_count(self) -> int:
        return self._query("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1")[0]["n"]

    # ---------- Settings / state ----------

    def get_setting(self, key: str):
        rows = self._query("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else None

    def set_setting(self, key: str, value) -> None:
        self._execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # ---------- Statistics ----------

    def record_play(self, track: dict) -> None:
        self._execute(
            "INSERT INTO plays (played_at, song_id, title, artist, cover_art, duration, radio, from_program, added_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(time.time()), str(track.get("id", "")), track.get("title"), track.get("artist"),
             track.get("coverArt"), int(track.get("duration") or 0), int(bool(track.get("radio"))),
             int(bool(track.get("fromProgram"))), track.get("added_by")),
        )

    def stats(self, days: int = 0, limit: int = 10) -> dict:
        since = int(time.time()) - days * 86400 if days > 0 else 0
        songs_sql = "FROM plays WHERE played_at >= ? AND radio = 0"
        total = self._query(f"SELECT COUNT(*) AS n, COALESCE(SUM(duration), 0) AS secs {songs_sql}", (since,))[0]
        top_songs = self._query(
            f"""SELECT song_id, title, artist, cover_art, COUNT(*) AS n {songs_sql}
                GROUP BY song_id ORDER BY n DESC, MAX(played_at) DESC LIMIT ?""", (since, limit))
        top_artists = self._query(
            f"""SELECT artist, COUNT(*) AS n {songs_sql} AND artist IS NOT NULL AND artist != ''
                GROUP BY artist COLLATE NOCASE ORDER BY n DESC LIMIT ?""", (since, limit))
        # Count only actual requests, not tracks from nonstop playback
        top_users = self._query(
            f"""SELECT added_by, COUNT(*) AS n {songs_sql} AND from_program = 0 AND added_by IS NOT NULL
                GROUP BY added_by ORDER BY n DESC LIMIT ?""", (since, limit))
        top_radios = self._query(
            """SELECT title, cover_art, COUNT(*) AS n FROM plays WHERE played_at >= ? AND radio = 1
               GROUP BY song_id ORDER BY n DESC LIMIT ?""", (since, limit))
        return {
            "plays": total["n"],
            "seconds": int(total["secs"]),
            "songs": [{"id": r["song_id"], "title": r["title"], "artist": r["artist"],
                       "coverArt": r["cover_art"], "count": r["n"]} for r in top_songs],
            "artists": [{"name": r["artist"], "count": r["n"]} for r in top_artists],
            "users": [{"name": r["added_by"], "count": r["n"]} for r in top_users],
            "radios": [{"name": r["title"], "coverArt": r["cover_art"], "count": r["n"]} for r in top_radios],
        }

    # ---------- Activity log ----------

    def log_activity(self, user: str | None, source: str, action: str, detail: str = "",
                     merge_seconds: int = 0) -> None:
        """Write an entry. With merge_seconds, a matching entry by the same user within the last
        few seconds is updated instead of creating a new one (e.g. for the volume slider)."""
        now = int(time.time())
        detail = (detail or "")[:300]
        with self._lock, self._conn:
            if merge_seconds:
                last = self._conn.execute(
                    "SELECT id, user, source, action, at FROM activity ORDER BY id DESC LIMIT 1").fetchone()
                if (last and last["user"] == user and last["source"] == source
                        and last["action"] == action and now - last["at"] <= merge_seconds):
                    self._conn.execute("UPDATE activity SET at = ?, detail = ? WHERE id = ?",
                                       (now, detail, last["id"]))
                    return
            self._conn.execute(
                "INSERT INTO activity (at, user, source, action, detail) VALUES (?, ?, ?, ?, ?)",
                (now, user, source, action, detail),
            )

    def list_activity(self, limit: int = 200, before: int | None = None) -> list[dict]:
        rows = self._query(
            "SELECT * FROM activity WHERE id < ? ORDER BY id DESC LIMIT ?",
            (before or 2**62, limit),
        )
        return [dict(r) for r in rows]

    def prune(self, activity_days: int = 180) -> None:
        self._execute("DELETE FROM activity WHERE at < ?", (int(time.time()) - activity_days * 86400,))

    # ---------- Favorites lists ----------

    def list_favlists(self) -> list[dict]:
        rows = self._query(
            """
            SELECT f.id, f.name, f.user_id, f.created_at, u.username AS owner,
                   COUNT(s.song_id) AS song_count,
                   COALESCE(SUM(json_extract(s.song, '$.duration')), 0) AS duration,
                   (SELECT json_extract(c.song, '$.coverArt') FROM favlist_songs c
                     WHERE c.list_id = f.id AND json_extract(c.song, '$.coverArt') IS NOT NULL
                     ORDER BY c.position LIMIT 1) AS cover_art
            FROM favlists f
            LEFT JOIN users u ON u.id = f.user_id
            LEFT JOIN favlist_songs s ON s.list_id = f.id
            GROUP BY f.id
            ORDER BY f.name COLLATE NOCASE
            """
        )
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "user_id": r["user_id"],
                "owner": r["owner"] or "deleted user",
                "songCount": r["song_count"],
                "duration": int(r["duration"] or 0),
                "coverArt": r["cover_art"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def get_favlist(self, list_id: int) -> dict | None:
        lists = [f for f in self.list_favlists() if f["id"] == list_id]
        if not lists:
            return None
        info = lists[0]
        rows = self._query(
            "SELECT song FROM favlist_songs WHERE list_id = ? ORDER BY position", (list_id,)
        )
        info["songs"] = [json.loads(r["song"]) for r in rows]
        return info

    def create_favlist(self, user_id: int, name: str) -> int:
        return self._execute(
            "INSERT INTO favlists (user_id, name) VALUES (?, ?)", (user_id, name)
        ).lastrowid

    def rename_favlist(self, list_id: int, name: str) -> None:
        self._execute("UPDATE favlists SET name = ? WHERE id = ?", (name, list_id))

    def delete_favlist(self, list_id: int) -> None:
        self._execute("DELETE FROM favlists WHERE id = ?", (list_id,))

    def add_favlist_songs(self, list_id: int, songs: list[dict]) -> int:
        """Append tracks, skipping ones already in the list. Returns the number of tracks added."""
        with self._lock, self._conn:
            existing = {r["song_id"] for r in self._conn.execute(
                "SELECT song_id FROM favlist_songs WHERE list_id = ?", (list_id,))}
            pos = self._conn.execute(
                "SELECT COALESCE(MAX(position), -1) FROM favlist_songs WHERE list_id = ?", (list_id,)
            ).fetchone()[0]
            added = 0
            for song in songs:
                if song["id"] in existing:
                    continue
                pos += 1
                existing.add(song["id"])
                self._conn.execute(
                    "INSERT INTO favlist_songs (list_id, song_id, position, song) VALUES (?, ?, ?, ?)",
                    (list_id, song["id"], pos, json.dumps(song, ensure_ascii=False)),
                )
                added += 1
            return added

    def remove_favlist_song(self, list_id: int, song_id: str) -> None:
        self._execute("DELETE FROM favlist_songs WHERE list_id = ? AND song_id = ?", (list_id, song_id))

    def move_favlist_song(self, list_id: int, src: int, dst: int) -> None:
        with self._lock, self._conn:
            ids = [r["song_id"] for r in self._conn.execute(
                "SELECT song_id FROM favlist_songs WHERE list_id = ? ORDER BY position", (list_id,))]
            if not (0 <= src < len(ids) and 0 <= dst < len(ids)):
                return
            ids.insert(dst, ids.pop(src))
            self._conn.executemany(
                "UPDATE favlist_songs SET position = ? WHERE list_id = ? AND song_id = ?",
                [(i, list_id, song_id) for i, song_id in enumerate(ids)],
            )
