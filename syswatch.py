#!/usr/bin/env python3
"""SysWatch - a tiny, dependency-free multi-host stats TUI.

Monitors the local machine plus any number of remote hosts over SSH, and
probes services (ports, HTTP, systemd units, docker containers, processes,
commands). Hosts and services are declared in a TOML config so adding another
PC later is a single block.

    syswatch                    # live TUI
    syswatch --once             # one plain-text snapshot
    syswatch --json             # one JSON snapshot
    syswatch --config FILE
"""

import argparse
import base64
import curses
import json
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import collector  # noqa: E402

COLLECTOR_PATH = os.path.join(HERE, "collector.py")
CONFIG_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), ".config", "syswatch", "config.toml"),
    os.path.join(HERE, "config.toml"),
    "/etc/syswatch/config.toml",
]

ATTR = {}
USE_ASCII = False
UI = {"interval": 2.0, "sample": 0.15}


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------

def human_bytes(n):
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < 1024.0:
            if unit == "B":
                return "{:.0f}{}".format(n, unit)
            return "{:.1f}{}".format(n, unit)
        n /= 1024.0
    return "{:.1f}E".format(n)


def human_uptime(sec):
    sec = int(sec or 0)
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return "{}d{}h".format(days, hours)
    if hours:
        return "{}h{}m".format(hours, mins)
    return "{}m".format(mins)


def pct_attr(pct, warn=70.0, crit=90.0):
    if pct >= crit:
        return ATTR.get("crit", 0)
    if pct >= warn:
        return ATTR.get("warn", 0)
    return ATTR.get("ok", 0)


def temp_attr(c):
    if c >= 80:
        return ATTR.get("crit", 0)
    if c >= 65:
        return ATTR.get("warn", 0)
    return ATTR.get("ok", 0)


SPARK = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def sparkline(values, width, fixed_max=None):
    data = list(values)[-width:]
    if not data:
        return ""
    chars = ".:-=+*#@" if USE_ASCII else SPARK
    if len(data) == 1:
        data = [data[0], data[0]]
    if fixed_max is not None:
        lo, hi = 0.0, float(fixed_max)
    else:
        lo, hi = min(data), max(data)
    rng = (hi - lo) or 1.0
    return "".join(chars[int((v - lo) / rng * (len(chars) - 1))] for v in data)


def dot(online):
    if USE_ASCII:
        return "[+]" if online else "[ ]"
    return "\u25cf" if online else "\u25cb"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def find_config(explicit=None):
    if explicit:
        return explicit
    for path in CONFIG_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


DEFAULT_CONFIG = """\
# SysWatch config. Add another machine by copying a [[hosts]] block below.

[settings]
interval = 2.0          # refresh cadence in seconds
sample = 0.15           # internal sampling window for cpu/net rates
ssh_control_persist = "120"

# ------------------------------------------------------------------ hosts --
[[hosts]]
id = "grug"
name = "Grug"
kind = "local"          # local = this machine, ssh = remote over ssh
disks = ["/"]           # filesystems to report
gpu = true              # set false to skip gpu probing
procs = true            # set false to skip top-process scan

[[hosts.services]]
name = "SSH"
type = "port"           # port | http | systemd | docker | process | command
port = 22

[[hosts.services]]
name = "SysWatch API"
type = "port"
port = 8899

[[hosts]]
id = "server"
name = "Server"
kind = "ssh"
host = "192.168.110.40"
user = "dev"
port = 22
disks = ["/"]
# identity = "~/.ssh/id_ed25519"   # optional key
gpu = false              # APU reports a stuck gpu_busy_percent here
# python = "python3"               # remote interpreter

[[hosts.services]]
name = "Minecraft"
type = "port"
port = 25565

[[hosts.services]]
name = "PostgreSQL"
type = "port"
host = "192.168.110.40"   # bind address (use host for LAN-only binds)
port = 5433

[[hosts.services]]
name = "MC Control API"
type = "port"
host = "127.0.0.1"
port = 8899

[[hosts.services]]
name = "Ollama"
type = "port"
host = "127.0.0.1"
port = 11434

[[hosts.services]]
name = "Docker"
type = "systemd"
unit = "docker.service"

[[hosts.services]]
name = "Samba"
type = "port"
port = 445

# ------------------------------------------------------------- more hosts --
# [[hosts]]
# id = "phone"
# name = "Phone"
# kind = "ssh"
# host = "192.168.110.84"
# user = "u0_a322"
# port = 8022
# disks = ["/"]
#
# [[hosts.services]]
# name = "Minecraft (Forge)"
# type = "port"
# port = 25565

# ------------------------------------------- checks run from this machine --
# [[services]]
# name = "Router"
# type = "http"
# url = "http://192.168.110.1/"
# expect = 200
#
# [[services]]
# name = "MC API tunnel"
# type = "command"
# command = "curl -fsS --max-time 3 http://127.0.0.1:8899/api/servers"
"""


