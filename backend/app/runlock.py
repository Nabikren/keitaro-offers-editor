"""Эксклюзивная блокировка «backend работает с этой базой».

Её держит запущенный backend (на время жизни приложения) и офлайн-сверка `python -m app.recover`.
Это блокировка ОС на файле рядом с SQLite-базой: при аварийном завершении процесса ОС снимает её сама,
поэтому «зависшего» замка не бывает, а сверка гарантированно не идёт параллельно с работающим backend.
"""

import os
from pathlib import Path


class LockBusy(RuntimeError):
    pass


def lock_path_for(database_url: str) -> Path | None:
    """Файл блокировки для SQLite-файла; для in-memory/других СУБД — None (блокировка не используется)."""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url.startswith("sqlite:///:memory:"):
        return None
    return Path(database_url[len(prefix):] + ".lock")


class RunLock:
    def __init__(self, path: Path | None):
        self.path = path
        self._fh = None

    def acquire(self, holder: str) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            raise LockBusy(
                f"База {self.path.with_suffix('')} уже используется другим процессом (backend или офлайн-сверка). "
                f"{holder} не запущен."
            ) from e
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None
