# dsup

[![CI](https://github.com/ainayves/docker-tui/actions/workflows/ci.yml/badge.svg)](https://github.com/ainayves/docker-tui/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dsup)](https://pypi.org/project/dsup/)
[![Python](https://img.shields.io/pypi/pyversions/dsup)](https://pypi.org/project/dsup/)
[![License](https://img.shields.io/pypi/l/dsup)](LICENSE)

Watching a Docker host scatters the essentials across three commands: `docker
ps` in a loop, `docker stats` that takes over your terminal, `docker logs -f`
in yet another tab. `dsup` brings all of it into a single terminal screen —
states, CPU, memory, ports, uptime, live logs, actions — with one command, no
server, no login, no configuration.

**And it reaches remote hosts over plain SSH, installing nothing on them.**

```
dsup · local                                            maj. 2 s · 7 conteneurs
  ÉTAT        NOM            IMAGE                      CPU   RAM PORTS      UP
▸ ● up        api            ghcr.io/acme/api:1.4      2.1%  184M 8080     3d2h
  ● up        worker         ghcr.io/acme/worker:1.4  11.4%  312M —        3d2h
  ● up        postgres       postgres:16.3             0.8%   96M 5432      12d
  ● up        redis          redis:7.2-alpine          0.3%   18M 6379      12d
  ● up        caddy          caddy:2.8                 0.1%   24M 80,443    12d
  ◌ exited    cron           acme/cron:1.2                —     — —           —
  ✖ err 137   batch          acme/batch:0.9               —     — —           —
~
~
j/k nav  ↵ logs  / filtre  s stop  r rest.  x kill  d rm  ? aide     ● 5 ◌ 1 ✖ 1
```

> The interface itself is currently in French, as shown above. Only the
> documentation is in English. Everything is single-key driven, so the labels
> rarely get in the way — but you should know before installing.

## Key features

- **Remote hosts over SSH, with nothing installed on them.** Not an agent, not
  a daemon, not even the `docker` CLI. An SSH account is the only requirement.
- **One authentication, then silence.** The tunnel opens before the interface
  takes over the terminal, so SSH can ask for a password — and it survives
  `dsup`, so the next launches start instantly.
- **One screen for everything** — state, CPU, memory, ports, uptime, health,
  restart policy, live logs.
- **Live logs** with follow mode, in-buffer search, and wrapping, on the
  selected container.
- **Actions where you already are** — start, restart, stop, kill, remove. The
  destructive ones ask for confirmation; the safe ones do not.
- **A detail panel** opens on the right on terminals of 110 columns or more,
  with nothing new to learn.
- **Survives a broken link.** An unreachable daemon degrades the screen and
  retries on its own instead of crashing.
- **No configuration.** No server, no login, no config file. Colors are mid
  ANSI tones and no background is painted, so `dsup` inherits your terminal
  theme, light or dark.

## Use cases

### Supervise a VPS without installing anything on it

You have SSH access to a server running Docker, and you would rather not
install an agent, expose port 2375, or add yet another daemon to maintain.

```sh
dsup root@vps
```

That is the whole setup. `dsup` forwards the remote Docker socket over SSH and
talks to it through a local one. **No command runs on the server** — not even
the `docker` CLI, which the usual approach (`DOCKER_HOST=ssh://…`, built on
`docker system dial-stdio`) does require there.

Your `~/.ssh/config` applies as-is, so aliases, keys and jump hosts keep
working:

```
Host prod
    HostName 10.0.0.4
    User admin
    ProxyJump bastion
```

```sh
dsup prod
```

### Debug a production incident

A container is gone and you do not yet know why. The list shows it at a
glance — `✖ err 137` is an out-of-memory kill, not a crash:

```
  ✖ err 137   batch   acme/batch:0.9   —   —
```

Press `↵` to read its logs, `/` to search the buffer, `r` to restart it. Three
keystrokes, one screen, no switching between `docker ps`, `docker logs` and a
second terminal — and the same keys whether the host is local or a VPS on the
other side of the planet.

## Installation

```sh
pipx install dsup
# or
uv tool install dsup
```

Python 3.10+. Linux and macOS. Windows is not supported: the SSH tunnel relies
on a local Unix socket, which does not exist there.

## Usage

### Local

`dsup` talks to the machine's Docker socket:

```sh
dsup                     # running containers, refreshed every 2s
dsup --all               # include stopped containers
dsup --refresh 5         # refresh interval
dsup --filter api        # initial filter on name or image
```

### Remote, over SSH

Pass the host as an argument:

```sh
dsup admin@prod-eu-1     # user@host
dsup prod-eu-1           # or a ~/.ssh/config alias
dsup --disconnect prod-eu-1
```

#### Nothing runs on the server

`dsup` opens an SSH tunnel to the remote Docker socket and talks to it through
a local one:

```
ssh -fN -L ~/.dsup/<id>.sock:/var/run/docker.sock prod-eu-1
```

Note the `-N`: no remote command. This is what sets it apart from
`DOCKER_HOST=ssh://…`, which executes `docker system dial-stdio` on the far
end and therefore needs the Docker CLI installed there. Here, the socket is
all that is touched.

It also removes a limit you would hit later: docker-py caps its SSH connection
pool at 9, against 25 over a Unix socket. Log and stats streams eat through
nine quickly on a busy host.

#### You type your password once

The tunnel opens *before* the interface takes over the terminal. SSH can
therefore ask for a password, a key passphrase, or confirmation of an unknown
host — and you can answer, with no time limit.

The tunnel then outlives `dsup`. Quit, relaunch, and it starts immediately
with nothing to type, until you run `--disconnect`. A key removes the question
for good:

```sh
ssh-copy-id admin@prod-eu-1
```

Once the interface is running, no connection can ask anything: automatic
reconnections use `BatchMode` and fail cleanly rather than demanding a
password over your display.

#### Rootless Docker

The socket lives elsewhere:

```sh
dsup prod-eu-1 --remote-socket /run/user/1000/docker.sock
```

> **Security** — the local socket grants full control of the remote daemon,
> which is equivalent to root on that server. `~/.dsup/` is created with `0700`
> permissions for that reason. This is also why an SSH tunnel is the minimum
> acceptable exposure, and why port 2375 should never be open.

## Keyboard shortcuts

| Key | Main screen | | Key | Logs screen |
|---|---|---|---|---|
| `j` / `k` | next / previous row (or `↓`/`↑`) | | `j` / `k` | scroll |
| `g` / `G` | top / bottom of list | | `g` / `G` | top / bottom (G resumes follow) |
| `/` | filter by name or image · `Esc` clears | | `/` | search the buffer |
| `↵` | logs for the selected container | | `n` / `N` | next / previous match |
| `S` | start *(no confirmation)* | | `f` | toggle live follow |
| `r` | restart *(no confirmation)* | | `w` | toggle line wrapping |
| `s` | stop — `y` confirms | | `v` | select lines to copy |
| `x` | kill — `y` confirms | | `↵` / `q` | back |
| `d` | rm — `y` confirms | | | |
| `R` | refresh / retry now | | | |
| `?` | show secondary keys | | | |
| `q` | quit | | | |

Destructive actions (`s`, `x`, `d`) show an inverted red banner: `y` confirms,
any other key cancels. Scrolling up in the logs stops the follow; `G` or `f`
resume it.

### Copying logs

`v` in the logs screen starts a line selection on the last visible line — or on
the current match after a search. `j`/`k` extend it, `g`/`G` reach the start
or end of the buffer, `a` takes the whole buffer, `y` or `↵` copies, `Esc`
cancels. Lines are copied whole: not cut at the screen width, not split by
wrapping, and beyond what fits on screen. Follow mode resumes afterwards if it
was on.

With the mouse, drag across the logs to select text, down to the character; it
is copied as soon as you release the button. Dragging past the top or bottom
edge keeps scrolling, and the wheel scrolls the logs.

`dsup` handles the mouse itself rather than leaving it to the terminal: on the
alternate screen, many terminals — VS Code's among them — turn the wheel and
trackpad gestures into arrow keys, which would erase the selection and move the
cursor in the container list. To use your terminal's own selection anyway, hold
the modifier it provides: Option in iTerm2, Shift in most Linux terminals; in
VS Code on macOS, enable `terminal.integrated.macOptionClickForcesSelection`
and hold Option.

## Behavior worth knowing

- The list appears immediately; CPU and memory fill in as readings arrive (the
  first CPU sample shows `—`, since the value is a delta between two points).
- Unreachable daemon or lost connection: the screen degrades and retries every
  5s on its own (`R` forces an attempt).
- The log buffer is capped at 2,000 lines, so a very chatty container cannot
  exhaust memory.
- Copies go to the system clipboard through `pbcopy`, `wl-copy`, `xclip` or
  `xsel`. Without one of them — typically when `dsup` itself runs inside an SSH
  session — they fall back to OSC 52, which depends on the terminal: iTerm2
  requires *Applications in terminal may access clipboard*, Terminal.app does
  not support it.
- `Ctrl-C` and `SIGTERM` restore the terminal cleanly.

## Development

```sh
git clone https://github.com/ainayves/docker-tui && cd docker-tui
python3 -m venv venv
source venv/bin/activate          # fish: source venv/bin/activate.fish
pip install -e ".[dev]"
```

Two traps hide in those four lines, and neither has anything to do with `dsup`
itself.

**Activation depends on your shell.** `venv/bin/activate` is a POSIX shell
script: fish does not share that syntax and fails on `case "$(uname)" in`. The
venv ships one script per shell — `activate.fish` for fish, `activate.csh` for
csh/tcsh, `Activate.ps1` for PowerShell. Source the one for your shell.

**`pip install -e ".[dev]"` is not optional.** Installing the dependencies
alone is not enough: `dsup` is an entry point declared in `[project.scripts]`,
and the `venv/bin/dsup` executable only exists once the package itself is
installed. Skip that step and the venv is active, `textual` and `docker` are
present, yet the command is nowhere to be found:

```
(venv) $ dsup
fish: Unknown command: dsup
```

The `-e` installs in editable mode — a link to your sources, so changes under
`dsup/` take effect without reinstalling. The `[dev]` suffix adds pytest and
mypy; without it, `python -m pytest` answers `No module named pytest`.

`requirements.txt` and `requirements-dev.txt` restate the same dependencies for
tools that expect them, but `pyproject.toml` remains the source of truth: all
three are kept in sync by hand.

### Verifying

```sh
pytest                   # no Docker daemon, no network access
mypy                     # strict, over dsup/ and tests/
dsup --version
```

The test suite needs no containers: the Docker layer sits behind a protocol and
the tests inject a fake gateway (`FakeGateway` in `tests/conftest.py`).
`subprocess.run` is replaced everywhere `dsup/tunnel.py` shells out to `ssh`.
A suite that takes more than a few seconds is therefore a signal that some test
is attempting a real connection — watch for it when adding tests here.

CI replays all of it on Python 3.10 through 3.13 plus macOS, and additionally
checks the entry point (`dsup --version`), which the suite never exercises
since it imports `main()` directly.

### Releasing

The version lives in **exactly one place**, `dsup/__init__.py`: hatch reads it
from there, and CI refuses to publish when the tag does not match. That is the
guard that matters, because a version number consumed on PyPI can never be
reused.

```sh
# 1. bump __version__ in dsup/__init__.py
# 2. add the matching entry to CHANGELOG.md
git commit -am "version 0.2.0"
git push origin master        # code only, CI only
git tag v0.2.0
git push origin v0.2.0        # this is what publishes
```

The tag triggers `release.yml`, which replays mypy and the tests, builds,
publishes to PyPI, then creates a GitHub Release with the notes taken from the
changelog. For a consequence-free rehearsal, run the same workflow manually and
pick `testpypi` — the only dry run available before the first real upload.

## License

MIT — see [LICENSE](LICENSE).