def load_config(path):
    if not path:
        os.makedirs(os.path.dirname(CONFIG_CANDIDATES[0]), exist_ok=True)
        with open(CONFIG_CANDIDATES[0], "w") as fh:
            fh.write(DEFAULT_CONFIG)
        path = CONFIG_CANDIDATES[0]
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    settings = cfg.setdefault("settings", {})
    settings.setdefault("interval", 2.0)
    settings.setdefault("sample", 0.15)
    settings.setdefault("ssh_control_persist", "120")
    settings.setdefault("remote_python", "python3")
    cfg.setdefault("hosts", [])
    for i, host in enumerate(cfg["hosts"]):
        host.setdefault("id", host.get("host", "host{}".format(i)))
        host.setdefault("name", host["id"])
        host.setdefault("kind", "local" if host.get("local") else "ssh")
        host.setdefault("disks", ["/"])
        host.setdefault("services", [])
        host.setdefault("connect_timeout", 8)
        host.setdefault("timeout", 25)
    return cfg


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

_SSH_OPTS_CACHE = {}


def ssh_options(cfg, host):
    persist = str(cfg["settings"].get("ssh_control_persist", "120"))
    opts = [
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout={}".format(host.get("connect_timeout", 8)),
        "-o", "ServerAliveInterval=15",
        "-o", "ControlMaster=auto",
        "-o", "ControlPath={}".format(os.path.join(tempfile.gettempdir(), "syswatch-ssh-%C")),
        "-o", "ControlPersist={}".format(persist),
    ]
    if host.get("identity"):
        opts += ["-i", os.path.expanduser(host["identity"])]
    if host.get("port"):
        opts += ["-p", str(host["port"])]
    opts += list(host.get("ssh_extra", []))
    return opts


def fetch_local(host, cfg):
    return collector.collect(
        host.get("services"),
        host.get("disks", ["/"]),
        cfg["settings"].get("sample", 0.15),
        host.get("gpu", True),
        host.get("procs", True),
    )


def fetch_remote(host, cfg):
    with open(COLLECTOR_PATH, "r", errors="replace") as fh:
        source = fh.read()
    payload = base64.b64encode(json.dumps({
        "services": host.get("services", []),
        "disks": host.get("disks", ["/"]),
        "sample": cfg["settings"].get("sample", 0.15),
        "gpu": host.get("gpu", True),
        "procs": host.get("procs", True),
    }).encode()).decode()
    target = host.get("user", "") + "@" + host["host"] if host.get("user") else host["host"]
    python = host.get("python") or cfg["settings"].get("remote_python", "python3")
    cmd = ["ssh"] + ssh_options(cfg, host) + [target, python, "-", payload]
    proc = subprocess.run(cmd, input=source, capture_output=True, text=True,
                          timeout=host.get("timeout", 25))
    if proc.returncode != 0:
        err = (proc.stderr or "ssh failed").strip().splitlines()
        raise RuntimeError(err[0] if err else "ssh failed")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("bad collector output")


def fetch_host(host, cfg):
    if host.get("kind") == "local":
        return fetch_local(host, cfg)
    return fetch_remote(host, cfg)


