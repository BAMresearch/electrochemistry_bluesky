"""Run ``tiled serve ...`` in this process for ``LocalTiledServer``.

    python -m electrochemistry_bluesky._tiled_server PARENT_PID serve catalog ...

Two additions to the plain ``tiled`` command:

- the API key (from ``TILED_SINGLE_USER_API_KEY``) is redacted from everything
  the server prints, so it never reaches ``server.log``;
- the server stops when the process that started it is gone, also when that
  process was killed (a hard-killed QueueServer worker or notebook kernel), so
  no orphan keeps the store's catalog and lock.
"""

import os
import re
import signal
import sys
import threading
import time
from typing import TextIO

_KEY_IN_URL = re.compile(r"(api_key=)[^\s&\"']+")


class _Redacting:
    """A text stream that hides the API key before writing to ``stream``."""

    def __init__(self, stream: TextIO, key: str) -> None:
        self._stream, self._key = stream, key

    def write(self, text: str) -> int:
        if self._key:
            text = text.replace(self._key, "<api-key>")
        self._stream.write(_KEY_IN_URL.sub(r"\1<api-key>", text))
        return len(text)

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


def _stop_with(parent: int, poll_s: float = 1.0) -> None:
    """Stop this server once ``parent`` is no longer its parent process."""
    while os.getppid() == parent:
        time.sleep(poll_s)
    print("The process that started this Tiled server has ended; stopping.", file=sys.stderr)
    os.kill(os.getpid(), signal.SIGTERM)  # uvicorn shuts down cleanly
    time.sleep(15)
    os._exit(1)


def main() -> None:
    parent = int(sys.argv[1])
    key = os.environ.get("TILED_SINGLE_USER_API_KEY", "")
    sys.stdout = _Redacting(sys.stdout, key)
    sys.stderr = _Redacting(sys.stderr, key)
    threading.Thread(target=_stop_with, args=(parent,), daemon=True).start()
    sys.argv = ["tiled", *sys.argv[2:]]
    from tiled.commandline.main import main as tiled_main

    tiled_main()


if __name__ == "__main__":
    main()
