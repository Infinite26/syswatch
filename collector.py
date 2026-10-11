#!/usr/bin/env python3
"""SysWatch collector.

Gathers host stats and probes services. Standard library only so it can be
piped to a remote host over ssh:

    ssh host python3 - <services-b64>

When run as a program it reads a base64 JSON payload ({"services": [...]})
from sys.argv[1] or $SYSWATCH_PAYLOAD and writes a JSON result to stdout.
It is also importable: collect(services, disks=None, sample=0.15).
"""

import base64
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

SAMPLE = 0.15


def _read(path, default=None):
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def _first_line(path, default=""):
    data = _read(path)
    return data.splitlines()[0].strip() if data else default


def _cpu_times():
    line = _first_line("/proc/stat")
    if not line or not line.startswith("cpu"):
        return 0, 1
    parts = line.split()[1:]
    nums = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 8:
        nums.append(0)
    idle = nums[3] + nums[4]
    total = sum(nums[:8])
    return idle, total


def _per_core_times():
    out = {}
    data = _read("/proc/stat", "")
    for line in data.splitlines():
        if not line.startswith("cpu") or line.startswith("cpu "):
            continue
        name = line.split()[0]
        nums = []
        for p in line.split()[1:]:
            try:
                nums.append(int(p))
            except ValueError:
                nums.append(0)
        while len(nums) < 8:
            nums.append(0)
        out[name] = (nums[3] + nums[4], sum(nums[:8]))
    return out


def _net_totals():
    rx = tx = 0
    data = _read("/proc/net/dev", "")
    for line in data.splitlines():
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        iface = iface.strip()
        if iface == "lo":
            continue
        cols = rest.split()
        if len(cols) >= 9:
            try:
                rx += int(cols[0])
                tx += int(cols[8])
            except ValueError:
                pass
    return rx, tx


def cpu_model():
    data = _read("/proc/cpuinfo", "")
    for line in data.splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    for line in data.splitlines():
        if line.lower().startswith("hardware") or line.lower().startswith("model"):
            if ":" in line:
                return line.split(":", 1)[1].strip()
    return ""


def cpu_mhz():
    for pat in ("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
                "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq"):
        val = _read(pat)
        if val:
            try:
                return int(val) / 1000.0
            except ValueError:
                pass
    data = _read("/proc/cpuinfo", "")
    for line in data.splitlines():
        if line.lower().startswith("cpu mhz"):
            try:
                return float(line.split(":", 1)[1].strip())
            except ValueError:
                break
    return None


def memory():
    info = {}
    data = _read("/proc/meminfo", "")
    for line in data.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    def kb(key):
        val = info.get(key, "0 kB").split()[0]
        try:
            return int(val) * 1024
        except ValueError:
            return 0
    total = kb("MemTotal")
    avail = kb("MemAvailable") or (kb("MemFree") + kb("Buffers") + kb("Cached"))
    swap_total = kb("SwapTotal")
    swap_free = kb("SwapFree")
    return {
        "total": total,
        "used": max(0, total - avail),
        "available": avail,
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - swap_free),
    }


def load():
    parts = _first_line("/proc/loadavg").split()
    out = {"1": 0.0, "5": 0.0, "15": 0.0, "running": 0, "total": 0}
    try:
        out["1"], out["5"], out["15"] = float(parts[0]), float(parts[1]), float(parts[2])
        run = parts[3].split("/")
        out["running"], out["total"] = int(run[0]), int(run[1])
    except (ValueError, IndexError):
        pass
    return out


def uptime_seconds():
    try:
        return float(_first_line("/proc/uptime").split()[0])
    except (ValueError, IndexError):
        return 0.0


def disks(paths):
    out = []
    for path in paths:
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        pct = (used / total * 100.0) if total else 0.0
        out.append({"path": path, "total": total, "used": used, "free": free, "percent": pct})
    return out


def temperatures():
    found = []
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        name = _first_line(os.path.join(hw, "name"), "")
        for inp in glob.glob(os.path.join(hw, "temp*_input")):
            raw = _read(inp)
            if not raw:
                continue
            try:
                celsius = int(raw.strip()) / 1000.0
            except ValueError:
                continue
            if celsius < -50 or celsius > 200:
                continue
            label = _read(inp.replace("_input", "_label"))
            label = label.strip() if label else f"{name} {os.path.basename(inp)}"
            found.append({"label": label, "celsius": celsius})
    found.sort(key=lambda t: t["celsius"], reverse=True)
    return found


