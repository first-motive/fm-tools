"""Keep an interactive child's terminal inside the app's output and reply panes.

This helper owns the PTY in a fresh interpreter. Signals reach the child process
 group, and shutdown waits for it. No workflow receives the application's TTY.
"""

from __future__ import annotations
import errno
import fcntl
import struct
import os
import pty
import select
import signal
import sys
import termios
import time
from .runner import invocation
from fm_tools.cli.exits import from_returncode


def main() -> int:
    size_fd = int(os.environ.pop("FM_TUI_SIZE_FD", "-1"))
    child, master = pty.fork()
    if child == 0:
        if size_fd >= 0:
            os.close(size_fd)
        fcntl.ioctl(
            0,
            termios.TIOCSWINSZ,
            struct.pack(
                "HHHH",
                int(os.environ.pop("FM_TUI_ROWS", "24")),
                int(os.environ.pop("FM_TUI_COLUMNS", "80")),
                0,
                0,
            ),
        )
        attrs = termios.tcgetattr(0)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(0, termios.TCSANOW, attrs)
        os.execv(sys.executable, invocation(tuple(sys.argv[1:])))
    deadline = None

    def stop(signum, frame):
        nonlocal deadline
        deadline = time.monotonic() + 3
        try:
            os.killpg(child, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    inputs = [master, 0] + ([size_fd] if size_fd >= 0 else [])
    size_buffer = b""
    try:
        while master in inputs:
            if deadline is not None and time.monotonic() >= deadline:
                try:
                    os.killpg(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                deadline = None
            ready, _, _ = select.select(inputs, [], [], 0.1)
            for descriptor in ready:
                try:
                    data = os.read(descriptor, 65536)
                except OSError as exc:
                    if descriptor == master and exc.errno == errno.EIO:
                        inputs.remove(master)
                        break
                    raise
                if not data:
                    inputs.remove(descriptor)
                    if descriptor == 0:
                        stop(signal.SIGTERM, None)
                    continue
                if descriptor == size_fd:
                    size_buffer += data
                    while b"\n" in size_buffer:
                        line, size_buffer = size_buffer.split(b"\n", 1)
                        columns, rows = map(int, line.split())
                        fcntl.ioctl(
                            master,
                            termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, columns, 0, 0),
                        )
                        try:
                            os.killpg(child, signal.SIGWINCH)
                        except ProcessLookupError:
                            pass
                elif descriptor == master:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                else:
                    while data:
                        data = data[os.write(master, data) :]
        _, status = os.waitpid(child, 0)
        return from_returncode(os.waitstatus_to_exitcode(status))
    finally:
        os.close(master)
        if size_fd >= 0:
            os.close(size_fd)
        try:
            os.killpg(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
