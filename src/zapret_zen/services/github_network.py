from __future__ import annotations

import json
import ssl
import time
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi

from zapret_zen import __version__
from zapret_zen.services.logging_service import LoggingManager

from urllib.parse import quote, urlsplit, urlunsplit

T = TypeVar("T")


class DownloadCancelledError(Exception):
    """Raised when the caller requests cancellation of an in-flight download."""


def encode_url_path(url: str) -> str:
    parts = urlsplit(str(url or ""))
    if not parts.scheme or not parts.netloc:
        return str(url or "")
    encoded = "/".join(seg if "%" in seg else quote(seg, safe="@:") for seg in parts.path.split("/"))
    return urlunsplit((parts.scheme, parts.netloc, encoded, parts.query, parts.fragment))


def _read_error_body(error: HTTPError) -> str:
    try:
        data = error.read(4096)
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data)
    except Exception:
        return ""


def is_github_rate_limit_error(error: BaseException) -> bool:
    if isinstance(error, HTTPError):
        if error.code == 429:
            return True
        if error.code == 403:
            return "rate limit" in _read_error_body(error).lower()
        return False
    text = str(error).lower()
    return "rate limit" in text


def is_recoverable_github_error(error: BaseException) -> bool:
    if is_github_rate_limit_error(error):
        return False
    if isinstance(error, HTTPError):
        return error.code in {403, 429, 500, 502, 503, 504}
    if isinstance(error, (URLError, TimeoutError, OSError, ssl.SSLError)):
        return True
    text = str(error).lower()
    return any(marker in text for marker in ("timed out", "timeout", "temporary failure", "certificate"))


