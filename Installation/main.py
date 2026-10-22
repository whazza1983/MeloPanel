import asyncio
import logging
import shutil

import uvicorn

from app.bot import MusicBot
from app.config import load_config
from app.db import Database
from app.navidrome import Navidrome, NavidromeError
from app.web import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main")


async def main() -> None:
    config = load_config()

    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found. Install it with: sudo apt install ffmpeg")

    db = Database(config.db_path)
    db.ensure_admin(config.admin_username, config.admin_password)

    navidrome = Navidrome(config.navidrome_url, config.navidrome_user,
                          config.navidrome_password, config.navidrome_max_bitrate)
    try:
        await navidrome.ping()
        log.info("Connection to Navidrome (%s) ok.", config.navidrome_url)
    except NavidromeError as exc:
        log.error("Navidrome check failed: %s – continuing anyway.", exc)

    bot = MusicBot(config, navidrome, db)
    app = create_app(config, db, bot)

    server = uvicorn.Server(uvicorn.Config(
        app,
        host=config.web_host,
        port=config.web_port,
        proxy_headers=True,
        forwarded_allow_ips=config.trusted_proxies,
        log_level="warning",
    ))
    log.info("Web panel running at http://%s:%s", config.web_host, config.web_port)

    web_task = asyncio.create_task(server.serve(), name="web")
    bot_task = asyncio.create_task(bot.start(config.discord_token), name="bot")
    try:
        done, _ = await asyncio.wait({web_task, bot_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.exception():
                raise task.exception()
    finally:
        server.should_exit = True
        if not bot.is_closed():
            await bot.close()
        await navidrome.close()
        await asyncio.gather(web_task, bot_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
