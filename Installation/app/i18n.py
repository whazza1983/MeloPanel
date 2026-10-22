"""Translations (German/English) for the Discord bot and server error messages.

Discord: the base language of the slash commands is English; German is sent as a
localization. Discord automatically shows each user the language of their client.
"""

import discord
from discord import app_commands

LANGS = ("de", "en")


def lang_of(locale) -> str:
    """Discord locale or language code -> 'de' or 'en'."""
    return "de" if str(locale or "").lower().startswith("de") else "en"


# ---------- Bot responses ----------

BOT = {
    "not_in_voice": ("Geh zuerst in einen Sprachkanal.", "Join a voice channel first."),
    "nothing_found": ("Nichts gefunden für **{q}**.", "Nothing found for **{q}**."),
    "added": ("Hinzugefügt: {track}", "Added: {track}"),
    "playing_now": ("Spiele jetzt: {track}", "Now playing: {track}"),
    "no_album": ("Kein Album gefunden für **{q}**.", "No album found for **{q}**."),
    "album_added": ("Album **{name}** – {artist} ({n} Titel) hinzugefügt.",
                    "Added album **{name}** – {artist} ({n} tracks)."),
    "no_playlist": ("Keine Playlist gefunden für **{q}**.", "No playlist found for **{q}**."),
    "playlist_added": ("Playlist **{name}** ({n} Titel) hinzugefügt.", "Added playlist **{name}** ({n} tracks)."),
    "no_favlist": ("Keine Favoritenliste gefunden für **{q}**.", "No favorites list found for **{q}**."),
    "favlist_empty": ("Die Liste **{name}** ist noch leer.", "The list **{name}** is still empty."),
    "favlist_added": ("⭐ Favoritenliste **{name}** ({n} Titel) hinzugefügt.",
                      "⭐ Added favorites list **{name}** ({n} tracks)."),
    "favlist_choice": ("{name} ({n} Titel, von {owner})", "{name} ({n} tracks, by {owner})"),
    "no_radio": ("Kein Radiosender gefunden für **{q}**.", "No radio station found for **{q}**."),
    "radio_live": ("📻 Jetzt live: **{name}**", "📻 Now live: **{name}**"),
    "random_added": ("{n} zufällige Titel hinzugefügt.", "Added {n} random tracks."),
    "joined": ("Bin in **{channel}**.", "Joined **{channel}**."),
    "bye": ("Tschüss! 👋", "Bye! 👋"),
    "nothing_playing": ("Es läuft gerade nichts.", "Nothing is playing right now."),
    "skipped": ("⏭️ Übersprungen.", "⏭️ Skipped."),
    "previous": ("⏮️ Zurück.", "⏮️ Back."),
    "paused": ("⏸️ Pausiert.", "⏸️ Paused."),
    "resumed": ("▶️ Weiter geht's.", "▶️ Resumed."),
    "stopped": ("⏹️ Gestoppt.", "⏹️ Stopped."),
    "program_list": ("🔁 Dauerschleife: **{name}** ({mode}). Wünsche mit `/play` laufen dazwischen.",
                     "🔁 Nonstop: **{name}** ({mode}). Requests via `/play` play in between."),
    "program_radio": ("📻 Dauerprogramm: **{name}**. Wünsche mit `/play` laufen dazwischen.",
                      "📻 Nonstop radio: **{name}**. Requests via `/play` play in between."),
    "mode_shuffle": ("gemischt", "shuffled"),
    "mode_order": ("in Reihenfolge", "in order"),
    "no_program": ("Es läuft keine Dauerschleife.", "No nonstop playback is running."),
    "program_stopped": ("Dauerschleife **{name}** beendet.", "Stopped nonstop **{name}**."),
    "volume_set": ("🔊 Lautstärke: {n}%", "🔊 Volume: {n}%"),
    "loop_set": ("🔁 Wiederholung: {mode}", "🔁 Repeat: {mode}"),
    "loop_off": ("Aus", "Off"),
    "loop_track": ("Titel", "Track"),
    "loop_queue": ("Warteschlange", "Queue"),
    "shuffled": ("🔀 Warteschlange gemischt.", "🔀 Queue shuffled."),
    "seek_format": ("Format: `1:30` oder `90`", "Format: `1:30` or `90`"),
    "seeked": ("⏩ Gesprungen zu {time}.", "⏩ Jumped to {time}."),
    "live_radio": ("Live-Radio", "Live radio"),
    "playing_for": ("Läuft seit", "Playing for"),
    "position": ("Position", "Position"),
    "volume": ("Lautstärke", "Volume"),
    "repeat": ("Wiederholung", "Repeat"),
    "added_by": ("Hinzugefügt von {who}", "Added by {who}"),
    "queue_title": ("Warteschlange", "Queue"),
    "queue_more": ("… und {n} weitere", "… and {n} more"),
    "queue_empty": ("Die Warteschlange ist leer.", "The queue is empty."),
    "queue_program": ("\n🔁 Danach Dauerschleife: **{name}**{mode}", "\n🔁 Then nonstop: **{name}**{mode}"),
    "round": ("Runde {n}", "round {n}"),
    "order": ("Reihenfolge", "in order"),
    "panel": ("🎛️ Webpanel: {url}", "🎛️ Web panel: {url}"),
    "no_panel_url": ("Im .env ist keine PANEL_URL eingetragen.", "No PANEL_URL is set in .env."),
    "cmd_disabled": ("Dieser Befehl ist im Panel ausgeschaltet.", "This command is disabled in the panel."),
    "wrong_channel": ("Bitte benutze die Bot-Befehle in <#{channel}>.", "Please use bot commands in <#{channel}>."),
    "dj_only": ("Dafür brauchst du die Rolle <@&{role}>.", "You need the <@&{role}> role for this."),
    "admin_only": ("Das dürfen nur Server-Admins.", "Only server admins can do that."),
    "navidrome_error": ("Navidrome-Fehler: {error}", "Navidrome error: {error}"),
    "generic_error": ("Da ist etwas schiefgelaufen.", "Something went wrong."),
}