class GitHubNetworkClient:
    def __init__(
        self,
        logging: LoggingManager,
        *,
        recovery_runner: Callable[[Callable[[], T], str], T] | None = None,
    ) -> None:
        self.logging = logging
        self.recovery_runner = recovery_runner

    def github_json(self, url: str, *, timeout: int = 20, purpose: str = "github-json", retry: bool = True) -> object:
        if retry:
            return self._run(lambda: self._request_json(url, timeout=timeout), purpose)
        return self._request_json(url, timeout=timeout)

    def github_bytes(self, url: str, *, timeout: int = 60, purpose: str = "github-download", retry: bool = True) -> bytes:
        if retry:
            return self._run(lambda: self._download_bytes_once(url, timeout=timeout), purpose)
        return self._download_bytes_once(url, timeout=timeout)

    def probe_url(self, url: str, *, timeout: int = 5) -> bool:
        request = Request(encode_url_path(url), headers={"User-Agent": f"ZapretZen/{__version__}"})
        for _label, context in self._ssl_context_chain():
            try:
                with urlopen(request, timeout=timeout, context=context) as response:
                    return bool(response.status == 200)
            except Exception:
                continue
        return False

    def download_stream(
        self,
        url: str,
        destination: Path,
        *,
        connect_timeout: int = 10,
        read_timeout: int = 10,
        total_timeout: int = 60,
        purpose: str = "download",
        min_bytes: int = 1,
        progress_cb: Callable[[int, int, float | None], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        request = Request(encode_url_path(url), headers={"User-Agent": f"ZapretZen/{__version__}"})
        deadline = time.monotonic() + max(1, total_timeout)
        errors: list[str] = []
        for label, context in self._ssl_context_chain():
            if is_cancelled is not None and is_cancelled():
                raise DownloadCancelledError()
            try:
                with urlopen(request, timeout=connect_timeout, context=context) as response:
                    self.logging.log("info", "Download started", url=url, ssl_path=label)
                    total = self._content_length(response)
                    received = 0
                    with destination.open("wb") as out:
                        while True:
                            if is_cancelled is not None and is_cancelled():
                                raise DownloadCancelledError()
                            if time.monotonic() > deadline:
                                raise TimeoutError(f"Download timed out after {total_timeout} seconds")
                            chunk = response.read(65536)
                            if not chunk:
                                break
                            out.write(chunk)
                            received += len(chunk)
                            if progress_cb is not None:
                                progress_cb(received, total, None if total <= 0 else received / total)
                    if progress_cb is not None:
                        progress_cb(received, total, 1.0)
                if destination.stat().st_size < max(1, min_bytes):
                    raise OSError("Downloaded archive is unexpectedly small")
                return
            except DownloadCancelledError:
                raise
            except Exception as error:
                errors.append(f"{label}: {error}")
                if not self._is_certificate_error(error):
                    raise
                self.logging.log("warning", "Download certificate fallback", url=url, ssl_path=label, error=str(error))
        raise RuntimeError("; ".join(errors) or "Download failed")

    def github_download(
        self,
        url: str,
        destination: Path,
        *,
        timeout: int = 60,
        purpose: str = "github-download",
        min_bytes: int = 1,
        progress_cb: Callable[[int, int, float | None], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.download_stream(
            url,
            destination,
            connect_timeout=timeout,
            read_timeout=timeout,
            total_timeout=timeout,
            purpose=purpose,
            min_bytes=min_bytes,
            progress_cb=progress_cb,
            is_cancelled=is_cancelled,
        )

    @staticmethod
    def _content_length(response: Any) -> int:
        try:
            value = response.headers.get("Content-Length")
            if value is None:
                return 0
            return int(value)
        except Exception:
            return 0

    def _run(self, operation: Callable[[], T], purpose: str) -> T:
        errors: list[str] = []
        for attempt in range(2):
            try:
                return operation()
            except Exception as error:
                errors.append(str(error))
                if not is_recoverable_github_error(error):
                    raise
                self.logging.log("warning", "GitHub request retry", purpose=purpose, attempt=attempt + 1, error=str(error))
                time.sleep(0.8)
        if self.recovery_runner is not None:
            try:
                return self.recovery_runner(operation, purpose)
            except Exception as error:
                errors.append(str(error))
                raise RuntimeError("; ".join(errors)) from error
        raise RuntimeError("; ".join(errors) or "GitHub request failed")

    def _request_json(self, url: str, *, timeout: int) -> object:
        payload = self._download_bytes_once(url, timeout=timeout)
        if not payload.strip():
            raise RuntimeError("GitHub returned an empty response.")
        text = payload.decode("utf-8", errors="replace").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            preview = text[:120].replace("\r", " ").replace("\n", " ")
            raise RuntimeError(f"GitHub returned invalid JSON: {preview}") from error

    def _download_bytes_once(self, url: str, *, timeout: int) -> bytes:
        request = Request(encode_url_path(url), headers={"User-Agent": f"ZapretZen/{__version__}"})
        errors: list[str] = []
        for label, context in self._ssl_context_chain():
            try:
                with urlopen(request, timeout=timeout, context=context) as response:
                    self.logging.log("info", "GitHub request succeeded", url=url, ssl_path=label)
                    return response.read()
            except Exception as error:
                errors.append(f"{label}: {error}")
                if not self._is_certificate_error(error):
                    raise
                self.logging.log("warning", "GitHub certificate fallback", url=url, ssl_path=label, error=str(error))
        raise RuntimeError("; ".join(errors) or "GitHub request failed")

    def _ssl_context_chain(self) -> list[tuple[str, ssl.SSLContext]]:
        return [
            ("system", ssl.create_default_context()),
            ("certifi", ssl.create_default_context(cafile=certifi.where())),
        ]

    def _is_certificate_error(self, error: BaseException) -> bool:
        if isinstance(error, ssl.SSLCertVerificationError):
            return True
        if isinstance(error, URLError):
            reason = getattr(error, "reason", None)
            if isinstance(reason, ssl.SSLCertVerificationError):
                return True
            if isinstance(reason, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(reason).upper():
                return True
        return "CERTIFICATE_VERIFY_FAILED" in str(error).upper()
