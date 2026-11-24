#!/usr/bin/env python3
"""The agent's status window: model, memory, heat, fan, and the speeds of the
last turn. Small, in thin type, placed beside the agent's own window.

main.py opens it: panel.py <agent pid> <status file> [<agent's tty>]. The
agent writes the status file whenever something changes; the panel also
checks what's loaded and the memory every couple of seconds on its own, so it
stays current between turns. It only reads; if it fails or is closed, the
agent keeps working.

The fan's ramp-up temperature isn't published by macOS, so it is learned: each
time the fan's target speed rises off its idle speed, the chip temperature is
recorded. The marker is the median of the recent readings.
"""

import ctypes as C
import ctypes.util
import json
import os
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path

import config
import context
import inference

STATE_FILE = Path(__file__).parent / ".panel_state.json"  # learned fan marker

SAMPLE_EVERY = 2.0    # seconds between sensor reads (for learning the fan)
FAN_RAMP_RPM = 150    # above the idle speed by this much counts as ramping
FAN_READINGS = 8      # recent ramp temperatures kept
HEAT_MIN, HEAT_MAX = 30, 110  # meter range in °C
BAR_WIDTH = 28
COLUMNS, ROWS = 32, 25  # the window, in characters
FONT, FONT_SIZE = "SFMono-Light", 11  # thin; Terminal ships SF Mono
REFRESH = 2.0  # seconds between checks of what's loaded and the memory

# The colors the agent uses for tool calls (yellow), results (green), and
# notices (cyan). No bold: the window should stay quiet beside the work.
GREY, CYAN, GREEN, YELLOW, RED, RESET = (
    "\033[90m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
)


# -- memory --------------------------------------------------------------

def memory_gb():
    """(used, total) in GB, counted the way Activity Monitor does: app memory
    + wired (which includes models on the GPU) + compressed."""
    total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]))
    output = subprocess.check_output(["vm_stat"], text=True)
    page = int(output.split("page size of ")[1].split()[0])
    pages = {}
    for line in output.splitlines()[1:]:
        key, _, value = line.partition(":")
        if value.strip().rstrip(".").isdigit():
            pages[key.strip()] = int(value.strip().rstrip("."))
    used = (pages["Anonymous pages"] - pages["Pages purgeable"] + pages["Pages wired down"]
            + pages["Pages occupied by compressor"]) * page
    return used / 1024 ** 3, total / 1024 ** 3


# -- model ---------------------------------------------------------------

def loaded_models():
    """What's actually in memory right now, through any driver."""
    return inference.loaded()


# -- chip temperature (IOKit HID sensors, no sudo) -----------------------

