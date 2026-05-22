# yumweb

> A general-purpose, AI-friendly browser-automation layer for Microsoft Edge on
> Windows. Drive a dedicated, persistent-profile Edge instance from the
> command line — navigate, read, click, type, screenshot, run JS, and
> read/post tweets.

Designed to be dropped into agent frameworks (OpenClaw, Claude Code, GitHub
Copilot, custom RPA scripts, etc.) as a **skill**: every command is one-shot,
prints to stdout, and exits with a sensible code.

Under the hood: [Playwright](https://playwright.dev/python/) attaches over
Chrome DevTools Protocol to an Edge instance launched with a private user-data
directory and `--remote-debugging-port=9333`. No bundled browser is
downloaded — we use the Edge you already have installed.

---

## Requirements

- **Windows** (PowerShell or cmd)
- **Microsoft Edge** installed (any modern Chromium-based Edge)
- **64-bit Python 3.10+**
  - 32-bit Python will **not** work: Playwright depends on `greenlet`, which
    ships no 32-bit Windows wheels. Make sure `python -c "import platform;
    print(platform.architecture())"` reports `('64bit', ...)`.

## Install

```cmd
git clone https://github.com/yumyumtum/yumweb.git
cd yumweb
python -m pip install -r requirements.txt
```

(or `python -m pip install playwright html2text requests`)

You do **not** need `playwright install` — yumweb never launches a bundled
browser. It only attaches to your real Edge.

## First run

```cmd
python scripts\yumweb.py start
```

This launches Edge with a dedicated profile directory (`./profile/`) and
remote-debugging enabled on `127.0.0.1:9333`. The Edge window stays open in
the background; subsequent commands attach to it.

**Log into any sites you want yumweb to access** (e.g. `x.com`). Cookies are
stored in `./profile/` and survive across runs.

To stop the dedicated Edge:

```cmd
python scripts\yumweb.py stop
```

## Quick tour

```cmd
:: Read any page as markdown (great for feeding to an LLM)
python scripts\yumweb.py fetch https://example.com

:: Plain-text body of the currently active tab
python scripts\yumweb.py read --mode text --max 2000

:: Take a screenshot
python scripts\yumweb.py screenshot shot.png

:: Run arbitrary JavaScript and print the JSON result
python scripts\yumweb.py eval "document.title"

:: Click and type
python scripts\yumweb.py click "button.signup"
python scripts\yumweb.py type "input[name=q]" "hello world" --enter

:: List tabs
python scripts\yumweb.py tabs

:: X (Twitter) — read home timeline (requires prior login in the Edge window)
python scripts\yumweb.py x-read --n 10

:: X — read another user's tweets
python scripts\yumweb.py x-read --user satyanadella --n 5

:: X — post (also requires login)
python scripts\yumweb.py x-post "hello from yumweb"
```

Full command reference: [SKILL.md](SKILL.md).

## Configuration

`scripts/config.json` controls the port, profile location, log location, and
where to look for `msedge.exe`. By default the profile and log paths are
**blank**, which means they auto-resolve to `./profile/` and
`./logs/yumweb.log` next to the skill root. Override them with absolute paths
if you want the profile somewhere else.

```json
{
  "remote_debugging_port": 9333,
  "profile_dir": "",
  "log_path": "",
  "edge_exe_candidates": [
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"
  ],
  "default_read_max_chars": 8000
}
```

## Use as an agent skill

The repo's [SKILL.md](SKILL.md) has YAML front-matter
(`name`/`description`/`argument-hint`) compatible with skill-loading agents
such as OpenClaw and GitHub Copilot. Point your agent at this directory and
it can discover the commands automatically.

A typical pattern an agent should follow:

1. Call `python scripts\yumweb.py status` — if not running, call `start`.
2. For reading a URL, call `fetch <url>` and pipe the markdown to the LLM.
3. For X timeline / posting, use `x-read` / `x-post`.
4. For custom DOM extraction, write JS and call `eval` (it must return
   JSON-serializable data).

## Architecture

```
User / Agent
     |
     | (shell)
     v
scripts/yumweb.py  --[Playwright sync API]--> CDP @ 127.0.0.1:9333
                                                    |
                                                    v
                                        msedge.exe (--user-data-dir=./profile)
```

- `yumweb.py start` `Popen`s Edge detached with a unique profile dir.
- Every other command opens a fresh Playwright session, `connect_over_cdp()`s
  to port 9333, performs the action on the first non-`devtools://`/`edge://`
  page, prints the result, and exits.
- `browser.close()` after a CDP attach **only disconnects the client**; it
  does **not** kill the Edge instance.

## Security

- `profile/` contains live cookies and session data. The bundled `.gitignore`
  excludes it. Never commit, share, or upload that directory.
- `eval` executes arbitrary JavaScript in the active tab. Only call it with
  trusted input.
- The CDP port is bound to `127.0.0.1` only. Do not expose `9333` externally.

## License

MIT — see [LICENSE](LICENSE).