def bt(lang: str, key: str, **kwargs) -> str:
    """Bot text in the requested language."""
    text = BOT[key][0 if lang == "de" else 1]
    return text.format(**kwargs) if kwargs else text


# ---------- Server error messages (panel) ----------
# The code uses the English texts; they are translated to German here.

ERRORS_DE = {
    "Not logged in": "Nicht angemeldet",
    "Admins only": "Nur für Admins",
    "Too many failed attempts. Please try again in a few minutes.":
        "Zu viele Fehlversuche. Bitte in ein paar Minuten erneut versuchen.",
    "Wrong username or password": "Benutzername oder Passwort falsch",
    "Current password is wrong": "Altes Passwort ist falsch",
    "Username: 3–32 characters, only letters, digits, _ . -":
        "Benutzername: 3–32 Zeichen, nur Buchstaben, Zahlen, _ . -",
    "Username is already taken": "Benutzername ist schon vergeben",
    "User not found": "Benutzer nicht gefunden",
    "You can't delete yourself": "Du kannst dich nicht selbst löschen",
    "You can't remove your own admin rights": "Du kannst dir die Admin-Rechte nicht selbst entziehen",
    "Invalid list": "Ungültige Liste",
    "The stream URL must start with http:// or https://":
        "Die Stream-Adresse muss mit http:// oder https:// beginnen",
    "The homepage must start with http:// or https://": "Die Homepage muss mit http:// oder https:// beginnen",
    "The bot is not connected to the Discord server (yet)":
        "Bot ist (noch) nicht mit dem Discord-Server verbunden",
    "Voice channel not found": "Sprachkanal nicht gefunden",
    "No tracks given": "Keine Titel angegeben",
    "Favorites list not found": "Favoritenliste nicht gefunden",
    "Unknown type": "Unbekannter Typ",
    "This list belongs to someone else": "Diese Liste gehört jemand anderem",
    "The favorites list is empty or doesn't exist": "Die Favoritenliste ist leer oder existiert nicht",
    "Unknown nonstop program": "Unbekanntes Dauerprogramm",
    "Radio station not found": "Radiosender nicht gefunden",
    "Cover not found": "Cover nicht gefunden",
    "Unknown error from Navidrome": "Unbekannter Fehler von Navidrome",
    "Unknown command": "Unbekannter Befehl",
    "Role not found": "Rolle nicht gefunden",
    "Text channel not found": "Textkanal nicht gefunden",
    "Please choose at least one weekday": "Bitte mindestens einen Wochentag wählen",
    "Entry not found": "Eintrag nicht gefunden",
}

