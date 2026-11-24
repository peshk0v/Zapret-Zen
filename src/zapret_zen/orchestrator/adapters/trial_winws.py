"""Runs a candidate desync recipe in an isolated winws instance.

Isolation contract (borrowed from NetZapret):

* the trial instance captures **only** probe traffic, selected by the source
  port lane;
* the production instance excludes the same lane (see ``ProcessManager``), so
  the two engines never desync the same packet;
* trials are serialized, short-lived and always killed with the whole tree.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from zapret_zen.orchestrator.domain.models import ProbeResult, Recipe

_READY_MARKERS = ("capture is started", "windivert initialized")


class WinwsTrialRunner:
    def __init__(self, install_root: Path, data_dir: Path, *, lane: tuple[int, int] = (40000, 40100), ready_timeout_s: float = 6.0) -> None:
        self.install_root = Path(install_root)
        self.probe_dir = Path(data_dir) / "orchestrator" / "probe"
        self.lane = (int(lane[0]), int(lane[1]))
        self.ready_timeout_s = max(1.0, float(ready_timeout_s))
        self._lock = threading.Lock()
        self._winws = self._find_winws()
        self._creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

    @property
    def available(self) -> bool:
        return self._winws is not None

    @property
    def executable(self) -> str:
        return str(self._winws) if self._winws else ""

    def run(
        self,
        host: str,
        recipe: Recipe,
        prober: Any,
        *,
        port: int = 443,
        path: str = "/",
        source_port: int | None = None,
    ) -> ProbeResult | None:
        if self._winws is None or not host:
            return None
        if not self._lock.acquire(blocking=False):
            return None
        process: subprocess.Popen[Any] | None = None
        lines: list[str] = []
        try:
            self.probe_dir.mkdir(parents=True, exist_ok=True)
            hostlist = self.probe_dir / "recipe.txt"
            hostlist.write_text(host.strip().lower() + "\n", encoding="utf-8")
            start, end = self.lane
            port = max(1, min(65535, int(port)))
            source_filter = (
                f"((tcp.SrcPort>={start} and tcp.SrcPort<={end})"
                f" or (tcp.DstPort>={start} and tcp.DstPort<={end}))"
            )
            command = [
                str(self._winws),
                f"--wf-tcp={port}",
                f"--wf-raw-part={source_filter}",
                f"--filter-tcp={port}",
                f"--hostlist={hostlist}",
                *recipe.args,
            ]
            process = subprocess.Popen(
                command,
                cwd=str(self._winws.parent),
                creationflags=self._creationflags,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            reader = threading.Thread(target=_drain, args=(process, lines), daemon=True)
            reader.start()
            if not self._wait_ready(process, lines):
                return None
            time.sleep(0.25)
            return prober.probe(host, port=port, path=path, recipe=recipe.id, source_port=source_port)
        except Exception:
            return None
        finally:
            if lines:
                try:
                    (self.probe_dir / "winws-trial.log").write_text("".join(lines), encoding="utf-8", errors="replace")
                except OSError:
                    pass
            if process is not None:
                self._terminate(process)
            self._lock.release()

    def _wait_ready(self, process: subprocess.Popen[Any], lines: list[str]) -> bool:
        deadline = time.monotonic() + self.ready_timeout_s
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            text = "".join(lines).lower()
            if any(marker in text for marker in _READY_MARKERS):
                return True
            time.sleep(0.1)
        return False

    def _terminate(self, process: subprocess.Popen[Any]) -> None:
        try:
            if process.poll() is None:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/F", "/T"],
                    creationflags=self._creationflags,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                )
        except Exception:
            pass
        try:
            if process.poll() is None:
                process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=2)
        except Exception:
            pass
        try:
            if process.stdout is not None:
                process.stdout.close()
        except Exception:
            pass

    def _find_winws(self) -> Path | None:
        candidates = [
            self.install_root / "merged_runtime" / "zapret" / "bin" / "winws.exe",
            self.install_root / "runtime" / "zapret-discord-youtube" / "bin" / "winws.exe",
        ]
        for candidate in candidates:
            try:
                if candidate.exists():
                    return candidate
            except OSError:
                continue
        return None


def _drain(process: subprocess.Popen[Any], lines: list[str]) -> None:
    try:
        if process.stdout is None:
            return
        for line in process.stdout:
            if len(lines) < 2000:
                lines.append(line)
    except Exception:
        pass
