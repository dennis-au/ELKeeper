"""Streaming command execution for compatibility workers and adapters."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence


def stream_command(command: Sequence[str], on_line: Callable[[str], None]) -> int:
    """Run a command and forward complete combined-output lines in order."""

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if process.stdout:
        try:
            for line in process.stdout:
                on_line(line)
        finally:
            process.stdout.close()
    return process.wait()
