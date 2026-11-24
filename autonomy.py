"""Autonomous mode: a small background runner for scheduled tasks, and for
Telegram (communication/telegram/) whenever that's set up.

Echo starts it from the terminal, so it keeps the folder access macOS already
gave the terminal. It keeps running after Echo closes and stops at a restart
or logout, then comes back the next time Echo opens. While any Echo window is
open, that window runs the tasks and this runner waits, so a task never
competes with a conversation for the model.
"""

import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import config
import context
import errorlog
import inference
import sentinel
import tasks
import turns
from communication.telegram import bridge as telegram
from communication.telegram import store as telegram_store

POLL_SECONDS = 5
MAX_LOG_BYTES = 1_000_000


def _file(name):
    config.OPTIONS_DIR.mkdir(parents=True, exist_ok=True)
    return config.OPTIONS_DIR / name


def _lock(name, mode):
    handle = open(_file(name), "a+")
    try:
        fcntl.flock(handle, mode | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def is_running():
    handle = _lock(".autonomy.lock", fcntl.LOCK_EX)
    if handle is None:
        return True
    handle.close()
    return False


def hold_session():
    """Marks Echo as open for as long as the returned file stays open."""
    handle = open(_file(".session.lock"), "a+")
    fcntl.flock(handle, fcntl.LOCK_SH)
    return handle


def _session_open():
    handle = _lock(".session.lock", fcntl.LOCK_EX)
    if handle is None:
        return True
    handle.close()
    return False


def start():
    if is_running():
        return
    logs = _file("logs")
    logs.mkdir(exist_ok=True)
    with open(logs / "autonomous.log", "a") as log:
        subprocess.Popen([sys.executable, "-B", "-u", str(Path(__file__).resolve())],
                         cwd=str(Path(__file__).resolve().parent), stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def stop():
    if not is_running():
        return
    try:
        pid = int(_file(".autonomy.lock").read_text().strip())
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        return
    deadline = time.time() + 5
    while time.time() < deadline and is_running():
        time.sleep(0.1)


def _autonomous():
    clock = tasks.load_clock()
    return clock["enabled"] and clock["autonomous"]


def wanted():
    """The runner runs for autonomous tasks, or to serve Telegram."""
    return _autonomous() or telegram_store.ready()


def sync():
    """Make the runner match the clock and Telegram settings."""
    if wanted():
        start()
    else:
        stop()


def _interrupt(*_):
    raise KeyboardInterrupt


HERE = Path(__file__).resolve().parent


def _code_stamp():
    """Changes whenever a harness .py file changes."""
    files = list(HERE.glob("*.py")) + list((HERE / "communication").rglob("*.py"))
    return max((p.stat().st_mtime_ns for p in files), default=0)


def _loads():
    """Whether the changed code starts cleanly. A half-finished edit (a file
    imported before it exists) must not kill the runner; it keeps the old
    code until the new code loads."""
    try:
        done = subprocess.run([sys.executable, "-B", "-c", "import autonomy, tasks, main, communication.telegram.bridge"], cwd=str(HERE),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _restart(lock):
    """Run the new harness code: replace this process with a fresh runner."""
    print(f"{time.strftime('%Y-%m-%d %H:%M')} Harness code changed; restarting the runner.", flush=True)
    telegram.stop()  # its conversation continues after the restart
    lock.close()
    os.execve(sys.executable, [sys.executable, "-B", "-u", str(Path(__file__).resolve())],
              {**os.environ, "ECHO_CAFFEINATED": "1"})  # same pid, so caffeinate keeps watching it


def run_forever():
    lock = _lock(".autonomy.lock", fcntl.LOCK_EX)
    if lock is None:
        return
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGHUP, _interrupt)
    stamp = _code_stamp()
    if sys.platform == "darwin" and not os.environ.get("ECHO_CAFFEINATED"):
        # idle sleep would pause the schedule; the display can still sleep
        try:
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            print("caffeinate isn't available, so the Mac may sleep.", flush=True)
    def fit(model, size):
        try:
            return context.fitted(inference.driver().facts(model), size)
        except Exception:
            return size

    ran = False  # a task ran this cycle, so a model may be loaded

    next_check = 0
    try:
        while True:
            clock = tasks.load_clock()
            autonomous = clock["enabled"] and clock["autonomous"]
            if not (autonomous or telegram_store.ready()):
                return
            telegram.start()  # no-op while it's already serving or isn't set up
            if _code_stamp() != stamp and not tasks.running() and not turns.busy():
                if _loads():
                    _restart(lock)
                stamp = _code_stamp()  # broken code: keep running this version until the next change
                print(f"{time.strftime('%Y-%m-%d %H:%M')} Changed harness code doesn't load yet; "
                      "keeping the running version.", flush=True)
            try:
                sentinel.tick()  # the harness's own security watch: no model, every SENTINEL_MINUTES
            except Exception as exc:
                errorlog.log_error("autonomy.sentinel", exc)
            now = time.time()
            asked = tasks.requested() is not None  # Echo was asked to run a task now (chat or Telegram)
            if sentinel.due_response() or asked:
                next_check = 0  # answer now, not at the next cycle
            if (autonomous or asked) and now >= next_check and not _session_open() and not tasks.running() \
                    and not turns.busy():
                task = tasks.next_due(now)
                if task is None and ran:
                    inference.driver().release()  # the cycle is over: free the memory until the next one
                    ran = False
                if task is not None:
                    driver = inference.driver()
                    ready = driver.healthy() if driver.name == "ollama" else driver.available()[0]
                    if not ready:
                        print(f"{time.strftime('%H:%M')} {driver.label} isn't available, will retry.", flush=True)
                        next_check = now + clock["minutes"] * 60
                    else:
                        if os.fstat(1).st_size > MAX_LOG_BYTES:
                            os.ftruncate(1, 0)
                        print(f"{time.strftime('%Y-%m-%d %H:%M')} Task {task['id']}: {task['name']}", flush=True)
                        ran = True
                        result, _ = tasks.run(task, inference.default_model(), config.NUM_CTX, fit_context=fit)
                        print(f"{time.strftime('%H:%M')} Task {task['id']} finished: {result}", flush=True)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        return
    finally:
        telegram.stop()
        inference.driver().release()  # the runner stopped: nothing stays loaded


if __name__ == "__main__":
    run_forever()