# Messages with a variable part: replace the prefix
ERROR_PREFIXES_DE = {
    "Could not join:": "Konnte nicht beitreten:",
    "Navidrome responded with HTTP": "Navidrome antwortet mit HTTP",
    "Navidrome not reachable:": "Navidrome nicht erreichbar:",
    "Navidrome:": "Navidrome:",
}


def translate_error(message: str, lang: str) -> str:
    if lang != "de" or not isinstance(message, str):
        return message
    if message in ERRORS_DE:
        return ERRORS_DE[message]
    for en, de in ERROR_PREFIXES_DE.items():
        if message.startswith(en):
            rest = message[len(en):]
            # e.g. "Navidrome: Navidrome not reachable: …" -> translate the rest as well
            if rest.startswith(" "):
                return f"{de} {translate_error(rest[1:], lang)}"
            return de + rest
    return message


# ---------- Slash commands: German localization ----------
# The base language (keys) is English.

COMMANDS_DE = {
    # Command names (only where they differ)
    "favorites": "favoriten",
    "nonstop": "dauerschleife",
    "list": "liste",
    # Parameter names
    "title": "titel",
    "now": "sofort",
    "shuffle": "mischen",
    "station": "sender",
    "count": "anzahl",
    "percent": "prozent",
    "time": "zeit",
    "mode": "modus",
    # Descriptions
    "Search for a track and play it": "Einen Titel suchen und abspielen",
    "Title or artist": "Titel oder Künstler",
    "Play immediately instead of queueing": "Sofort abspielen statt hinten anstellen",
    "Play a whole album": "Ein ganzes Album abspielen",
    "Album name": "Albumname",
    "Play a Navidrome playlist": "Eine Navidrome-Playlist abspielen",
    "Playlist name": "Name der Playlist",
    "Play a favorites list from the web panel": "Eine Favoritenliste aus dem Webpanel abspielen",
    "Name of the favorites list": "Name der Favoritenliste",
    "Shuffle the order": "Reihenfolge zufällig mischen",
    "Listen to a radio station": "Einen Radiosender hören",
    "Station name": "Name des Senders",
    "Play random tracks from the library": "Zufällige Titel aus der Bibliothek abspielen",
    "How many tracks (1–100)": "Wie viele Titel (1–100)",
    "Bring the bot into your voice channel": "Den Bot in deinen Sprachkanal holen",
    "Leave the voice channel": "Den Sprachkanal verlassen",
    "Skip to the next track": "Zum nächsten Titel springen",
    "Go to the previous track": "Zum vorherigen Titel",
    "Pause playback": "Wiedergabe pausieren",
    "Resume playback": "Wiedergabe fortsetzen",
    "Stop everything: playback, queue and nonstop": "Alles stoppen: Wiedergabe, Warteschlange und Dauerschleife",
    "Favorites list or radio on nonstop repeat": "Favoritenliste oder Radio in Dauerschleife",
    "Play a favorites list over and over": "Eine Favoritenliste immer wieder abspielen",
    "In order or shuffled": "Reihenfolge oder gemischt",
    "Keep a radio station running": "Einen Radiosender dauerhaft laufen lassen",
    "Stop nonstop playback": "Die Dauerschleife beenden",
    "Set the volume": "Lautstärke einstellen",
    "Volume in percent (0–100)": "Lautstärke in Prozent (0–100)",
    "Set repeat mode": "Wiederholung einstellen",
    "What to repeat": "Was wiederholt werden soll",
    "Shuffle the queue": "Warteschlange mischen",
    "Jump to a position in the track": "Zu einer Stelle im Titel springen",
    "e.g. 1:30 or 90": "z. B. 1:30 oder 90",
    "Show the current track": "Zeigt den aktuellen Titel",
    "Show the queue": "Zeigt die Warteschlange",
    "Link to the web panel": "Link zum Webpanel",
    # Choices
    "In order": "Reihenfolge",
    "Shuffled": "Gemischt",
    "Off": "Aus",
    "Track": "Titel",
    "Queue": "Warteschlange",
}


class BotTranslator(app_commands.Translator):
    async def translate(self, string: app_commands.locale_str, locale: discord.Locale,
                        context: app_commands.TranslationContextTypes) -> str | None:
        if locale is discord.Locale.german:
            return COMMANDS_DE.get(string.message)
        return None
