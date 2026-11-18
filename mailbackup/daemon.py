"""Long-running 5-minute loop with graceful shutdown."""

from __future__ import annotations

import logging
import signal
import threading

from .backup import BackupEngine
from .config import Config

log = logging.getLogger("mailbackup.daemon")


def run_forever(engine: BackupEngine, config: Config) -> int:
    stop = threading.Event()

    def handle(signum, _frame):
        log.info("received signal %s; finishing up", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    log.info(
        "daemon started: every %ds, data in %s, remote %s",
        config.interval_seconds,
        config.data_dir,
        engine.backend,
    )
    while not stop.is_set():
        try:
            engine.run_cycle()
        except Exception as exc:
            log.error("cycle failed: %s", exc)
            log.debug("cycle failure detail", exc_info=True)
        log.info("sleeping %ds", config.interval_seconds)
        if stop.wait(config.interval_seconds):
            break
    log.info("daemon stopped")
    return 0