class ChipSensors:
    TEMPERATURE_EVENT = 15

    def __init__(self):
        P = C.c_void_p
        cf = self.cf = C.CDLL(ctypes.util.find_library("CoreFoundation"))
        io = self.io = C.CDLL(ctypes.util.find_library("IOKit"))
        cf.CFStringCreateWithCString.restype = P
        cf.CFStringCreateWithCString.argtypes = [P, C.c_char_p, C.c_uint32]
        cf.CFNumberCreate.restype = P
        cf.CFNumberCreate.argtypes = [P, C.c_long, P]
        cf.CFDictionaryCreate.restype = P
        cf.CFDictionaryCreate.argtypes = [P, P, P, C.c_long, P, P]
        cf.CFArrayGetCount.restype = C.c_long
        cf.CFArrayGetCount.argtypes = [P]
        cf.CFArrayGetValueAtIndex.restype = P
        cf.CFArrayGetValueAtIndex.argtypes = [P, C.c_long]
        cf.CFStringGetCString.restype = C.c_bool
        cf.CFStringGetCString.argtypes = [P, C.c_char_p, C.c_long, C.c_uint32]
        cf.CFRelease.argtypes = [P]
        io.IOHIDEventSystemClientCreate.restype = P
        io.IOHIDEventSystemClientCreate.argtypes = [P]
        io.IOHIDEventSystemClientSetMatching.argtypes = [P, P]
        io.IOHIDEventSystemClientCopyServices.restype = P
        io.IOHIDEventSystemClientCopyServices.argtypes = [P]
        io.IOHIDServiceClientCopyProperty.restype = P
        io.IOHIDServiceClientCopyProperty.argtypes = [P, P]
        io.IOHIDServiceClientCopyEvent.restype = P
        io.IOHIDServiceClientCopyEvent.argtypes = [P, C.c_int64, C.c_int32, C.c_int64]
        io.IOHIDEventGetFloatValue.restype = C.c_double
        io.IOHIDEventGetFloatValue.argtypes = [P, C.c_int32]

        def cfstr(text):
            return cf.CFStringCreateWithCString(None, text.encode(), 0x08000100)

        def cfint(value):
            number = C.c_int(value)
            return cf.CFNumberCreate(None, 3, C.byref(number))

        keys = (P * 2)(cfstr("PrimaryUsagePage"), cfstr("PrimaryUsage"))
        values = (P * 2)(cfint(0xff00), cfint(5))
        matching = cf.CFDictionaryCreate(None, keys, values, 2, None, None)
        self.client = io.IOHIDEventSystemClientCreate(None)
        io.IOHIDEventSystemClientSetMatching(self.client, matching)
        services = io.IOHIDEventSystemClientCopyServices(self.client)
        product = cfstr("Product")
        self.dies = []  # the sensors on the chip's dies ("PMU tdie...")
        for i in range(cf.CFArrayGetCount(services) if services else 0):
            service = cf.CFArrayGetValueAtIndex(services, i)
            name = io.IOHIDServiceClientCopyProperty(service, product)
            if not name:
                continue
            buffer = C.create_string_buffer(128)
            if cf.CFStringGetCString(name, buffer, 128, 0x08000100) and b"tdie" in buffer.value:
                self.dies.append(service)
            cf.CFRelease(name)

    def read(self):
        """Average temperature across the chip in °C, or None."""
        readings = []
        for service in self.dies:
            event = self.io.IOHIDServiceClientCopyEvent(service, self.TEMPERATURE_EVENT, 0, 0)
            if event:
                value = self.io.IOHIDEventGetFloatValue(event, self.TEMPERATURE_EVENT << 16)
                self.cf.CFRelease(event)
                if 0 < value < 150:
                    readings.append(value)
        return sum(readings) / len(readings) if readings else None


# -- fan (read-only SMC access) ------------------------------------------

class _Version(C.Structure):
    _fields_ = [("major", C.c_uint8), ("minor", C.c_uint8), ("build", C.c_uint8),
                ("reserved", C.c_uint8), ("release", C.c_uint16)]


class _Limits(C.Structure):
    _fields_ = [("version", C.c_uint16), ("length", C.c_uint16), ("cpu", C.c_uint32),
                ("gpu", C.c_uint32), ("memory", C.c_uint32)]


class _KeyInfo(C.Structure):
    _fields_ = [("size", C.c_uint32), ("type", C.c_uint32), ("attributes", C.c_uint8)]


class _SMCData(C.Structure):
    _fields_ = [("key", C.c_uint32), ("version", _Version), ("limits", _Limits), ("info", _KeyInfo),
                ("result", C.c_uint8), ("status", C.c_uint8), ("command", C.c_uint8),
                ("data32", C.c_uint32), ("bytes", C.c_uint8 * 32)]


