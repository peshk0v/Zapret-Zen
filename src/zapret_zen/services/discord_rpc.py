"""Discord Rich Presence client using pypresence."""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

APP_ID = "1539264958054006825"
RPC_DETAIL = "Использует для обхода блокировок"


class DiscordRPCService:
    def __init__(self) -> None:
        self._detail = RPC_DETAIL
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._running

    def configure(self, enabled: bool) -> None:
        if enabled and not self._running:
            self.start()
        elif not enabled and self._running:
            self.stop()

    def start(self) -> None:
        if self._running:
            return
        self._stop_event = threading.Event()
        self._running = True
        self._thread = threading.Thread(
            target=self._run, args=(self._stop_event,),
            daemon=True, name="discord-rpc",
        )
        self._thread.start()
        logger.info("Discord RPC: started")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        self._thread = None
        logger.info("Discord RPC: stopped")

    def _connect(self):
        import pypresence
        rpc = pypresence.Presence(APP_ID)
        rpc.connect()
        return rpc

    def _set_activity(self, rpc, detail: str) -> bool:
        try:
            rpc.update(
                details=detail,
                large_image="ds_icon",
                large_text="Zapret Zen",
                buttons=[
                    {"label": "Подробнее", "url": "https://zapret-zen.neocities.org/"},
                ],
            )
            logger.info("Discord RPC: presence set (detail=%s)", detail)
            return True
        except Exception as exc:
            logger.warning("Discord RPC: set_activity failed: %s", exc)
            return False

    def _close(self, rpc) -> None:
        if rpc is not None:
            try:
                rpc.close()
            except Exception:
                pass

    def _run(self, stop: threading.Event) -> None:
        rpc = None
        while self._running and not stop.is_set():
            try:
                self._close(rpc)
                rpc = self._connect()
                logger.info("Discord RPC: connected")
                if not self._set_activity(rpc, self._detail):
                    stop.wait(timeout=3)
                    continue
                while self._running and not stop.is_set():
                    stop.wait(timeout=15)
                    if self._running and not stop.is_set():
                        if not self._set_activity(rpc, self._detail):
                            logger.info("Discord RPC: reconnecting...")
                            break
                break
            except Exception as exc:
                logger.warning("Discord RPC: %s", exc)
                stop.wait(timeout=5)
            finally:
                self._close(rpc)
                rpc = None
