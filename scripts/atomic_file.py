"""os.replace that survives Windows readers holding the target open."""

import os
import time


def replace(source, target, attempts=40, delay=0.05):
    # On Windows, replacing a file another process is reading raises PermissionError
    # (WinError 5/32) until that reader closes it — usually within milliseconds.
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == attempts - 1:
                raise
            time.sleep(delay)