class HostState:
    def __init__(self, host):
        self.host = host
        self.id = host["id"]
        self.name = host.get("name", self.id)
        self.data = None
        self.error = None
        self.latency = None
        self.updated = 0.0
        self.cpu_hist = deque(maxlen=90)
        self.mem_hist = deque(maxlen=90)
        self.lock = threading.Lock()
        self.wake = threading.Event()

    def push(self, data, latency):
        mem = data.get("memory", {})
        mem_pct = (mem.get("used", 0) / mem["total"] * 100.0) if mem.get("total") else 0.0
        with self.lock:
            self.data = data
            self.error = None
            self.latency = latency
            self.updated = time.time()
            self.cpu_hist.append(data.get("cpu", {}).get("percent", 0.0))
            self.mem_hist.append(mem_pct)

    def fail(self, message):
        with self.lock:
            self.error = message


class ExternalState(HostState):
    def __init__(self, services):
        super().__init__({"id": "_external", "name": "External checks"})
        self.services = services


def worker(state, cfg, stop_event):
    interval = float(cfg["settings"].get("interval", 2.0))
    while not stop_event.is_set():
        start = time.time()
        try:
            if isinstance(state, ExternalState):
                state.push({
                    "identity": {"hostname": "local"},
                    "services": [collector.probe_service(s) for s in state.services],
                }, time.time() - start)
            else:
                state.push(fetch_host(state.host, cfg), time.time() - start)
        except Exception as exc:  # noqa: BLE001
            state.fail("{}".format(exc))
        elapsed = time.time() - start
        remaining = max(0.5, interval - elapsed)
        if state.wake.wait(timeout=remaining):
            state.wake.clear()
        if stop_event.wait(timeout=0):
            return


# --------------------------------------------------------------------------
# curses rendering
# --------------------------------------------------------------------------

def put(win, y, x, text, attr=0, maxw=None):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w or not text:
        return
    if maxw is not None:
        text = text[:max(0, maxw)]
    avail = w - x
    if avail <= 0:
        return
    text = text[:avail]
    if x + len(text) >= w:
        text = text[:avail - 1]
    if not text:
        return
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def draw_bar(win, y, x, width, pct, attr):
    filled_char = "#" if USE_ASCII else "\u2588"
    empty_char = "-" if USE_ASCII else "\u2591"
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100.0 * width))
    filled = max(0, min(width, filled))
    put(win, y, x, filled_char * filled, attr)
    put(win, y, x + filled, empty_char * (width - filled), ATTR.get("dim", 0))
    return width


def init_colors():
    global ATTR
    ATTR = {"ok": 0, "warn": 0, "crit": 0, "title": 0, "dim": 0, "accent": 0, "border": 0}
    if not curses.has_colors():
        ATTR["title"] = curses.A_BOLD
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(1, curses.COLOR_CYAN, bg)
    curses.init_pair(2, curses.COLOR_GREEN, bg)
    curses.init_pair(3, curses.COLOR_YELLOW, bg)
    curses.init_pair(4, curses.COLOR_RED, bg)
    curses.init_pair(5, curses.COLOR_WHITE, bg)
    curses.init_pair(6, curses.COLOR_MAGENTA, bg)
    curses.init_pair(7, curses.COLOR_BLUE, bg)
    ATTR = {
        "title": curses.color_pair(1) | curses.A_BOLD,
        "ok": curses.color_pair(2),
        "warn": curses.color_pair(3),
        "crit": curses.color_pair(4) | curses.A_BOLD,
        "dim": curses.color_pair(5) | curses.A_DIM,
        "accent": curses.color_pair(6),
        "border": curses.color_pair(7),
    }


