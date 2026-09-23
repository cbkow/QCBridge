"""Temp-file-then-rename, tolerant of Windows.

The smoke halves publish their state as `<name>.json` by writing
`<name>.json.tmp` and `os.replace`-ing it over the old file, so a poller
never reads a half-written document. On POSIX that is atomic and always
succeeds. On Windows a rename over a file that another process has open
without FILE_SHARE_DELETE (CPython's `open()` is one) fails with
PermissionError (WinError 5) — and the poller opens that very file every
half second. Found by the coverage survey on 2026-09-23: the host died at
step 102 and 19 rows went inconclusive. Retrying for a moment is enough;
the reader closes the file within microseconds.
"""
import os
import time


def replace(tmp: str, dst: str, attempts: int = 40, pause: float = 0.025) -> None:
    for i in range(attempts):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(pause)