class Fan:
    READ_BYTES, READ_INFO = 5, 9

    def __init__(self):
        self.io = C.CDLL(ctypes.util.find_library("IOKit"))
        self.io.IOServiceMatching.restype = C.c_void_p
        self.io.IOServiceMatching.argtypes = [C.c_char_p]
        self.io.IOServiceGetMatchingService.restype = C.c_uint
        self.io.IOServiceGetMatchingService.argtypes = [C.c_uint, C.c_void_p]
        self.io.IOServiceOpen.argtypes = [C.c_uint, C.c_uint, C.c_uint, C.POINTER(C.c_uint)]
        self.io.IOConnectCallStructMethod.argtypes = [
            C.c_uint, C.c_uint, C.c_void_p, C.c_size_t, C.c_void_p, C.POINTER(C.c_size_t)]
        service = self.io.IOServiceGetMatchingService(0, self.io.IOServiceMatching(b"AppleSMC"))
        task = C.c_uint.in_dll(C.CDLL(None), "mach_task_self_").value
        self.connection = C.c_uint()
        if self.io.IOServiceOpen(service, task, 0, C.byref(self.connection)) != 0:
            raise OSError("can't open the SMC")

    def _call(self, request):
        reply, size = _SMCData(), C.c_size_t(C.sizeof(_SMCData))
        status = self.io.IOConnectCallStructMethod(self.connection.value, 2, C.byref(request),
                                                   C.sizeof(_SMCData), C.byref(reply), C.byref(size))
        return None if status or reply.result else reply

    def _float(self, key):
        code = struct.unpack(">I", key.encode())[0]
        info = self._call(_SMCData(key=code, command=self.READ_INFO))
        if info is None or info.info.size != 4:
            return None
        request = _SMCData(key=code, command=self.READ_BYTES)
        request.info.size = 4
        reply = self._call(request)
        return struct.unpack("<f", bytes(reply.bytes[:4]))[0] if reply else None

    def read(self):
        """{'rpm', 'target', 'max'}, or None if unavailable."""
        rpm, target, top = self._float("F0Ac"), self._float("F0Tg"), self._float("F0Mx")
        return None if rpm is None or target is None else {"rpm": rpm, "target": target, "max": top}


class FanLearner:
    """Learns the temperature where the fan ramps up off its idle speed."""

    def __init__(self):
        try:
            state = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            state = {}
        self.idle = state.get("idle_rpm")
        self.readings = state.get("ramp_temps", [])
        self.was_ramping = None

    @property
    def marker(self):
        return statistics.median(self.readings) if self.readings else None

    def update(self, fan, temp):
        if fan is None or temp is None:
            return
        changed = self.idle is None or fan["target"] < self.idle
        if changed:
            self.idle = fan["target"]
        ramping = fan["target"] >= self.idle + FAN_RAMP_RPM
        if ramping and self.was_ramping is False:  # only rising edges: it cools slower than it heats
            self.readings = (self.readings + [round(temp, 1)])[-FAN_READINGS:]
            changed = True
        self.was_ramping = ramping
        if changed:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"idle_rpm": self.idle, "ramp_temps": self.readings}))
            os.replace(tmp, STATE_FILE)


# -- drawing -------------------------------------------------------------

def level_color(fraction):
    return GREEN if fraction < 0.7 else YELLOW if fraction < 0.9 else RED


def bar(fraction, color):
    filled = round(max(0, min(1, fraction)) * BAR_WIDTH)
    return f"{color}{'━' * filled}{GREY}{'─' * (BAR_WIDTH - filled)}{RESET}"


def heat_position(temp):
    fraction = (temp - HEAT_MIN) / (HEAT_MAX - HEAT_MIN)
    return max(0, min(BAR_WIDTH - 1, int(fraction * BAR_WIDTH)))


def heat_color(temp):
    return GREEN if temp < 75 else YELLOW if temp < 95 else RED


def speed(value):
    return f"{value:>9,.1f} {GREY}tok/s{RESET}" if value is not None else f"{GREY}{'—':>9}{RESET}"


def shorten(name, width=COLUMNS):
    return name if len(name) <= width else name[:10] + "…" + name[-(width - 11):]


def label(text):
    return f"{GREY}{text}{RESET}"


