from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict
from datetime import datetime
from typing import Any

from zapret_zen.domain import LogEntry
from zapret_zen.services.storage import StorageManager


class LoggingManager:
    def __init__(self, storage: StorageManager) -> None:
        self.storage = storage
        self.log_path = self.storage.paths.logs_dir / "app.log"
        self.zapret_log_path = self.storage.paths.logs_dir / "zapret.log"
        self.tg_log_path = self.storage.paths.logs_dir / "tg_ws_proxy.log"
        self.reset_runtime_logs()

    def reset_runtime_logs(self) -> None:
        self._rotate_app_log(max_bytes=1_500_000)
        for path in (
            self.zapret_log_path,
            self.tg_log_path,
            self.storage.paths.logs_dir / "tg_worker_error.log",
        ):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
            except Exception:
                continue

    def _rotate_app_log(self, *, max_bytes: int) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.log_path.exists() or self.log_path.stat().st_size <= max_bytes:
                return
            lines = self.log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            self.log_path.write_text("\n".join(lines[-900:]) + "\n", encoding="utf-8")
        except Exception:
            return

    def source_log_path(self, source: str) -> str:
        source_id = (source or "").strip().lower()
        if source_id == "zapret":
            return str(self.zapret_log_path)
        if source_id == "tg-ws-proxy":
            return str(self.tg_log_path)
        return str(self.log_path)

    def log(self, level: str, message: str, **context: Any) -> LogEntry:
        entry = LogEntry(
            timestamp=datetime.utcnow().isoformat(),
            level=level.upper(),
            message=message,
            context=context,
        )
        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        return entry

    def read_entries(self) -> list[LogEntry]:
        if not self.log_path.exists():
            return []
        entries: list[LogEntry] = []
        for line in self.log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                entries.append(LogEntry(**payload))
            except Exception:
                entries.append(LogEntry(timestamp="", level="INFO", message=line.strip(), context={}))
        return entries

    def read_source_lines(self, source: str, limit: int = 250) -> list[str]:
        source_id = (source or "app").strip().lower()
        if source_id == "app":
            return self._format_entries(self.read_entries()[-limit:])
        if source_id == "zapret":
            lines = self._format_entries(
                [
                    entry
                    for entry in self.read_entries()
                    if str(entry.context.get("component_id", "") or "") == "zapret"
                    or "zapret" in entry.message.lower()
                ][-limit:]
            )
            lines.extend(self._read_plain_log_tail("zapret.log", limit=limit, heading=None))
            return lines[-limit:]
        if source_id == "tg-ws-proxy":
            entries = [
                entry
                for entry in self.read_entries()
                if str(entry.context.get("component_id", "") or "") == "tg-ws-proxy"
                or "tg ws proxy" in entry.message.lower()
                or "telegram proxy" in entry.message.lower()
            ]
            lines = self._format_entries(entries[-limit:])
            lines.extend(self._read_plain_log_tail("tg_ws_proxy.log", limit=limit, heading=None))
            lines.extend(self._read_plain_log_tail("tg_worker_error.log", limit=80, heading="tg_worker_error.log"))
            return lines[-limit:] if len(lines) > limit else lines
        if source_id == "all":
            return self._read_all_source_lines(limit=limit)
        return self._format_entries(self.read_entries()[-limit:])

    def _read_all_source_lines(self, *, limit: int) -> list[str]:
        records: list[tuple[float, int, str]] = []
        ordinal = 0
        app_entries = self.read_entries()[-limit:]
        for entry, line in zip(app_entries, self._format_entries(app_entries)):
            stamp = self._entry_timestamp(entry)
            records.append((stamp, ordinal, f"[app] {line}"))
            ordinal += 1
        for source, filename, source_limit in (
            ("zapret", "zapret.log", limit),
            ("tg-ws-proxy", "tg_ws_proxy.log", limit),
            ("tg-ws-proxy", "tg_worker_error.log", 80),
        ):
            for stamp, line in self._read_plain_log_tail_records(filename, limit=source_limit):
                records.append((stamp, ordinal, f"[{source}] {line}"))
                ordinal += 1
        records.sort(key=lambda item: (item[0], item[1]))
        return [line for _stamp, _ordinal, line in records[-limit:]]

    def _format_entries(self, entries: list[LogEntry]) -> list[str]:
        lines: list[str] = []
        for entry in entries:
            context_suffix = ""
            if entry.context:
                useful = {key: value for key, value in entry.context.items() if value not in ("", None, [], {})}
                if useful:
                    context_suffix = " | " + ", ".join(f"{key}={value}" for key, value in useful.items())
            stamp = str(entry.timestamp or "").replace("T", " ")[:19]
            prefix = f"[{stamp}] " if stamp else ""
            lines.append(f"{prefix}{entry.level}: {entry.message}{context_suffix}")
        return lines

    def _entry_timestamp(self, entry: LogEntry) -> float:
        try:
            raw = str(entry.timestamp or "").replace("Z", "+00:00")
            return datetime.fromisoformat(raw).timestamp()
        except Exception:
            return 0.0

    def _line_timestamp(self, line: str) -> float | None:
        prepared = str(line or "").strip()
        if not prepared.startswith("[") or "]" not in prepared:
            return None
        token = prepared[1 : prepared.find("]")].strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(token).timestamp()
        except Exception:
            return None

    def _read_plain_log_tail_records(self, filename: str, *, limit: int) -> list[tuple[float, str]]:
        path = self.storage.paths.logs_dir / filename
        if not path.exists():
            return []
        lines = self._read_plain_log_tail(filename, limit=limit, heading=None)
        if not lines:
            return []
        try:
            fallback_stamp = path.stat().st_mtime
        except Exception:
            fallback_stamp = 0.0
        count = max(1, len(lines))
        records: list[tuple[float, str]] = []
        for index, line in enumerate(lines):
            stamp = self._line_timestamp(line)
            if stamp is None:
                stamp = fallback_stamp - ((count - index) * 0.000001)
            records.append((stamp, line))
        return records

    def _read_plain_log_tail(self, filename: str, *, limit: int, heading: str | None = None) -> list[str]:
        path = self.storage.paths.logs_dir / filename
        if not path.exists():
            return []
        tail = deque(maxlen=limit)
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip():
                tail.append(line)
        if not tail:
            return []
        prefix = [f"=== {heading or filename} ==="] if heading else []
        return prefix + list(tail)