def render_card(win, top, left, width, height, state, focused):
    attr_border = ATTR.get("border", 0)
    attr_title = ATTR.get("title", 0)
    attr_dim = ATTR.get("dim", 0)
    inner = width - 2
    x = left + 1
    y = top
    end = top + height

    border = "=" if USE_ASCII else "\u2500"
    corner = "+" if USE_ASCII else "\u2500"
    put(win, top, left, "+" + border * inner + "+", attr_border)
    put(win, end - 1, left, "+" + border * inner + "+", attr_border)

    with state.lock:
        data = state.data
        error = state.error
        latency = state.latency
        cpu_hist = list(state.cpu_hist)
        mem_hist = list(state.mem_hist)

    ident = (data or {}).get("identity", {})
    hostname = ident.get("hostname") or state.host.get("host", "local")
    title = " {} ".format(state.name)
    put(win, top, left, title, attr_title | (curses.A_REVERSE if focused else 0), inner)
    y += 1
    status = "{} {}".format(dot(data is not None), hostname)
    lat = " {:>4.0f}ms".format((latency or 0) * 1000) if latency is not None else ""
    osname = ident.get("os", "")
    sub = "{}  {}  {}".format(status, osname, lat).rstrip()
    put(win, y, x, sub, attr_dim, inner)
    y += 1

    if error and data is None:
        put(win, y, x, "!! " + error, ATTR.get("crit", 0), inner)
        return
    if error:
        put(win, y, x, "!! " + error, ATTR.get("crit", 0), inner)
        y += 1
    if not data:
        put(win, y, x, "waiting...", attr_dim, inner)
        return

    cpu = data.get("cpu", {})
    mem = data.get("memory", {})
    lab = data.get("load", {})
    dsk = data.get("disks", [])
    net = data.get("net", {})

    def row(yy, label, pct, rtext, hist=None, fixed=100):
        if yy >= end - 1:
            return
        put(win, yy, x, "{:<4}".format(label), attr_dim)
        bar_x = x + 4
        bar_w = max(4, min(14, inner - 26))
        attr = pct_attr(pct)
        draw_bar(win, yy, bar_x, bar_w, pct, attr)
        tx = bar_x + bar_w + 1
        put(win, yy, tx, "{:>5.1f}%".format(pct), attr)
        rx = tx + 6
        if rtext:
            put(win, yy, rx, rtext, attr_dim)
        if hist and inner > 40:
            hunits = max(0, inner - (rx - x) - 13)
            if hunits >= 5:
                put(win, yy, x + inner - hunits, sparkline(hist, hunits, fixed), attr)

    row(y, "CPU", cpu.get("percent", 0.0),
        "{}MHz".format(int(cpu["mhz"])) if cpu.get("mhz") else "",
        cpu_hist)
    y += 1
    mem_pct = (mem.get("used", 0) / mem["total"] * 100.0) if mem.get("total") else 0.0
    row(y, "RAM", mem_pct,
        "{}/{}".format(human_bytes(mem.get("used")), human_bytes(mem.get("total"))),
        mem_hist)
    y += 1
    for d in dsk[: max(0, height - 9)]:
        row(y, "DSK", d.get("percent", 0.0),
            "{} {}".format(human_bytes(d.get("used")), d.get("path", "")))
        y += 1

    def info(yy, text, attr=0):
        if yy < end - 1:
            put(win, yy, x, text, attr, inner)

    gpu = data.get("gpu") or {}
    info(y, "LOAD {} {} {}   up {}   {}p".format(
        lab.get("1", 0), lab.get("5", 0), lab.get("15", 0),
        human_uptime(data.get("uptime")), data.get("proc_count", 0)), attr_dim)
    y += 1
    netline = "NET  down {}  up {}".format(
        human_bytes(net.get("rx_rate", 0)) + "/s", human_bytes(net.get("tx_rate", 0)) + "/s")
    if gpu:
        netline += "   GPU {:>3.0f}%".format(gpu.get("util", 0))
    info(y, netline, attr_dim)
    y += 1
    temps = data.get("temps", [])
    if temps:
        tstr = "  ".join("{} {:.0f}\u00b0C".format(t["label"][:10], t["celsius"]) for t in temps[:3])
        info(y, "TMP " + tstr, temp_attr(temps[0]["celsius"]))
        y += 1
    procs = data.get("procs", [])[:3]
    if procs and y < end - 2:
        pstr = "  ".join("{} {}".format(p["name"][:12], human_bytes(p["rss"])) for p in procs)
        info(y, "PROC " + pstr, attr_dim)
        y += 1
    if cpu.get("per_core") and y < end - 2 and height >= 16:
        corebar = "".join("{}".format(int(min(9, c / 11.2))) if USE_ASCII else SPARK[min(7, int(c / 12.5))]
                          for c in cpu["per_core"])
        info(y, "CORE " + corebar, attr_dim)
        y += 1

    services = data.get("services", [])
    if services and y < end - 1:
        y += 1

        def srow(yy, svc):
            if yy >= end - 1:
                return
            online = svc.get("online")
            sattr = ATTR.get("ok" if online else "crit")
            put(win, yy, x, "{} ".format(dot(online)), sattr)
            nm = svc.get("name", "?")[:16]
            put(win, yy, x + 2, "{:<17}".format(nm), 0 if online else attr_dim)
            put(win, yy, x + 20, str(svc.get("detail", ""))[: max(0, inner - 20)], attr_dim)

        for svc in services:
            srow(y, svc)
            y += 1


