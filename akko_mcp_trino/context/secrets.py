"""Secrets that come from a file: a mounted secret is read again when it is rotated."""

import os
import threading
from collections.abc import Mapping


class FileSecret:
    """A secret held in a file, read again when the file's modification time or size changes."""

    def __init__(self, path: str) -> None:
        """Name the file; nothing is read before ``value``."""
        self._path = path
        self._lock = threading.Lock()
        self._seen: tuple[int, int] | None = None
        self._value = ""

    def value(self) -> str:
        """Return the secret, read again if the file changed since the last read."""
        with self._lock:
            status = os.stat(self._path)
            seen = (status.st_mtime_ns, status.st_size)
            if seen != self._seen:
                with open(self._path, encoding="utf-8") as handle:
                    self._value = handle.read().strip()
                self._seen = seen
            return self._value

    def stamp(self) -> str:
        """Return a value that changes with the file, and never holds the secret."""
        try:
            status = os.stat(self._path)
        except OSError:
            return "missing"
        return f"{status.st_mtime_ns}:{status.st_size}"


def secret_setting(env: Mapping[str, str], name: str) -> FileSecret | str:
    """Return the secret ``name``: a ``FileSecret`` when ``<name>_FILE`` is set, else the
    plain variable, or an empty string. A file that cannot be read or is empty raises
    ``ValueError``, so the server refuses to start."""
    path = env.get(f"{name}_FILE", "")
    if not path:
        return env.get(name, "")
    secret = FileSecret(path)
    try:
        content = secret.value()
    except OSError as exc:
        raise ValueError(f"{name}_FILE: cannot read {path!r} ({type(exc).__name__})") from exc
    if not content:
        raise ValueError(f"{name}_FILE: the file is empty")
    return secret