def model_lines(status, loaded, engine):
    """The selected model, whether it's actually loaded, and the context."""
    selected = status.get("model")
    lines = [label("model"), shorten(selected) if selected else label("none yet")]
    if loaded is None:
        lines.append(label("can't tell what's loaded"))
    else:
        mine = next((m for m in loaded if m["name"] == selected), None)
        if mine:
            ctx = context.format_size(mine["context_length"]) if mine.get("context_length") else "?"
            lines.append(f"{GREEN}loaded{RESET} {GREY}· {inference.LABELS.get(mine['driver'], '')} · {ctx}{RESET}")
        elif loaded:
            lines.append(f"{YELLOW}in memory:{RESET} {GREY}{shorten(loaded[0]['name'], COLUMNS - 11)}{RESET}")
        else:
            lines.append(label("not loaded · loads on next message"))
    size = status.get("num_ctx")
    if size:
        text = f"{GREY}context{RESET} {context.format_size(size)}"
        top, lock = status.get("max_ctx"), status.get("ctx_lock")
        limit = lock if lock and (not top or lock < top) else top
        if limit:
            text += f" {GREY}of {context.format_size(limit)}{RESET}"
        if status.get("tokens") is not None:
            text += f" {GREY}·{RESET} {round(100 * status['tokens'] / size)}%"
        lines.append(text)
    guard = engine.get("guard_gb")
    lines.append(label(f"{inference.LABELS.get(engine.get('driver'), '')} · guard "
                       f"{f'{guard:g} GB' if guard else 'off'} · {status.get('mode') or 'standard'}"))
    if status.get("gate_flags"):
        lines.append(f"{YELLOW}gate: {status['gate_flags']} flagged{RESET}")
    return lines


def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def timer_line(status):
    worked = status.get("worked")
    if worked is None:
        return label("no prompt yet")
    if status.get("working_since"):
        return f"{YELLOW}working {format_duration(worked + time.time() - status['working_since'])}{RESET}"
    if status.get("paused"):
        return f"{YELLOW}paused at {format_duration(worked)}{RESET} {GREY}· waiting for you{RESET}"
    return f"{GREEN}idle{RESET} {GREY}· worked {format_duration(worked)}{RESET}"


def render(status, memory, temp, fan, marker, loaded, engine):
    """The window's text."""
    status = status or {}
    lines = [f"{CYAN}{config.AGENT_NAME} Status{RESET}", timer_line(status)]
    lines.append(label(f"{status['turn']} · {status.get('updated', '')}" if status.get("turn")
                       else "waiting for the first turn"))

    lines += [""] + model_lines(status, loaded, engine)
    lines += ["", label("memory")]
    if memory:
        used, total = memory
        lines += [bar(used / total, level_color(used / total)), f"{used:.1f} {GREY}of {total:.0f} GB in use{RESET}"]
    else:
        lines += [label("unavailable"), ""]

    lines += ["", label("heat")]
    if temp is not None:
        lines.append(bar((temp - HEAT_MIN) / (HEAT_MAX - HEAT_MIN), heat_color(temp)))
        if marker is not None:
            position = heat_position(marker)
            note = f"▲ fan ramps ~{marker:.0f}°C"
            lines.append(f"{GREY}{' ' * min(position, COLUMNS - len(note))}{note}{RESET}")
        else:
            lines.append(label("fan marker: still learning"))
        fan_text = f" {GREY}· fan{RESET} {fan['rpm']:,.0f} {GREY}rpm{RESET}" if fan else ""
        lines.append(f"{GREY}chip{RESET} {temp:.0f}°C{fan_text}")
    else:
        lines += [label("unavailable"), "", ""]

    lines += ["", label("speed · last turn"),
              f"{GREY}decoding{RESET} {speed(status.get('decoding'))}",
              f"{GREY}thinking{RESET} {speed(status.get('thinking'))}",
              f"{GREY}writing {RESET} {speed(status.get('writing'))}"]
    return "\033[H" + "".join(line + "\033[K\n" for line in lines) + "\033[J"


# -- main ----------------------------------------------------------------

def attempt(read):
    try:
        return read()
    except Exception:
        return None


