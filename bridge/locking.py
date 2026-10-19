from __future__ import annotations

import os
from pathlib import Path

from .config import ConfigError


class InstanceLock:
    def __init__(self, database: Path):
        self.path = Path(str(database) + ".lock")
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ConfigError("Мост уже запущен с этой базой; сначала остановите его.") from None
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()
