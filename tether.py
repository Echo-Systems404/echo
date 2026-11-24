"""Tether: runs a model server and kills it once the Echo process that
started it is gone, even if Echo was force-killed or its terminal closed.

    python3 tether.py <echo pid> -- <server command...>

It checks every TETHER_POLL seconds. Nothing ever restarts the server: the
next Echo request starts a new one. Only the standard library, so any Python
can run it.
"""

import os
import signal
import subprocess
import sys
import time

TETHER_POLL = 1.0
STOP_GRACE = 5.0  # seconds between asking the server to stop and killing it


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main(argv):
    if len(argv) < 4:
        print("usage: tether.py <pid> [--pidfile <path>] -- <command...>", file=sys.stderr)
        return 2
    try:
        owner = int(argv[1])
    except ValueError:
        print("usage: tether.py <pid> [--pidfile <path>] -- <command...>", file=sys.stderr)
        return 2

    pidfile = None
    if len(argv) >= 6 and argv[2] == "--pidfile" and argv[4] == "--":
        pidfile = argv[3]
        cmd = argv[5:]
    elif argv[2] == "--":
        cmd = argv[3:]
    else:
        print("usage: tether.py <pid> [--pidfile <path>] -- <command...>", file=sys.stderr)
        return 2

    server = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, start_new_session=True)
    if pidfile:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(pidfile)), exist_ok=True)
            with open(pidfile, "w", encoding="utf-8") as f:
                f.write(str(server.pid) + "\n")
        except OSError:
            pass

    def stop(*_):
        if server.poll() is None:
            try:
                os.killpg(server.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                server.wait(STOP_GRACE)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(server.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                server.wait()
        if pidfile:
            try:
                os.unlink(pidfile)
            except OSError:
                pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, stop)
    while True:
        if server.poll() is not None:
            if pidfile:
                try:
                    os.unlink(pidfile)
                except OSError:
                    pass
            return server.returncode
        if not _alive(owner) or os.getppid() == 1:
            stop()
        time.sleep(TETHER_POLL)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
