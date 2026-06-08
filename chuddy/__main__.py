"""Chuddy bot entry point."""

import asyncio
import logging
import signal
import sys

import uvloop

from chuddy.bot import ChuddyBot
from chuddy.config import get_config, settings
from chuddy.utils import remove_dir


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    # Suppress noisy libraries
    logging.getLogger('aiosqlite').setLevel(logging.WARNING)
    logging.getLogger('pyrogram').setLevel(logging.WARNING)
    logging.getLogger('pyrogram.session').setLevel(logging.WARNING)


async def main():
    setup_logging()
    log = logging.getLogger('chuddy')

    # Initialize DB
    import chuddy.db as db
    db.init_db(settings.DATA_DIR)

    # Load config
    config = get_config()

    # Create and start bot
    bot = ChuddyBot(config)

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler():
        log.info('Shutdown signal received')
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        await bot.start_bot()
        log.info('Bot is running')
        await shutdown_event.wait()
    except Exception:
        log.exception('Bot crashed')
        raise
    finally:
        log.info('Shutting down...')
        try:
            await bot.stop()
        except Exception:
            pass
        try:
            await db.close_db()
        except Exception:
            pass
        # Clean up temp download dirs
        try:
            for subdir in ('downloading', 'downloaded'):
                p = settings.TMP_DOWNLOAD_ROOT_PATH / subdir
                if p.exists():
                    remove_dir(p)
        except Exception:
            pass
        log.info('Shutdown complete')


if __name__ == '__main__':
    uvloop.install()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    sys.exit(0)
