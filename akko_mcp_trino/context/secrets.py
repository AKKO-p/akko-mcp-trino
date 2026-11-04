"""Secrets that come from a file: a mounted secret is read again when it is rotated."""

import logging
import os
import threading
from collections.abc import Mapping

log = logging.getLogger(__name__)


def _identity(status: os.stat_result) -> tuple[int, int, int, int]:
    """What changes when a mounted secret is rotated: the file, its time and its size."""
    return (status.st_dev, status.st_ino, status.st_mtime_ns, status.st_size)


class FileSecret:
    """A secret held in a file, read again when the file is replaced or its time or size changes."""

    def __init__(self, path: str, name: str = "") -> None:
        """Name the file, and the setting it belongs to for the log; nothing is read yet."""
        self._path = path
        self._name = name or "secret file"
        self._lock = threading.Lock()
        self._seen: tuple[int, int, int, int] | None = None
        self._value = ""

    def value(self) -> str:
        """Return the secret, read again if the file changed since the last read.

        A file rotated to an empty or undecodable content keeps the last good value and
        logs one warning, with the setting and the path only. Raises ``OSError`` when the
        file cannot be read, and ``UnicodeError`` (without text) when the first read cannot
        be decoded.
        """
        with self._lock:
            seen = _identity(os.stat(self._path))
            if seen == self._seen:
                return self._value
            try:
                with open(self._path, encoding="utf-8") as handle:
                    content = handle.read().strip()
            except UnicodeError:
                if not self._value:
                    raise UnicodeError("the secret file cannot be decoded") from None
                self._keep(seen, "cannot be decoded")
                return self._value
            if not content and self._value:
                self._keep(seen, "is empty")
                return self._value
            self._value, self._seen = content, seen
            return self._value

    def _keep(self, seen: tuple[int, int, int, int], why: str) -> None:
        self._seen = seen
        log.warning(
            "%s: the file %r %s after a change; the last good value is still served",
            self._name,
            self._path,
            why,
        )

    def stamp(self) -> str:
        """Return a value that changes with the file, and never holds the secret."""
        try:
            status = os.stat(self._path)
        except OSError:
            return "missing"
        return ":".join(str(part) for part in _identity(status))


def secret_setting(env: Mapping[str, str], name: str) -> FileSecret | str:
    """Return the secret ``name``: a ``FileSecret`` when ``<name>_FILE`` is set, else the
    plain variable, or an empty string. A file that cannot be read, decoded or is empty
    raises ``ValueError``, so the server refuses to start; the message names the setting,
    the path and the type of the failure, never its text."""
    path = env.get(f"{name}_FILE", "")
    if not path:
        return env.get(name, "")
    secret = FileSecret(path, f"{name}_FILE")
    try:
        content = secret.value()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{name}_FILE: cannot read {path!r} ({type(exc).__name__})") from None
    if not content:
        raise ValueError(f"{name}_FILE: the file is empty")
    return secret