def agent_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def close_own_window():
    """Close this Terminal window, and only this one, just after exiting."""
    if not sys.stdout.isatty():
        return
    script = ('tell application "Terminal"\n'
              "repeat with w in windows\n"
              "repeat with t in tabs of w\n"
              f'if tty of t is "{os.ttyname(1)}" then\n'
              "close w saving no\nreturn\nend if\n"
              "end repeat\nend repeat\nend tell")
    subprocess.Popen(["sh", "-c", 'sleep 0.5; osascript -e "$0"', script], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


PLACE_SCRIPT = """
on run argv
    set agentTTY to item 1 of argv
    set ownTTY to item 2 of argv
    set screenRight to (item 3 of argv) as integer
    tell application "Terminal"
        set agentId to missing value
        set ownId to missing value
        repeat with w in windows
            repeat with t in tabs of w
                if tty of t is agentTTY then set agentId to id of w
                if tty of t is ownTTY then set ownId to id of w
            end repeat
        end repeat
        if ownId is missing value then return
        set own to window id ownId
        try
            set font name of selected tab of own to item 4 of argv
            set font size of selected tab of own to (item 5 of argv) as integer
        end try
        set number of columns of selected tab of own to (item 6 of argv) as integer
        set number of rows of selected tab of own to (item 7 of argv) as integer
        if agentId is missing value then return
        set {ax1, ay1, ax2, ay2} to bounds of window id agentId
        set {ox1, oy1, ox2, oy2} to bounds of own
        set w to ox2 - ox1
        set h to oy2 - oy1
        set x to ax2 + 8
        if x + w > screenRight then set x to ax1 - w - 8
        if x < 0 then set x to screenRight - w
        set bounds of own to {x, ay1, x + w, ay1 + h}
    end tell
end run
"""


def screen_width():
    """The main display's width in points."""
    graphics = C.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")

    class Rect(C.Structure):
        _fields_ = [("x", C.c_double), ("y", C.c_double), ("w", C.c_double), ("h", C.c_double)]

    graphics.CGMainDisplayID.restype = C.c_uint32
    graphics.CGDisplayBounds.restype = Rect
    graphics.CGDisplayBounds.argtypes = [C.c_uint32]
    return int(graphics.CGDisplayBounds(graphics.CGMainDisplayID()).w)


def place_window(agent_tty):
    """Thin font, small size, and beside the agent's window (not on top)."""
    if not sys.stdout.isatty():
        return
    subprocess.run(["osascript", "-e", PLACE_SCRIPT, agent_tty or "", os.ttyname(1),
                    str(attempt(screen_width) or 1440), FONT, str(FONT_SIZE), str(COLUMNS), str(ROWS)],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)


def main():
    agent_pid, status_file = int(sys.argv[1]), Path(sys.argv[2])
    agent_tty = sys.argv[3] if len(sys.argv) > 3 else ""
    sys.stdout.write(f"\033]0;{config.AGENT_NAME} Status\007\033[?25l\033[2J")
    sys.stdout.flush()
    attempt(lambda: place_window(agent_tty))
    chip, fan_sensor, learner = attempt(ChipSensors), attempt(Fan), FanLearner()
    status, seen, next_sample, next_check = {}, None, 0, 0
    temp = fan = memory = loaded = None
    engine, shown = {}, None
    try:
        while agent_alive(agent_pid):
            now = time.monotonic()
            if now >= next_sample:
                temp = attempt(chip.read) if chip else None
                fan = attempt(fan_sensor.read) if fan_sensor else None
                attempt(lambda: learner.update(fan, temp))
                next_sample = now + SAMPLE_EVERY
            if now >= next_check:  # between turns too: loads, unloads, switches
                memory, loaded = attempt(memory_gb), attempt(loaded_models)
                engine = attempt(inference.settings) or {}
                next_check = now + REFRESH
            stamp = attempt(lambda: status_file.stat().st_mtime_ns)
            if stamp is not None and stamp != seen:
                fresh = attempt(lambda: json.loads(status_file.read_text()))
                if fresh:
                    seen, status = stamp, fresh
                    status["updated"] = time.strftime("%-I:%M:%S %p")
                    memory, loaded = attempt(memory_gb), attempt(loaded_models)
                    engine = attempt(inference.settings) or {}
            screen = render(status, memory, temp, fan, learner.marker, loaded, engine)
            if screen != shown:  # redraw only on a change (the timer ticks each second)
                sys.stdout.write(screen)
                sys.stdout.flush()
                shown = screen
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        attempt(status_file.unlink)
    close_own_window()


if __name__ == "__main__":
    main()