def render_external(win, top, left, width, height, state):
    attr_dim = ATTR.get("dim", 0)
    border = "=" if USE_ASCII else "\u2500"
    put(win, top, left, "+" + border * (width - 2) + "+", ATTR.get("border", 0))
    put(win, top, left, " External checks ", ATTR.get("title", 0), width - 2)
    services = (state.data or {}).get("services", [])
    if not services:
        put(win, top + 1, left + 2, state.error or "waiting...", attr_dim, width - 4)
        return
    col_w = max(24, width // 2)
    for i, svc in enumerate(services):
        row = top + 1 + i // max(1, (width - 2) // col_w)
        col = (i % max(1, (width - 2) // col_w))
        if row >= top + height - 1:
            break
        online = svc.get("online")
        sattr = ATTR.get("ok" if online else "crit")
        x = left + 1 + col * col_w
        put(win, row, x, "{} ".format(dot(online)), sattr)
        put(win, row, x + 2, "{:<18}".format(svc.get("name", "?")[:18]), 0 if online else attr_dim)
        put(win, row, x + 21, str(svc.get("detail", ""))[: max(0, col_w - 22)], attr_dim)


def layout(hosts, ext, width, height):
    n = len(hosts)
    min_card_w = 50
    cols = max(1, min(n, width // min_card_w)) if n else 1
    rows = (n + cols - 1) // cols
    ext_h = 0
    if ext and ext.services:
        lines = max(1, (len(ext.services) + 1) // max(1, (width - 2) // max(24, width // 2)))
        ext_h = min(1 + lines, max(3, height // 4))
    grid_h = height - ext_h
    card_w = width // cols if cols else width
    card_h = max(6, grid_h // rows) if rows else grid_h
    return cols, rows, card_w, card_h, ext_h


# --------------------------------------------------------------------------
# snapshots (non-TUI)
# --------------------------------------------------------------------------

def snapshot(cfg, only=None, as_json=False):
    hosts = [h for h in cfg["hosts"] if not only or h["id"] in only]
    states = [HostState(h) for h in hosts]
    ext = ExternalState(cfg.get("services", [])) if cfg.get("services") else None
    stop = threading.Event()
    threads = [threading.Thread(target=worker, args=(s, cfg, stop), daemon=True) for s in states]
    if ext:
        threads.append(threading.Thread(target=worker, args=(ext, cfg, stop), daemon=True))
    for t in threads:
        t.start()
    deadline = time.time() + max(6.0, max((h.get("timeout", 25) for h in hosts), default=6))
    while time.time() < deadline:
        if all(s.data is not None or s.error for s in states):
            break
        time.sleep(0.1)
    stop.set()
    if as_json:
        out = {"hosts": [], "external": None}
        for s in states:
            rec = dict(s.data or {})
            rec["_error"] = s.error
            rec["_latency"] = s.latency
            out["hosts"].append(rec)
        if ext:
            out["external"] = {"services": (ext.data or {}).get("services", []), "_error": ext.error}
        return out
    lines = []
    for s in states:
        d = s.data or {}
        ident = d.get("identity", {})
        lines.append("== {} ({}) ==".format(s.name, ident.get("hostname", s.host.get("host", "local"))))
        if s.error and not d:
            lines.append("  !! " + s.error)
            lines.append("")
            continue
        if s.error:
            lines.append("  !! " + s.error)
        cpu, mem = d.get("cpu", {}), d.get("memory", {})
        lab, dsk, net = d.get("load", {}), d.get("disks", []), d.get("net", {})
        mem_pct = (mem.get("used", 0) / mem["total"] * 100.0) if mem.get("total") else 0
        lines.append("  CPU {:5.1f}%   RAM {:5.1f}% ({}/{})".format(
            cpu.get("percent", 0), mem_pct,
            human_bytes(mem.get("used")), human_bytes(mem.get("total"))))
        for disk in dsk:
            lines.append("  DSK {} {:5.1f}% ({}/{})".format(
                disk["path"], disk["percent"], human_bytes(disk["used"]), human_bytes(disk["total"])))
        lines.append("  LOAD {} {} {}   up {}   net down {}/s up {}/s".format(
            lab.get("1"), lab.get("5"), lab.get("15"), human_uptime(d.get("uptime")),
            human_bytes(net.get("rx_rate", 0)), human_bytes(net.get("tx_rate", 0))))
        temps = d.get("temps", [])
        if temps:
            lines.append("  TMP " + "  ".join("{} {:.0f}C".format(t["label"], t["celsius"]) for t in temps[:3]))
        for svc in d.get("services", []):
            lines.append("    {} {:<20} {}".format(
                dot(svc.get("online")), svc.get("name"), svc.get("detail", "")))
        lines.append("")
    if ext:
        lines.append("== External checks ==")
        for svc in (ext.data or {}).get("services", []):
            lines.append("    {} {:<20} {}".format(dot(svc.get("online")), svc.get("name"), svc.get("detail", "")))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# TUI
# --------------------------------------------------------------------------

def tui(stdscr, cfg):
    global USE_ASCII
    curses.curs_set(0)
    stdscr.nodelay(True)
    init_colors()
    USE_ASCII = USE_ASCII or bool(cfg["settings"].get("ascii", False))

    hosts = cfg["hosts"]
    states = [HostState(h) for h in hosts]
    ext = ExternalState(cfg.get("services", [])) if cfg.get("services") else None
    stop = threading.Event()
    for s in states:
        threading.Thread(target=worker, args=(s, cfg, stop), daemon=True).start()
    if ext:
        threading.Thread(target=worker, args=(ext, cfg, stop), daemon=True).start()

    focus = 0
    offset = 0
    interval = float(cfg["settings"].get("interval", 2.0))
    tick = max(0.1, min(0.25, interval / 4.0))

    try:
        while True:
            try:
                stdscr.erase()
                height, width = stdscr.getmaxyx()
                now = time.strftime("%H:%M:%S")
                header = " SysWatch  {}  {} host(s)  refresh {:.1f}s ".format(
                    now, len(states), interval)
                put(stdscr, 0, 0, header, ATTR.get("title", 0) | curses.A_BOLD, width)

                cols, rows, card_w, card_h, ext_h = layout(states, ext, width, height - 2)
                grid_top = 1
                for idx, state in enumerate(states):
                    my_row = idx // cols
                    my_col = idx % cols
                    top = grid_top + my_row * card_h - offset
                    left = my_col * card_w
                    this_w = width - left if my_col == cols - 1 else card_w
                    if top + card_h <= grid_top or top >= height - 1:
                        continue
                    render_card(stdscr, top, left, this_w, card_h, state, idx == focus)

                if ext:
                    top = height - 1 - ext_h
                    render_external(stdscr, top, 0, width, ext_h, ext)

                footer = " q quit   r refresh   a ascii   \u2191\u2193 scroll   \u2190\u2192 focus   ? help "
                if USE_ASCII:
                    footer = " q quit   r refresh   a ascii   up/down scroll   left/right focus   ? help "
                put(stdscr, height - 1, 0, footer, ATTR.get("dim", 0) | curses.A_REVERSE, width)
                stdscr.refresh()
            except curses.error:
                pass

            ch = stdscr.getch()
            if ch == -1:
                time.sleep(tick)
                continue
            if ch in (ord("q"), 27):
                break
            elif ch == ord("r"):
                for s in states:
                    s.wake.set()
                if ext:
                    ext.wake.set()
            elif ch == ord("a"):
                USE_ASCII = not USE_ASCII
            elif ch in (curses.KEY_DOWN, ord("j")):
                if focus + cols < len(states):
                    focus += cols
            elif ch in (curses.KEY_UP, ord("k")):
                if focus - cols >= 0:
                    focus -= cols
            elif ch in (curses.KEY_RIGHT, ord("l")):
                if focus + 1 < len(states):
                    focus += 1
            elif ch in (curses.KEY_LEFT, ord("h")):
                if focus > 0:
                    focus -= 1
            elif ch in (ord("?"), ord("H")):
                help_overlay(stdscr)
    finally:
        stop.set()


def help_overlay(stdscr):
    lines = [
        "SysWatch help",
        "",
        "q / Esc     quit",
        "r           force refresh",
        "a           toggle ascii / unicode",
        "arrows/hjkl move focus between hosts",
        "?           this help",
        "",
        "Hosts and services come from the TOML config:",
        "  ~/.config/syswatch/config.toml",
        "",
        "Add another PC by appending a [[hosts]] block with",
        'kind = "ssh" and its host/user. Add service checks',
        "with [[hosts.services]] (port, http, systemd,",
        "docker, process, command).",
        "",
        "Press any key to close.",
    ]
    height, width = stdscr.getmaxyx()
    w = min(62, width - 2)
    h = len(lines) + 2
    top = max(0, (height - h) // 2)
    left = max(0, (width - w) // 2)
    stdscr.nodelay(False)
    for i in range(h):
        put(stdscr, top + i, left, " " * w, ATTR.get("dim", 0))
    attr = ATTR.get("title", 0) | curses.A_BOLD
    put(stdscr, top, left, lines[0][:w], attr)
    for i, line in enumerate(lines[1:], start=1):
        put(stdscr, top + i, left, line[:w], 0)
    stdscr.refresh()
    stdscr.getch()
    stdscr.nodelay(True)


def main():
    global USE_ASCII
    parser = argparse.ArgumentParser(description="Multi-host stats TUI")
    parser.add_argument("--config", "-c", help="path to config.toml")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    parser.add_argument("--json", action="store_true", help="print one JSON snapshot and exit")
    parser.add_argument("--hosts", help="comma-separated host ids to include")
    parser.add_argument("--interval", type=float, help="override refresh interval (seconds)")
    parser.add_argument("--ascii", action="store_true", help="force ascii glyphs")
    parser.add_argument("--no-color", action="store_true", help="disable colors")
    parser.add_argument("--init", action="store_true", help="write the default config and exit")
    args = parser.parse_args()

    if args.init:
        load_config(None)
        print("wrote {}".format(CONFIG_CANDIDATES[0]))
        return

    path = find_config(args.config)
    cfg = load_config(path)
    if args.interval:
        cfg["settings"]["interval"] = args.interval
    if args.ascii:
        cfg["settings"]["ascii"] = True
    USE_ASCII = bool(cfg["settings"].get("ascii", False))
    only = set(h.strip() for h in args.hosts.split(",")) if args.hosts else None

    if args.json:
        print(json.dumps(snapshot(cfg, only, as_json=True), indent=2, default=str))
        return
    if args.once or not sys.stdout.isatty():
        print(snapshot(cfg, only, as_json=False))
        return

    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(tui, cfg)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
