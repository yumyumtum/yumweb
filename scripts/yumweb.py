#!/usr/bin/env python3
"""
yumweb.py — Playwright-based Edge browser driver for AI/automation.

Attaches to a dedicated Microsoft Edge instance over Chrome DevTools Protocol
(CDP). The Edge instance runs with its own profile so it can stay separate
from your main browser session (cookies, logins, etc. are preserved across
runs).

Requires 64-bit Python with the `playwright` package installed:
    python -m pip install playwright html2text requests

No `playwright install` is needed — we attach to your existing Edge launched
by `yumweb.py start`, not a bundled browser.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
CONFIG_PATH = HERE / "config.json"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Auto-resolve profile_dir / log_path: if empty or relative, anchor to SKILL_DIR.
    def _resolve(val, default_rel):
        v = (val or "").strip() if isinstance(val, str) else ""
        if not v:
            return str(SKILL_DIR / default_rel)
        p = Path(v)
        return str(p if p.is_absolute() else (SKILL_DIR / p))
    cfg["profile_dir"] = _resolve(cfg.get("profile_dir"), "profile")
    cfg["log_path"] = _resolve(cfg.get("log_path"), "logs/yumweb.log")
    return cfg


def find_edge_exe(cfg: dict) -> str:
    for cand in cfg["edge_exe_candidates"]:
        if os.path.exists(cand):
            return cand
    raise FileNotFoundError("Microsoft Edge (msedge.exe) not found. Update config.json.")


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


def get_cdp_version(port: int) -> Optional[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def get_cdp_tabs(port: int) -> list:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
            data = json.loads(r.read())
            return [t for t in data if t.get("type") == "page"]
    except Exception:
        return []


def ensure_running(cfg: dict, wait_seconds: float = 15.0) -> None:
    port = cfg["remote_debugging_port"]
    if port_open("127.0.0.1", port) and get_cdp_version(port):
        return
    cmd_start(cfg)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if port_open("127.0.0.1", port) and get_cdp_version(port):
            return
        time.sleep(0.5)
    raise RuntimeError(f"Edge CDP did not come up on port {port} within {wait_seconds}s")


# ---------- playwright attach ----------

@contextmanager
def attached_browser(cfg: dict):
    """Attach Playwright to the running Edge CDP instance.

    Yields (playwright, browser, context, page) where page is the first
    non-devtools page in the first browser context.
    """
    ensure_running(cfg)
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    browser = None
    try:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cfg['remote_debugging_port']}")
        # connect_over_cdp returns the default browser; existing pages live under contexts[0]
        if not browser.contexts:
            context = browser.new_context()
        else:
            context = browser.contexts[0]
        page = _pick_active_page(context)
        yield p, browser, context, page
    finally:
        try:
            if browser is not None:
                # IMPORTANT: do NOT close() the browser — that would kill the Edge instance.
                # Just disconnect.
                browser.close()  # In CDP-attached mode this only detaches the client.
        except Exception:
            pass
        try:
            p.stop()
        except Exception:
            pass


def _pick_active_page(context):
    """Return the first non-devtools/non-edge:// page in the context.

    If none exist, create a new about:blank page.
    """
    for pg in context.pages:
        try:
            u = pg.url or ""
        except Exception:
            continue
        if not u.startswith("devtools://") and not u.startswith("edge://"):
            return pg
    if context.pages:
        return context.pages[0]
    return context.new_page()


# ---------- commands ----------

def cmd_start(cfg: dict) -> None:
    port = cfg["remote_debugging_port"]
    if port_open("127.0.0.1", port) and get_cdp_version(port):
        print(f"[yumweb] already running on port {port}")
        return

    edge = find_edge_exe(cfg)
    profile = cfg["profile_dir"]
    Path(profile).mkdir(parents=True, exist_ok=True)
    log_path = cfg["log_path"]
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)

    args = [
        edge,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=msEdgeFirstRunExperience",
        "--remote-allow-origins=*",
        "about:blank",
    ]

    creationflags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(
            args,
            stdout=logf,
            stderr=logf,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
    print(f"[yumweb] launched Edge pid={proc.pid} port={port} profile={profile}")
    for _ in range(30):
        if port_open("127.0.0.1", port) and get_cdp_version(port):
            print(f"[yumweb] CDP ready on port {port}")
            return
        time.sleep(0.5)
    print(f"[yumweb] WARNING: CDP not responding on port {port} after 15s", file=sys.stderr)


def cmd_stop(cfg: dict) -> None:
    port = cfg["remote_debugging_port"]
    if not get_cdp_version(port):
        print(f"[yumweb] not running on port {port}")
        return
    profile = cfg["profile_dir"]
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{profile.replace(chr(92), chr(92)+chr(92))}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"
        )
        out = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps_cmd], text=True, timeout=15)
        pids = [p.strip() for p in out.splitlines() if p.strip()]
        print(f"[yumweb] stopped pids: {pids}")
    except subprocess.CalledProcessError as e:
        print(f"[yumweb] stop failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_status(cfg: dict) -> None:
    port = cfg["remote_debugging_port"]
    v = get_cdp_version(port)
    if not v:
        print(json.dumps({"running": False, "port": port}, indent=2))
        return
    tabs = get_cdp_tabs(port)
    summary = {
        "running": True,
        "port": port,
        "browser": v.get("Browser"),
        "webSocketDebuggerUrl": v.get("webSocketDebuggerUrl"),
        "num_tabs": len(tabs),
        "tabs": [{"idx": i, "title": t.get("title", "")[:80], "url": t.get("url", "")} for i, t in enumerate(tabs)],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def cmd_goto(cfg: dict, url: str) -> None:
    with attached_browser(cfg) as (_p, _b, _c, page):
        page.goto(url, wait_until="load", timeout=20000)
        print(json.dumps({"ok": True, "url": page.url, "title": page.title()}, ensure_ascii=False))


def _ensure_html2text():
    try:
        import html2text  # noqa
        return html2text
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "html2text"])
        import html2text  # noqa
        return html2text


def cmd_read(cfg: dict, mode: str, selector: Optional[str], max_chars: int) -> None:
    with attached_browser(cfg) as (_p, _b, _c, page):
        if selector:
            html = page.evaluate(
                "(sel) => { const el = document.querySelector(sel); return el ? el.outerHTML : ''; }",
                selector,
            ) or ""
        else:
            html = page.content()

        if mode == "html":
            out = html
        elif mode == "text":
            if selector:
                out = page.evaluate(
                    "(sel) => { const el = document.querySelector(sel); return el ? el.innerText : ''; }",
                    selector,
                ) or ""
            else:
                out = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        elif mode == "md":
            html2text = _ensure_html2text()
            h = html2text.HTML2Text()
            h.body_width = 0
            h.ignore_images = False
            h.ignore_links = False
            out = h.handle(html)
        else:
            print(f"unknown mode: {mode}", file=sys.stderr); sys.exit(2)

        if max_chars and len(out) > max_chars:
            out = out[:max_chars] + f"\n\n[... truncated at {max_chars} chars, total {len(out)} ...]"
        print(f"# URL: {page.url}")
        print(f"# Title: {page.title()}")
        print(f"# Mode: {mode}  Length: {len(out)}")
        print("---")
        print(out)


def cmd_click(cfg: dict, selector: str) -> None:
    with attached_browser(cfg) as (_p, _b, _c, page):
        page.locator(selector).first.click(timeout=10000)
        print(json.dumps({"ok": True, "clicked": selector, "url_after": page.url}))


def cmd_type(cfg: dict, selector: str, text: str, press_enter: bool) -> None:
    with attached_browser(cfg) as (_p, _b, _c, page):
        loc = page.locator(selector).first
        loc.click(timeout=10000)
        loc.type(text)
        if press_enter:
            loc.press("Enter")
        print(json.dumps({"ok": True, "selector": selector, "len": len(text), "enter": press_enter}))


def cmd_screenshot(cfg: dict, path: str) -> None:
    with attached_browser(cfg) as (_p, _b, _c, page):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=path)
        print(json.dumps({"ok": True, "path": str(Path(path).resolve()), "url": page.url}))


def cmd_eval(cfg: dict, js: str) -> None:
    with attached_browser(cfg) as (_p, _b, _c, page):
        # Playwright's evaluate expects an expression or arrow function; strip a leading "return"
        expr = js.strip()
        if expr.startswith("return "):
            expr = expr[len("return "):].rstrip(";")
            result = page.evaluate(f"() => ({expr})")
        else:
            # Try as-is (could be an arrow fn or an expression)
            try:
                result = page.evaluate(expr)
            except Exception:
                result = page.evaluate(f"() => ({expr})")
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str))


def cmd_tabs(cfg: dict) -> None:
    tabs = get_cdp_tabs(cfg["remote_debugging_port"])
    out = [{"idx": i, "title": t.get("title", "")[:80], "url": t.get("url", "")} for i, t in enumerate(tabs)]
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_tab_new(cfg: dict, url: str) -> None:
    with attached_browser(cfg) as (_p, _b, context, _page):
        new_page = context.new_page()
        if url and url != "about:blank":
            new_page.goto(url, wait_until="load", timeout=20000)
        print(json.dumps({"ok": True, "url": new_page.url, "title": new_page.title()}))


def cmd_tab_switch(cfg: dict, idx: int) -> None:
    # CDP-level tabs ordering differs from Playwright's context.pages; use CDP /json/list ordering
    # and activate via Playwright bring_to_front on the matching page.
    cdp_tabs = get_cdp_tabs(cfg["remote_debugging_port"])
    if idx < 0 or idx >= len(cdp_tabs):
        print(f"idx out of range (0..{len(cdp_tabs)-1})", file=sys.stderr); sys.exit(2)
    target_url = cdp_tabs[idx].get("url", "")
    with attached_browser(cfg) as (_p, _b, context, _page):
        match = None
        for pg in context.pages:
            try:
                if pg.url == target_url:
                    match = pg; break
            except Exception:
                continue
        if match is None and context.pages:
            match = context.pages[idx] if idx < len(context.pages) else context.pages[0]
        if match is None:
            print(json.dumps({"ok": False, "msg": "no matching page"})); return
        match.bring_to_front()
        print(json.dumps({"ok": True, "idx": idx, "url": match.url, "title": match.title()}))


def cmd_tab_close(cfg: dict, idx: int) -> None:
    cdp_tabs = get_cdp_tabs(cfg["remote_debugging_port"])
    if idx < 0 or idx >= len(cdp_tabs):
        print(f"idx out of range (0..{len(cdp_tabs)-1})", file=sys.stderr); sys.exit(2)
    target_url = cdp_tabs[idx].get("url", "")
    with attached_browser(cfg) as (_p, _b, context, _page):
        match = None
        for pg in context.pages:
            try:
                if pg.url == target_url:
                    match = pg; break
            except Exception:
                continue
        if match is None and idx < len(context.pages):
            match = context.pages[idx]
        if match is None:
            print(json.dumps({"ok": False, "msg": "no matching page"})); return
        match.close()
        print(json.dumps({"ok": True, "closed_idx": idx}))


def cmd_fetch(cfg: dict, url: str, max_chars: int) -> None:
    """Navigate + read --mode md in one shot. SPA-friendly: wait for body text to stabilize."""
    with attached_browser(cfg) as (_p, _b, _c, page):
        page.goto(url, wait_until="load", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        prev_len = -1
        for _ in range(8):
            time.sleep(0.8)
            cur_len = page.evaluate("() => (document.body && document.body.innerText || '').length") or 0
            if cur_len > 200 and cur_len == prev_len:
                break
            prev_len = cur_len
        html = page.content()
        html2text = _ensure_html2text()
        h = html2text.HTML2Text()
        h.body_width = 0
        h.ignore_images = True
        h.ignore_links = False
        out = h.handle(html)
        if len(out.strip()) < 200:
            inner = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            if len(inner) > len(out):
                out = inner
        if max_chars and len(out) > max_chars:
            out = out[:max_chars] + f"\n\n[... truncated at {max_chars} chars, total {len(out)} ...]"
        print(f"# URL: {page.url}")
        print(f"# Title: {page.title()}")
        print(f"# Length: {len(out)} chars (markdown)")
        print("---")
        print(out)


# ---------- X (Twitter) helpers ----------

X_TWEET_EXTRACT_JS = r"""
(n) => {
  const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  return articles.slice(0, n).map((a, i) => {
    const text = a.querySelector('[data-testid="tweetText"]')?.innerText || '';
    const author = a.querySelector('[data-testid="User-Name"]')?.innerText || '';
    const time = a.querySelector('time')?.getAttribute('datetime') || '';
    const link = a.querySelector('a[href*="/status/"]')?.href || '';
    const stats = a.querySelector('[role="group"]')?.innerText || '';
    return { idx: i, author, time, text, link, stats };
  });
}
"""


def cmd_x_read(cfg: dict, user: Optional[str], n: int) -> None:
    with attached_browser(cfg) as (_p, _b, _c, page):
        url = f"https://x.com/{user}" if user else "https://x.com/home"
        page.goto(url, wait_until="load", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        time.sleep(2)
        last_count = 0
        tweets = []
        for _ in range(8):
            tweets = page.evaluate(X_TWEET_EXTRACT_JS, max(n, 1)) or []
            if len(tweets) >= n:
                break
            if len(tweets) == last_count and len(tweets) > 0:
                break
            last_count = len(tweets)
            page.evaluate("() => window.scrollBy(0, document.documentElement.clientHeight * 0.9)")
            time.sleep(1.2)
        tweets = page.evaluate(X_TWEET_EXTRACT_JS, n) or []
        if not tweets:
            snippet = (page.evaluate("() => document.body.innerText") or "")[:500]
            print(json.dumps({
                "ok": False,
                "url": page.url,
                "msg": "No tweets found. Likely not logged in or rate-limited.",
                "page_snippet": snippet,
            }, ensure_ascii=False, indent=2))
            return
        print(f"# X timeline: {page.url}  ({len(tweets)} tweets)")
        print("---")
        for t in tweets:
            print(f"[{t['idx']}] {t['author']}  ({t['time']})")
            print(t['text'])
            if t['stats']:
                print(f"  stats: {t['stats'].replace(chr(10), ' | ')}")
            if t['link']:
                print(f"  link: {t['link']}")
            print()


def cmd_x_post(cfg: dict, text: str) -> None:
    with attached_browser(cfg) as (_p, _b, _c, page):
        page.goto("https://x.com/compose/post", wait_until="load", timeout=20000)
        time.sleep(2)
        editor_selectors = [
            'div[data-testid="tweetTextarea_0"]',
            'div[role="textbox"][data-testid^="tweetTextarea"]',
            'div[contenteditable="true"][role="textbox"]',
        ]
        editor = None
        for sel in editor_selectors:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=5000)
                editor = loc
                break
            except Exception:
                continue
        if editor is None:
            print(json.dumps({"ok": False, "msg": "tweet editor not found — likely not logged in"}), file=sys.stderr)
            sys.exit(2)
        editor.click()
        editor.type(text)
        time.sleep(1)
        post_selectors = [
            'button[data-testid="tweetButton"]',
            'button[data-testid="tweetButtonInline"]',
        ]
        clicked = False
        for sel in post_selectors:
            try:
                btn = page.locator(sel).first
                btn.wait_for(state="visible", timeout=5000)
                btn.click()
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            editor.press("Control+Enter")
        time.sleep(3)
        print(json.dumps({"ok": True, "posted_text_len": len(text), "url": page.url}, ensure_ascii=False))


# ---------- main ----------

def main():
    cfg = load_config()
    p = argparse.ArgumentParser(prog="yumweb_pw.py", description="yumweb — dedicated Edge browser driver (Playwright backend)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("start", help="Launch the Edge CDP instance")
    sub.add_parser("stop", help="Stop the Edge CDP instance")
    sub.add_parser("status", help="Show status / tabs")

    g = sub.add_parser("goto"); g.add_argument("url")
    g = sub.add_parser("read")
    g.add_argument("--mode", choices=["text", "md", "html"], default="text")
    g.add_argument("--selector", default=None)
    g.add_argument("--max", type=int, default=cfg.get("default_read_max_chars", 8000))

    g = sub.add_parser("fetch"); g.add_argument("url"); g.add_argument("--max", type=int, default=cfg.get("default_read_max_chars", 8000))

    g = sub.add_parser("click"); g.add_argument("selector")
    g = sub.add_parser("type"); g.add_argument("selector"); g.add_argument("text"); g.add_argument("--enter", action="store_true")

    g = sub.add_parser("screenshot"); g.add_argument("path")
    g = sub.add_parser("eval"); g.add_argument("js")

    sub.add_parser("tabs")
    g = sub.add_parser("tab-new"); g.add_argument("url", nargs="?", default="about:blank")
    g = sub.add_parser("tab-switch"); g.add_argument("idx", type=int)
    g = sub.add_parser("tab-close"); g.add_argument("idx", type=int)

    g = sub.add_parser("x-read"); g.add_argument("--user", default=None); g.add_argument("--n", type=int, default=20)
    g = sub.add_parser("x-post"); g.add_argument("text")

    args = p.parse_args()

    if args.cmd == "start": cmd_start(cfg)
    elif args.cmd == "stop": cmd_stop(cfg)
    elif args.cmd == "status": cmd_status(cfg)
    elif args.cmd == "goto": cmd_goto(cfg, args.url)
    elif args.cmd == "read": cmd_read(cfg, args.mode, args.selector, args.max)
    elif args.cmd == "fetch": cmd_fetch(cfg, args.url, args.max)
    elif args.cmd == "click": cmd_click(cfg, args.selector)
    elif args.cmd == "type": cmd_type(cfg, args.selector, args.text, args.enter)
    elif args.cmd == "screenshot": cmd_screenshot(cfg, args.path)
    elif args.cmd == "eval": cmd_eval(cfg, args.js)
    elif args.cmd == "tabs": cmd_tabs(cfg)
    elif args.cmd == "tab-new": cmd_tab_new(cfg, args.url)
    elif args.cmd == "tab-switch": cmd_tab_switch(cfg, args.idx)
    elif args.cmd == "tab-close": cmd_tab_close(cfg, args.idx)
    elif args.cmd == "x-read": cmd_x_read(cfg, args.user, args.n)
    elif args.cmd == "x-post": cmd_x_post(cfg, args.text)
    else:
        p.print_help(); sys.exit(2)


if __name__ == "__main__":
    main()