def gpu():
    has_nv = os.path.exists("/dev/nvidiactl") or os.path.exists("/dev/nvidia0")
    smi = shutil.which("nvidia-smi") if has_nv else None
    if smi:
        try:
            res = subprocess.run(
                [smi, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=1.5)
            if res.returncode == 0 and res.stdout.strip():
                gpus = []
                for line in res.stdout.strip().splitlines():
                    cols = [c.strip() for c in line.split(",")]
                    if len(cols) >= 5:
                        gpus.append({"name": cols[0], "util": float(cols[1]),
                                     "mem_used": float(cols[2]) * 1048576,
                                     "mem_total": float(cols[3]) * 1048576,
                                     "temp": float(cols[4])})
                if gpus:
                    return gpus[0]
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    for path in sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")):
        raw = _read(path)
        if raw:
            try:
                return {"name": "AMD GPU", "util": float(raw.strip()), "mem_used": None,
                        "mem_total": None, "temp": None}
            except ValueError:
                pass
    return None


def processes(limit=4):
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    procs = []
    count = 0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 0, []
    for pid in entries:
        if not pid.isdigit():
            continue
        count += 1
        statm = _read("/proc/{}/statm".format(pid))
        if not statm:
            continue
        fields = statm.split()
        if len(fields) < 2:
            continue
        try:
            rss = int(fields[1]) * page
        except ValueError:
            continue
        if rss <= 0:
            continue
        name = _read("/proc/{}/comm".format(pid), "").strip() or "?"
        procs.append((rss, name))
    procs.sort(reverse=True)
    return count, [{"name": n, "rss": m} for m, n in procs[:limit]]


def identity():
    os_name = ""
    data = _read("/etc/os-release", "")
    for line in data.splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip().strip('"')
            break
    return {
        "hostname": socket.gethostname(),
        "os": os_name,
        "cpu_model": cpu_model(),
        "cpu_count": os.cpu_count() or 0,
    }


def probe_service(svc):
    kind = svc.get("type", "port")
    name = svc.get("name", kind)
    result = {"name": name, "type": kind, "online": False, "detail": ""}
    try:
        if kind == "port":
            host = svc.get("host", "127.0.0.1")
            port = int(svc.get("port", 0))
            with socket.create_connection((host, port), timeout=svc.get("timeout", 2.0)):
                result["online"] = True
            result["detail"] = "{}:{}".format(host, port)
        elif kind == "http":
            url = svc["url"]
            req = urllib.request.Request(url, headers={"User-Agent": "syswatch"})
            expect = svc.get("expect")
            try:
                with urllib.request.urlopen(req, timeout=svc.get("timeout", 3.0)) as resp:
                    code = resp.status
            except urllib.error.HTTPError as exc:
                code = exc.code
            expected = expect if isinstance(expect, list) else ([expect] if expect else None)
            if expected:
                result["online"] = code in expected
            else:
                result["online"] = code < 400
            result["detail"] = "HTTP {}".format(code)
        elif kind == "process":
            pattern = svc["pattern"]
            n = 0
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                cmd = _read(os.path.join("/proc", pid, "cmdline"), "")
                if pattern in cmd.replace("\x00", " "):
                    n += 1
            result["online"] = n > 0
            result["detail"] = "{} proc".format(n)
        elif kind == "systemd":
            unit = svc["unit"]
            res = subprocess.run(["systemctl", "is-active", unit],
                                 capture_output=True, text=True, timeout=3)
            state = res.stdout.strip()
            result["online"] = state == "active"
            result["detail"] = state or "unknown"
        elif kind == "docker":
            container = svc["container"]
            res = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container],
                                 capture_output=True, text=True, timeout=4)
            running = res.stdout.strip() == "true"
            result["online"] = running
            result["detail"] = "running" if running else "stopped"
        elif kind == "command":
            res = subprocess.run(svc["command"], shell=True, capture_output=True,
                                 text=True, timeout=svc.get("timeout", 4))
            result["online"] = res.returncode == svc.get("code", 0)
            out = (res.stdout or res.stderr).strip()
            first = out.splitlines()[0] if out else ""
            result["detail"] = first[:80]
        else:
            result["detail"] = "unknown type"
    except Exception as exc:  # noqa: BLE001 - surface any probe error as detail
        result["online"] = False
        result["detail"] = "{}".format(exc)[:80]
    return result


def collect(services=None, disk_paths=None, sample=SAMPLE, want_gpu=True, want_procs=True):
    services = services or []
    disk_paths = disk_paths or ["/"]
    errors = []

    i0, t0 = _cpu_times()
    cores0 = _per_core_times()
    rx0, tx0 = _net_totals()
    time.sleep(sample)
    i1, t1 = _cpu_times()
    cores1 = _per_core_times()
    rx1, tx1 = _net_totals()

    dt = t1 - t0
    cpu_pct = (1.0 - (i1 - i0) / dt) * 100.0 if dt > 0 else 0.0
    cores = []
    for name, (idle1, tot1) in sorted(cores1.items()):
        idle0, tot0 = cores0.get(name, (idle1, tot1))
        d = tot1 - tot0
        cores.append(max(0.0, min(100.0, (1.0 - (idle1 - idle0) / d) * 100.0)) if d > 0 else 0.0)

    net = {
        "rx_rate": max(0, rx1 - rx0) / sample if sample else 0,
        "tx_rate": max(0, tx1 - tx0) / sample if sample else 0,
        "rx_total": rx1,
        "tx_total": tx1,
    }

    proc_count = 0
    procs = []
    if want_procs:
        proc_count, procs = processes()

    data = {
        "ok": True,
        "identity": identity(),
        "cpu": {
            "percent": max(0.0, min(100.0, cpu_pct)),
            "per_core": cores,
            "mhz": cpu_mhz(),
        },
        "memory": memory(),
        "load": load(),
        "uptime": uptime_seconds(),
        "disks": disks(disk_paths),
        "net": net,
        "temps": temperatures()[:3],
        "procs": procs,
        "proc_count": proc_count,
        "gpu": (gpu() if want_gpu else None),
        "services": [probe_service(s) for s in services],
        "errors": errors,
        "timestamp": time.time(),
    }
    return data


def main():
    payload_b64 = None
    if len(sys.argv) > 1:
        payload_b64 = sys.argv[1]
    elif os.environ.get("SYSWATCH_PAYLOAD"):
        payload_b64 = os.environ["SYSWATCH_PAYLOAD"]
    payload = {}
    if payload_b64:
        try:
            payload = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
        except Exception:  # noqa: BLE001
            payload = {}
    result = collect(payload.get("services"), payload.get("disks"), payload.get("sample", SAMPLE),
                     payload.get("gpu", True), payload.get("procs", True))
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
