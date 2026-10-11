# SysWatch

A tiny, **zero-dependency** multi-host stats TUI. Pure Python standard library —
no pip install, no virtualenv. Runs on any Linux box with Python 3.11+.

```
┌ Grug ─────────────────────────────┐  ┌ Server ─────────────────────────────┐
│ ● Grug  Omarchy          12ms     │  │ ● server  CachyOS        280ms      │
│ CPU ███░░░░░░░░░░░  20.2% 2796MHz │  │ CPU █░░░░░░░░░░░░░   8.5% 3081MHz   │
│ RAM ██████████░░░░  70.9%  6.7/9G │  │ RAM █████████░░░░░  67.8%  6.5/9G   │
│ DSK █████████████░  91.2% 433/475 │  │ DSK ███████░░░░░░░  46.6% 106/229   │
│ LOAD 3.41 2.86 2.39  up 11h12m    │  │ LOAD 0.46 0.49 0.39  up 8d6h        │
│ NET  down 16.6K/s  up 106.5K/s    │  │ NET  down 50.4K/s  up 1.7M/s        │
│ TMP Tctl 71°C  edge 60°C          │  │ TMP Tctl 41°C  edge 40°C            │
│ PROC opencode 918M ...            │  │ PROC java 2.8G ...                  │
│                                   │  │                                     │
│ ● SSH              127.0.0.1:22   │  │ ● Minecraft        127.0.0.1:25565  │
│ ● SysWatch API     127.0.0.1:8899 │  │ ● PostgreSQL       192.168.110.40:5433│
└───────────────────────────────────┘  └─────────────────────────────────────┘
```

## Usage

```bash
./syswatch.py              # live TUI (local + server)
./syswatch.py --once       # plain-text snapshot (good over ssh / cron)
./syswatch.py --json       # machine-readable snapshot
./syswatch.py --hosts server
./syswatch.py --interval 5 # seconds
./syswatch.py --ascii      # ascii-only glyphs
./syswatch.py --init       # write the default config
```

Keys: `q` quit · `r` refresh now · `a` ascii toggle · arrows/`hjkl` move focus ·
`?` help.

## Config

`~/.config/syswatch/config.toml` (or `--config FILE`). It is created on first
run. Adding another PC is one block:

```toml
[[hosts]]
id = "laptop"
name = "Laptop"
kind = "ssh"                # "local" for this machine
host = "192.168.1.50"
user = "me"
port = 22
disks = ["/", "/home"]

[[hosts.services]]
name = "PostgreSQL"
type = "port"
host = "127.0.0.1"
port = 5432

[[hosts.services]]
name = "Minecraft"
type = "port"
port = 25565
```

`kind = "local"` hosts are read directly. `kind = "ssh"` hosts run the bundled
`collector.py` over ssh (multiplexed with `ControlMaster`, so polls are fast).
Remote hosts need `python3`; the ssh key must already work non-interactively.

### Service check types

| type      | fields                          | online when            |
|-----------|---------------------------------|------------------------|
| `port`    | `port`, `host` (default 127.0.0.1) | TCP connect succeeds |
| `http`    | `url`, optional `expect` (code or list) | status < 400 / matches |
| `systemd` | `unit`                          | `systemctl is-active` = active |
| `docker`  | `container`                     | container running      |
| `process` | `pattern`                       | matching `/proc` cmdline exists |
| `command` | `command`, optional `code`      | exit code matches (default 0) |

Checks attached to a `[[hosts.services]]` block run **on that host**, so
`host = "127.0.0.1"` means that host's loopback. A top-level `[[services]]`
block runs from the machine showing the TUI.

Switches per host: `gpu = false`, `procs = false`, `python = "..."`.

## Layout

- `syswatch.py` — CLI, config, ssh fan-out, curses TUI.
- `collector.py` — standalone stats + service probes. Importable, or piped to a
  remote host: `ssh host python3 - <payload-b64>`.
- `config.toml` — optional project-local config.
