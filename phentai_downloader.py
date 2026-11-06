"""
HR Downloader 2 - HDoujin-style gallery downloader for Windows.

Why a real browser?
    hentairead.com sits behind a Cloudflare "Just a moment..." check that plain
    HTTP clients cannot pass. This version drives your installed Microsoft Edge
    (or Chrome) with its own private profile. The browser window is kept
    off-screen; it only pops up when Cloudflare wants you to click the check.
    Once cleared, page images are downloaded in parallel using the browser's
    cookies, with the browser itself as a fallback.

Features
    - Galleries saved as plain image folders (001.jpg, 002.jpg, ...)
    - Multi-select, colour labels, drag rows into Windows Explorer
    - Paste / Ctrl+V / drop URLs, optional clipboard watcher
    - Duplicate URLs are detected, highlighted and counted
    - Dark theme, queue saved between sessions, resume of partial downloads
"""
from __future__ import annotations

import base64
import json
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import (QBuffer, QByteArray, QEvent, QItemSelectionModel, QMimeData,
                            QPoint, QRect, QSize, Qt, QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import (QAction, QBrush, QColor, QContextMenuEvent, QCursor, QFont,
                           QDesktopServices, QDrag, QImageReader,
                           QGuiApplication, QIcon, QImage, QKeySequence, QPainter,
                           QPalette, QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QColorDialog, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QProgressDialog,
                               QPushButton, QSpinBox, QSplitter, QStyle,
                               QStyledItemDelegate, QStyleOptionProgressBar,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

APP_NAME = "PHentai Downloader"
VERSION = "4.4"
SUPPORTED_HOSTS = ["hentairead.com", "nhentai.net"] + [
    h for h in os.environ.get("HRD_EXTRA_HOSTS", "").split(",") if h]
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
IMG_URL_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif)(?:\?|$)", re.I)
BAD_IMG_RE = re.compile(
    r"(logo|avatar|icon|banner|/ads?/|ads?[_-]|thumb|favicon|emoji|gravatar|"
    r"/wp-content/themes/|/plugins/|-\d{2,3}x\d{2,3}\.)", re.I)


# --------------------------------------------------------------------------- #
# Settings / storage
# --------------------------------------------------------------------------- #
def config_dir() -> Path:
    base = Path(os.environ.get("APPDATA") or str(Path.home() / ".config"))
    p = base / "PHentaiDownloader4"
    if not p.exists():
        # first start: COPY settings + list from an older version (never modify it)
        p.mkdir(parents=True, exist_ok=True)
        for old in (base / "PHentaiDownloader", base / "HRDownloader"):
            if (old / "queue.json").exists() or (old / "settings.json").exists():
                for f in ("settings.json", "queue.json"):
                    try:
                        shutil.copy2(old / f, p / f)
                    except Exception:
                        pass
                break
    p.mkdir(parents=True, exist_ok=True)
    return p


SETTINGS_FILE = config_dir() / "settings.json"
QUEUE_FILE = config_dir() / "queue.json"
LOG_FILE = config_dir() / "log.txt"
DEFAULT_SETTINGS = {
    "output_dir": str(Path.home() / "Downloads" / "HRDownloader"),
    "threads": 8,
    "retries": 3,
    "delay": 0.0,
    "settings_version": 3,
    "browser_path": "",          # blank = auto-detect Edge / Chrome
    "show_browser": False,       # keep browser window visible (debugging)
    "watch_clipboard": False,
    "autostart": False,
    "thumb_on": True,
    "thumb_size": 220,
    "extra_browser_args": [],
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
    except Exception:
        pass
    if int(s.get("settings_version", 1)) < 3:     # faster defaults for old settings
        s["threads"] = max(int(s.get("threads", 4)), 8)
        s["delay"] = 0.0
        s["settings_version"] = 3
    return s


def save_settings(s: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), "utf-8")


WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
WIN_MAX_PATH = 259          # 260 incl. the terminating NUL


def safe_name(name: str, limit: int = 150) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", unescape(name))
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = (name or "untitled")[:limit].rstrip(" .")
    if name.split(".")[0].upper() in WIN_RESERVED:
        name = "_" + name
    return name or "untitled"


def fit_name(name: str, parent=None, reserve: int = 0, limit: int = 150) -> str:
    """File/folder name that keeps the full path within Windows' 260-char limit.
    reserve = characters still needed after this name (e.g. '\\0001.webp.part')."""
    room = limit
    if parent is not None:
        room = min(room, WIN_MAX_PATH - len(str(Path(parent))) - 1 - reserve)
    room = max(room, 16)
    return safe_name(name, room)


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
URL_FIND_RE = re.compile(r"https?://[^\s<>\"'\]\[)]+", re.I)


def extract_urls(text: str) -> list[str]:
    return [u.rstrip(".,;") for u in URL_FIND_RE.findall(text or "")]


def is_builtin(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in SUPPORTED_HOSTS)


def is_supported(url: str) -> bool:
    """Any web page can be tried: built-in sites, gallery-dl's ~270 sites, or the
    generic 'collect the big images on the page' method."""
    p = urlparse(url)
    return p.scheme in ("http", "https") and bool(p.hostname)


_GDL_SKIP = {"directlink", "twitter", "reddit", "instagram", "facebook", "tiktok",
             "bluesky", "mastodon", "tumblr", "flickr", "wikimedia", "wikiart",
             "imgur", "discord", "telegraph", "vk", "youtube", "generic"}


def gdl_category(url: str) -> str | None:
    try:
        from gallery_dl import extractor as gex
        ex = gex.find(url)
        return ex.category if ex else None
    except Exception:
        return None


def is_known_site(url: str) -> bool:
    """Used by the clipboard watcher: only auto-add links of recognised gallery sites."""
    if is_builtin(url):
        return True
    cat = gdl_category(url)
    return bool(cat) and cat not in _GDL_SKIP


def gallery_root(url: str) -> str:
    """.../hentai/<slug>/english/p/3/  ->  .../hentai/<slug>/ ; other sites keep
    their path + query (only the #fragment and a trailing slash are normalised)."""
    p = urlparse(url.strip())
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    parts = [x for x in p.path.split("/") if x]
    if is_builtin(url):
        if len(parts) >= 2 and parts[0].lower() in ("hentai", "g"):
            parts = parts[:2]
        return f"{p.scheme or 'https'}://{netloc}/" + "/".join(parts) + ("/" if parts else "")
    path = "/" + "/".join(parts) + ("/" if parts and p.path.endswith("/") else "")
    return f"{p.scheme or 'https'}://{netloc}{path}" + (f"?{p.query}" if p.query else "")


def normalize_url(url: str) -> str:
    return gallery_root(url)


def title_from_url(url: str) -> str:
    """Readable name from the URL slug, shown until the real title is fetched."""
    parts = [x for x in urlparse(url).path.split("/") if x]
    if len(parts) >= 2 and parts[0].lower() == "g" and parts[1].isdigit():
        return f"nhentai {parts[1]}"
    slug = parts[1] if len(parts) >= 2 and parts[0].lower() == "hentai" else \
        (next((x for x in reversed(parts) if not x.isdigit() and len(x) > 3), None)
         or (f"{urlparse(url).hostname} {'/'.join(parts[-2:])}" if parts else url))
    words = re.sub(r"[-_]+", " ", slug).strip()
    return words[:1].upper() + words[1:] if words else url


# --------------------------------------------------------------------------- #
# Image helpers
# --------------------------------------------------------------------------- #
def sniff_ext(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    return None


def pick_pages(cands: list[dict]) -> list[str]:
    """Choose the real page images out of everything found on a page."""
    seen, out = set(), []
    for c in cands:
        u = c.get("u") or ""
        if not IMG_URL_RE.search(u) or BAD_IMG_RE.search(u) or u in seen:
            continue
        if 0 < (c.get("w") or 0) < 250:   # loaded and tiny -> icon / thumbnail
            continue
        seen.add(u)
        out.append(u)
    if len(out) > 3:  # real pages normally share one directory
        groups: dict[str, list[str]] = {}
        for u in out:
            groups.setdefault(u.split("?")[0].rsplit("/", 1)[0], []).append(u)
        best = max(groups.values(), key=len)
        if len(best) >= 3:
            out = best
    return out


# --------------------------------------------------------------------------- #
# Browser engine (runs entirely inside the worker thread)
# --------------------------------------------------------------------------- #
class Cancelled(Exception):
    pass


@dataclass
class Gallery:
    url: str
    title: str
    pages: list[list[str]] = field(default_factory=list)   # candidate URLs per page
    referer: str = ""
    expected: int = 0
    low_from: int = 99   # candidate index from which an image is preview quality
    cover: str = ""      # small image for the hover preview
    session: object = None          # HTTP session to download with (gallery-dl)
    children: list[str] = field(default_factory=list)   # list/tag page -> galleries

    @property
    def images(self) -> list[str]:
        return [c[0] for c in self.pages]


JS_INFO = r"""() => {
  const og = document.querySelector('meta[property="og:title"]');
  const h1 = document.querySelector('h1');
  return {
    title: (h1 && h1.innerText.trim()) || (og && og.content) || document.title,
    links: [...document.querySelectorAll('a[href]')].map(a => ({h: a.href,
             t: (a.innerText || a.title || '').trim().toLowerCase().slice(0, 60)})),
    text: (document.body ? document.body.innerText : '').slice(0, 20000)
  };
}"""

JS_IMAGES = r"""() => {
  const out = [], seen = new Set();
  const add = (u, w, h, src) => {
    if (!u) return; u = u.trim();
    if (u.startsWith('data:') || u.startsWith('blob:')) return;
    try { u = new URL(u, location.href).href } catch (e) { return }
    if (seen.has(u)) return; seen.add(u); out.push({u, w, h, src});
  };
  const lastOfSet = s => s ? s.split(',').pop().trim().split(/\s+/)[0] : '';
  for (const img of document.querySelectorAll('img')) {
    const d = img.dataset || {};
    let u = d.src || d.lazySrc || d.original || d.url || lastOfSet(d.srcset) ||
            lastOfSet(img.getAttribute('srcset')) || img.currentSrc || img.src;
    add(u, img.naturalWidth || 0, img.naturalHeight || 0, 'img');
  }
  const html = document.documentElement.innerHTML;
  const re = /(?:https?:)?\\?\/\\?\/[^"'\s<>()]+?\.(?:jpe?g|png|webp|gif|avif)(?:\?[^"'\s<>()]*)?/gi;
  for (const m of html.matchAll(re)) add(m[0].replace(/\\\//g, '/'), 0, 0, 'html');
  return out;
}"""

JS_HR_PREVIEWS = r"""() => {
  const pick = img => {
    const d = img.dataset || {};
    const ss = (img.getAttribute('srcset') || d.srcset || '').split(',')
                 .map(x => x.trim().split(/\s+/)[0]).filter(Boolean);
    return d.src || d.lazySrc || d.original || img.getAttribute('src') || ss[ss.length - 1] || '';
  };
  let imgs = [...document.querySelectorAll('ul.lazy-listing__list img, .lazy-listing__list img')];
  if (!imgs.length)
    imgs = [...document.querySelectorAll('img')].filter(i => /\/preview\//.test(pick(i)));
  const out = [];
  for (const i of imgs) {
    let u = pick(i);
    if (!u || u.startsWith('data:')) continue;
    u = new URL(u, location.href).href;
    if (!out.includes(u)) out.push(u);
  }
  return out;
}"""

JS_FETCH_MANY = r"""async (urls) => {
  const enc = buf => { const b = new Uint8Array(buf); let s = '';
    for (let i = 0; i < b.length; i += 32768) s += String.fromCharCode.apply(null, b.subarray(i, i + 32768));
    return btoa(s); };
  return Promise.all(urls.map(async u => {
    try { const r = await fetch(u, {referrerPolicy: 'unsafe-url'});
          if (!r.ok) return {e: 'HTTP ' + r.status};
          return {d: enc(await r.arrayBuffer())}; }
    catch (e) { return {e: String(e)} } }));
}"""


def page_images(page) -> list[dict]:
    return [c for c in page.evaluate(JS_IMAGES)
            if IMG_URL_RE.search(c["u"]) and not BAD_IMG_RE.search(c["u"])]


JS_SCROLL = r"""() => { window.scrollBy(0, Math.max(400, window.innerHeight * 0.9));
  return [window.scrollY + window.innerHeight, document.documentElement.scrollHeight]; }"""


def is_closed_error(e: Exception) -> bool:
    return "has been closed" in str(e) or type(e).__name__ == "TargetClosedError"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _profile_pids(profile: Path) -> set[int]:
    if sys.platform != "win32":
        try:
            out = subprocess.run(["pgrep", "-f", "--", f"--user-data-dir={profile}"],
                                 capture_output=True, text=True, timeout=10).stdout
            return {int(x) for x in out.split() if x.isdigit()}
        except Exception:
            return set()
    try:
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
              f"$_.CommandLine.Contains('{profile}') }} | ForEach-Object {{ $_.ProcessId }}")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             timeout=30, capture_output=True, text=True).stdout
        return {int(x) for x in out.split() if x.isdigit()}
    except Exception:
        return set()


def bring_to_foreground(profile: Path) -> None:
    """Raise the browser window above other windows (Windows only)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        pids = _profile_pids(profile)
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _lp):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd) and \
                    user32.GetWindowTextLengthW(hwnd) > 0:
                found.append(hwnd)
            return True
        user32.EnumWindows(cb, 0)
        for hwnd in found:
            user32.ShowWindow(hwnd, 9)                       # SW_RESTORE
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003)  # TOPMOST, no move/size
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0003)  # back to normal z-order
            user32.SetForegroundWindow(hwnd)
            user32.FlashWindow(hwnd, True)
    except Exception:
        pass


class BrowserSession:
    OFFSCREEN = (-3200, -3200)

    def __init__(self, settings: dict, log, attention, manual_done=None):
        self.st, self.log, self.attention = settings, log, attention
        self.manual_done = manual_done or threading.Event()
        self.pw = self.browser = self.ctx = self.page = self.img_page = None
        self.proc: subprocess.Popen | None = None
        self.ua = ""
        self.net_imgs: list[tuple[str, int]] = []

    # -- lifecycle ---------------------------------------------------------- #
    def find_browser(self) -> str:
        bp = (self.st.get("browser_path") or "").strip().strip('"')
        if bp and Path(bp).exists():
            return bp
        cands = []
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                          Path(base) / "Google/Chrome/Application/chrome.exe"]
        for c in cands:
            if c.exists():
                return str(c)
        for n in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
            w = shutil.which(n)
            if w:
                return w
        raise RuntimeError("Microsoft Edge or Google Chrome was not found. "
                           "Set the browser path in Settings.")

    @property
    def profile(self) -> Path:
        return config_dir() / "browser-profile"

    @property
    def alive(self) -> bool:
        # Only the CDP connection matters. On Windows the msedge.exe we launch can
        # exit right away while the real browser keeps running in another process.
        try:
            return bool(self.browser and self.browser.is_connected())
        except Exception:
            return False

    def _port_file(self) -> Path:
        return self.profile / "DevToolsActivePort"

    def _read_port(self) -> int | None:
        try:
            line = self._port_file().read_text().splitlines()[0].strip()
            return int(line) if line.isdigit() and int(line) > 0 else None
        except Exception:
            return None

    def _connect(self, port: int, timeout_ms: int = 5000) -> bool:
        try:
            self.browser = self.pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=timeout_ms)
            return True
        except Exception:
            self.browser = None
            return False

    def kill_profile_browsers(self) -> None:
        """Kill any browser processes still using OUR private profile (never the
        user's normal Edge/Chrome)."""
        marker = str(self.profile)
        try:
            if sys.platform == "win32":
                ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
                      f"$_.CommandLine.Contains('{marker}') }} | "
                      "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                      "-ErrorAction SilentlyContinue }")
                subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               timeout=30, capture_output=True)
            else:
                subprocess.run(["pkill", "-f", "--", f"--user-data-dir={marker}"],
                               timeout=10, capture_output=True)
        except Exception as e:
            self.log(f"(cleanup of old browser failed: {e})")
        time.sleep(1.0)
        for name in ("DevToolsActivePort", "SingletonLock", "SingletonCookie",
                     "SingletonSocket", "lockfile"):
            try:
                (self.profile / name).unlink()
            except Exception:
                pass

    def _launch(self, exe: str) -> None:
        try:
            self._port_file().unlink()
        except Exception:
            pass
        self.profile.mkdir(parents=True, exist_ok=True)
        args = [exe, "--remote-debugging-port=0", f"--user-data-dir={self.profile}",
                "--no-first-run", "--no-default-browser-check",
                "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
                "--disable-features=Translate,msEdgeSidebarV2,msUndersideButton,"
                "msStartupBoost",
                "--window-size=1100,850"]
        if not self.st.get("show_browser"):
            args.append("--window-position=%d,%d" % self.OFFSCREEN)
        args += list(self.st.get("extra_browser_args") or [])
        args.append("about:blank")
        self.log(f"Starting browser: {Path(exe).name}")
        self.proc = subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _wait_port_and_connect(self, seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            port = self._read_port()
            if port and self._connect(port):
                return True
            time.sleep(0.5)
        return False

    def start(self) -> None:
        if self.alive:
            return
        self.close(kill=False)
        from playwright.sync_api import sync_playwright
        exe = self.find_browser()
        self.pw = sync_playwright().start()

        # 1) a browser from an earlier run may still be open on our profile -> reuse it
        port = self._read_port()
        if port and self._connect(port, 3000):
            self.log("Reconnected to the already-running browser.")
        else:
            if port or (self.profile / "lockfile").exists() or \
                    (self.profile / "SingletonLock").is_symlink():
                self.log("Closing a leftover browser from an earlier run...")
                self.kill_profile_browsers()
            # 2) launch; if the profile is held by a stuck instance, kill it and retry
            for attempt in (1, 2):
                if attempt == 2:
                    self.log("Browser did not answer - closing leftover browser and retrying...")
                    self.kill_profile_browsers()
                self._launch(exe)
                if self._wait_port_and_connect(45 if attempt == 1 else 60):
                    break
            else:
                self.kill_profile_browsers()
                raise RuntimeError(
                    "Could not start/connect to the browser. Close every Microsoft Edge "
                    "window that HR Downloader opened (or restart the PC) and try again. "
                    f"Browser: {exe}")
        self.ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        # A brand-new Edge profile shows its first-run/welcome screen, which can close
        # or replace the starting tab - give it a moment, then work in our own tab.
        time.sleep(1.5)
        self.page = None
        self.ensure_page()
        self.ua = self.page.evaluate("navigator.userAgent")

    def ensure_page(self):
        """Return a usable tab, replacing it if Edge (or the user) closed it."""
        if not self.alive:
            self.close(kill=False)
            self.start()
            return self.page
        if self.page is not None and not self.page.is_closed():
            return self.page
        pages = [p for p in self.ctx.pages if not p.is_closed()
                 and not p.url.startswith(("edge://", "chrome://"))]
        self.page = pages[0] if pages else self.ctx.new_page()
        self.page.on("response", self._on_response)
        if not self.st.get("show_browser"):
            self._set_window(self.page, False)
        return self.page

    def close(self, kill: bool = True) -> None:
        had_browser = self.browser is not None
        try:
            if self.browser and self.browser.is_connected():
                try:
                    self.browser.new_browser_cdp_session().send("Browser.close")
                except Exception:
                    pass
                try:
                    self.browser.close()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        self.pw = self.browser = self.ctx = self.page = self.img_page = None
        self.proc = None
        if kill and had_browser:
            time.sleep(2.0)
            self.kill_profile_browsers()   # make sure nothing is left behind

    def _on_response(self, resp) -> None:
        try:
            if resp.request.resource_type == "image":
                self.net_imgs.append((resp.url, int(resp.headers.get("content-length") or 0)))
        except Exception:
            pass

    # -- window control (only used for Cloudflare) -------------------------- #
    def _set_window(self, page, visible: bool) -> None:
        try:
            cdp = self.ctx.new_cdp_session(page)
            wid = cdp.send("Browser.getWindowForTarget")["windowId"]
            if visible:
                cdp.send("Browser.setWindowBounds",
                         {"windowId": wid, "bounds": {"windowState": "normal"}})
                cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {
                    "left": 160, "top": 90, "width": 1000, "height": 800}})
                page.bring_to_front()
                bring_to_foreground(self.profile)
            else:
                cdp.send("Browser.setWindowBounds",
                         {"windowId": wid, "bounds": {"windowState": "normal"}})
                x, y = self.OFFSCREEN
                cdp.send("Browser.setWindowBounds",
                         {"windowId": wid, "bounds": {"left": x, "top": y}})
            cdp.detach()
        except Exception as e:
            self.log(f"(could not move browser window: {e})")

    def show_window(self, visible: bool) -> None:
        if self.alive and self.page:
            self._set_window(self.page, visible)

    # -- Cloudflare ---------------------------------------------------------- #
    @staticmethod
    def challenge_state(page) -> bool | None:
        """True = challenge page, False = normal page, None = can't tell (navigating)."""
        try:
            t = (page.title() or "").lower()
            if any(k in t for k in ("just a moment", "attention required",
                                     "un instant", "einen moment", "checking your browser",
                                     "lütfen bekleyin", "un momento")):
                return True
            return page.locator("#challenge-form, #challenge-running, "
                                "#cf-challenge-running").count() > 0
        except Exception:
            return None

    @classmethod
    def is_challenge(cls, page) -> bool:
        return bool(cls.challenge_state(page))

    def _tab_title(self, cdp) -> str | None:
        """Title of the tab read from the browser side - nothing runs inside the page,
        so the check box is left completely alone while the person clicks it."""
        try:
            return cdp.send("Target.getTargetInfo")["targetInfo"].get("title", "")
        except Exception:
            return None

    def _wait_clear(self, page, seconds: float, tok) -> bool:
        end = time.time() + seconds
        clear = 0
        try:
            cdp = self.ctx.new_cdp_session(page)
        except Exception:
            cdp = None
        try:
            while time.time() < end:
                if tok.is_set():
                    raise Cancelled()
                time.sleep(1)
                if page.is_closed() or not self.alive:
                    raise RuntimeError("The browser window/tab was closed before the "
                                       "Cloudflare check was completed - press Start to try again.")
                title = self._tab_title(cdp) if cdp else None
                if title is None:
                    st = None
                else:
                    t = title.lower()
                    st = any(k in t for k in ("just a moment", "attention required",
                                               "un instant", "einen moment",
                                               "checking your browser", "lütfen bekleyin",
                                               "un momento"))
                clear = clear + 1 if st is False else 0
                if clear >= 2:      # stable for 2 checks (not mid-redirect)
                    return True
            return False
        finally:
            try:
                if cdp:
                    cdp.detach()
            except Exception:
                pass

    def handle_challenge(self, page, tok, is_main: bool = True):
        """Returns the page to continue with (re-found after reconnecting)."""
        if not self.is_challenge(page):
            return page
        self.log("Cloudflare check detected - waiting for it to clear...")
        if self._wait_clear(page, 8, tok):
            self.log("Cloudflare check cleared automatically.")
            return page
        url = page.url
        port = self._read_port()
        # Cloudflare rejects the check while a program is connected to the browser,
        # even when a person clicks it. So the program brings the window on screen and
        # then fully DISCONNECTS - the person completes the check in a plain Edge
        # window. Only the tab title is read (from Edge's local status page) to know
        # when it has passed; nothing is clicked or run in the page.
        self._set_window(page, True)
        self._detach()
        bring_to_foreground(self.profile)
        self.manual_done.clear()
        self.attention("!manual:Cloudflare check: click the check in the Edge window. "
                       "The program has let go of the browser so the check sees a normal "
                       "window. If it keeps failing, press F5 in that window. Downloading "
                       "continues by itself once it passes.")
        self.log("Waiting for you to pass the check in the Edge window...")
        try:
            self._wait_passive(port, url, tok, 600)
        finally:
            self.attention("")
        self.start()                       # reconnect to the same window
        pages = [p for p in self.ctx.pages if not p.is_closed()
                 and urlparse(p.url).hostname == urlparse(url).hostname]
        page = pages[0] if pages else self.ensure_page()
        if not self.st.get("show_browser"):
            self._set_window(page, False)
        if self.is_challenge(page) and not self._wait_clear(page, 15, tok):
            raise RuntimeError("Cloudflare still asks for the check - press Start to try "
                               "again (in the Edge window, F5 reloads the check).")
        self.log("Cloudflare check passed - continuing.")
        if is_main:
            self.page = page
            try:
                page.on("response", self._on_response)
            except Exception:
                pass
        else:
            self.img_page = page
        return page

    def _detach(self) -> None:
        """Disconnect from the browser WITHOUT closing it."""
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        self.pw = self.browser = self.ctx = self.page = self.img_page = None

    def _wait_passive(self, port, url: str, tok, seconds: float) -> None:
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        host = urlparse(url).hostname
        end, clear = time.time() + seconds, 0
        while True:
            if tok.is_set():
                raise Cancelled()
            if time.time() > end:
                raise RuntimeError("The Cloudflare check was not completed within 10 "
                                   "minutes - press Start to try again.")
            time.sleep(1.5)
            try:
                targets = json.loads(opener.open(
                    f"http://127.0.0.1:{port}/json/list", timeout=5).read())
            except Exception:
                raise RuntimeError("The Edge window was closed before the Cloudflare check "
                                   "was completed - press Start to try again.")
            tabs = [t for t in targets if t.get("type") == "page"
                    and urlparse(t.get("url", "")).hostname == host]
            if not tabs:
                if not any(t.get("type") == "page" for t in targets):
                    raise RuntimeError("The Edge window was closed before the Cloudflare "
                                       "check was completed - press Start to try again.")
                continue
            if self.manual_done.is_set():
                return
            title = (tabs[0].get("title") or "").lower()
            challenged = any(k in title for k in (
                "just a moment", "attention required", "un instant", "einen moment",
                "checking your browser", "lütfen bekleyin", "un momento"))
            clear = 0 if challenged or not title else clear + 1
            if clear >= 2:
                return

    def goto(self, url: str, tok, page=None, referer: str | None = None):
        self.start()
        own = page is None
        page = page or self.ensure_page()
        if tok.is_set():
            raise Cancelled()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=90000, referer=referer)
        except Exception as e:
            if not is_closed_error(e):
                raise
            self.log("Browser tab was closed - opening a new one and retrying...")
            if own:
                self.page = None
            elif self.img_page is page:
                self.img_page = None
            page = self.ensure_page() if own else self.ctx.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=90000, referer=referer)
        if self.is_challenge(page):
            page = self.handle_challenge(page, tok, is_main=own)
            if page.url.rstrip("/") != url.rstrip("/"):
                resp = page.goto(url, wait_until="domcontentloaded", timeout=90000,
                                 referer=referer)
            else:
                resp = None
        try:
            page.wait_for_load_state("load", timeout=20000)
        except Exception:
            pass
        if resp is not None and resp.status >= 400 and not self.is_challenge(page):
            raise RuntimeError(f"HTTP {resp.status} for {url}")
        return page, resp

    def scroll_all(self, page, tok, fast: bool = False) -> None:
        same = 0
        for _ in range(600):
            if tok.is_set():
                raise Cancelled()
            pos, height = page.evaluate(JS_SCROLL)
            same = same + 1 if pos >= height - 5 else 0
            if same >= 3:
                break
            page.wait_for_timeout(60 if fast else 200)
        page.wait_for_timeout(200 if fast else 500)

    # -- gallery parsing ----------------------------------------------------- #
    def _dump_debug(self, page, name: str) -> str:
        d = config_dir() / "debug"
        d.mkdir(exist_ok=True)
        p = d / f"{safe_name(name, 60)}.html"
        try:
            p.write_text(page.content(), "utf-8")
        except Exception:
            pass
        return str(p)

    def fetch_gallery(self, url: str, tok) -> Gallery:
        host = (urlparse(url).hostname or "").lower()
        if host == "nhentai.net" or host.endswith(".nhentai.net"):
            gal = self.fetch_nhentai(url, tok)
        elif is_builtin(url):
            gal = self.fetch_hentairead(url, tok)
        else:
            gal = None
            if gdl_category(url):
                try:
                    gal = self.fetch_gallerydl(url, tok)
                except Cancelled:
                    raise
                except Exception as e:
                    self.log(f"gallery-dl could not read it ({e}) - trying the browser...")
            if gal is None:
                gal = self.fetch_generic(url, tok)
        if not gal.cover and gal.pages:
            gal.cover = gal.pages[0][min(gal.low_from, len(gal.pages[0]) - 1)]
        return gal

    # ---- ~270 sites through gallery-dl -------------------------------------- #
    @staticmethod
    def _gdl_title(kw: dict, fallback: str) -> str:
        for k in ("title_en", "title", "manga", "gallery_title", "name", "title_jp",
                  "title_ja", "post_title"):
            v = kw.get(k)
            if isinstance(v, str) and v.strip():
                t = v.strip()
                break
        else:
            gid = kw.get("gallery_id") or kw.get("id")
            t = f"{kw.get('category', fallback)} {gid}" if gid else fallback
        ch = kw.get("chapter")
        if kw.get("manga") and t == kw.get("manga") and ch not in (None, "", 0):
            t += f" - Ch.{ch}{kw.get('chapter_minor') or ''}"
        return t

    def _gdl_run(self, url: str, tok, cookies: dict | None):
        from gallery_dl import config as gcfg
        from gallery_dl import extractor as gex
        from gallery_dl.extractor.message import Message
        gcfg.clear()
        gcfg.set(("extractor",), "user-agent", self.ua or DEFAULT_UA)
        if cookies:
            gcfg.set(("extractor",), "cookies", cookies)
        ex = gex.find(url)
        if ex is None:
            raise RuntimeError("no gallery-dl extractor")
        pages, children, title = [], [], ""
        for msg in ex:
            if tok.is_set():
                raise Cancelled()
            # message layouts differ between gallery-dl versions: the metadata
            # dict is always the last element
            kw = msg[-1] if isinstance(msg[-1], dict) else {}
            if msg[0] == Message.Directory and not title and kw:
                title = self._gdl_title(kw, title_from_url(url))
            elif msg[0] == Message.Url:
                u = msg[1]
                if isinstance(u, str) and u.startswith(("http://", "https://")):
                    pages.append([u])
                    if not title and kw:
                        title = self._gdl_title(kw, title_from_url(url))
            elif msg[0] == Message.Queue and isinstance(msg[1], str):
                children.append(msg[1])
        return ex, pages, children, title

    def fetch_gallerydl(self, url: str, tok) -> Gallery:
        self.log(f"Opening {url}  (via gallery-dl: {gdl_category(url)})")
        try:
            ex, pages, children, title = self._gdl_run(url, tok, None)
        except Cancelled:
            raise
        except Exception as e:
            if not re.search(r"403|503|cloudflare|forbidden|challenge", str(e), re.I):
                raise
            # Cloudflare: pass it in the real browser, then reuse its cookies
            self.log("Site is protected - opening it in the browser first...")
            self.goto(url, tok)
            host = urlparse(url).hostname or ""
            cookies = {c["name"]: c["value"] for c in self.ctx.cookies()
                       if host.endswith(c.get("domain", "").lstrip("."))}
            ex, pages, children, title = self._gdl_run(url, tok, cookies)
        if not pages and not children:
            raise RuntimeError("gallery-dl found no images")
        if children and not pages:
            self.log(f"List page: found {len(children)} galleries")
        else:
            self.log(f"'{title}' - {len(pages)} pages")
        return Gallery(url=url, title=title or title_from_url(url), pages=pages,
                       referer=(ex.root.rstrip("/") + "/") if str(getattr(ex, "root", "") or "")
                       .startswith("http") else url, session=ex.session,
                       children=children if not pages else [])

    # ---- any other site: real browser, collect the large images ------------- #
    def fetch_generic(self, url: str, tok) -> Gallery:
        self.log(f"Opening {url}  (generic: collecting the large images)")
        self.net_imgs.clear()
        page, _ = self.goto(url, tok)
        self.scroll_all(page, tok)
        info = page.evaluate(JS_INFO)
        cands = page.evaluate(JS_IMAGES)
        big = [c for c in cands if (c.get("w") or 0) >= 300 or not c.get("w")]
        imgs = pick_pages(big)
        if len(imgs) < 2:
            net = [{"u": u, "w": 0} for u, size in self.net_imgs if size == 0 or size > 50000]
            alt = pick_pages(net)
            if len(alt) > len(imgs):
                imgs = alt
        if not imgs:
            dbg = self._dump_debug(page, title_from_url(url))
            raise RuntimeError(f"No gallery images found on this page. Saved for inspection: {dbg}")
        title = (info.get("title") or "").strip() or title_from_url(url)
        title = re.split(r"\s+[|\u2013\u2014-]\s+(?=[^|\u2013\u2014-]*$)", title)[0].strip() or title
        self.log(f"'{title}' - {len(imgs)} images")
        return Gallery(url=url, title=title, pages=[[u] for u in imgs], referer=url)

    # ---- hentairead.com ---------------------------------------------------- #
    @staticmethod
    def hr_candidates(preview: str) -> tuple[list[str], int]:
        """Full-size candidates for a gallery preview image, best first.
        Returns (candidates, index_of_low_quality_preview)."""
        def strip_size(u):
            return re.sub(r"-\d{2,4}x\d{2,4}(?=\.\w+(?:\?|$))", "", u)
        full = preview.replace("/preview/", "/").replace("/preview", "")
        cands = [strip_size(full.replace("hencover", "henread")),
                 full.replace("hencover", "henread"),
                 strip_size(full), full, preview]
        out = []
        for c in cands:
            if c not in out:
                out.append(c)
        return out, out.index(preview)

    def fetch_hentairead(self, url: str, tok) -> Gallery:
        root = gallery_root(url)
        self.log(f"Opening {root}")
        page, _ = self.goto(root, tok)
        self.scroll_all(page, tok, fast=True)
        info = page.evaluate(JS_INFO)
        title = re.sub(r"\s*[-|–]\s*hentai\s*read.*$", "", info["title"] or "",
                       flags=re.I).strip() or "untitled"
        m = re.search(r"(?:pages?\s*[:\-]?\s*(\d{1,4}))|(?:(\d{1,4})\s*pages?)",
                      info["text"], re.I)
        expected = int(m.group(1) or m.group(2)) if m else 0

        previews = page.evaluate(JS_HR_PREVIEWS)
        pages, low_from = [], 99
        for u in previews:
            cands, low = self.hr_candidates(u)
            pages.append(cands)
            low_from = min(low_from, low)
        if pages:
            self.log(f"Found {len(pages)} pages on the gallery page"
                     + (f" (site lists {expected})" if expected else ""))
        if not pages:   # unknown layout -> slow reader walk as a last resort
            self.log("Gallery page has no page list - falling back to the reader (slow).")
            imgs = self._reader_walk(page, info, root, expected, tok)
            pages, low_from = [[u] for u in imgs], 99
        if not pages:
            dbg = self._dump_debug(page, root.rstrip("/").rsplit("/", 1)[-1])
            raise RuntimeError(f"No page images found. Page saved for inspection: {dbg}")
        if expected and expected != len(pages):
            self.log(f"Note: site lists {expected} pages, found {len(pages)}.")
        self.log(f"'{title}' - {len(pages)} pages")
        return Gallery(url=root, title=title, pages=pages, referer=root,
                       expected=expected, low_from=low_from,
                       cover=previews[0] if previews else "")

    def _reader_walk(self, page, info, root, expected, tok) -> list[str]:
        links = [lk["h"] for lk in info["links"] if lk["h"].startswith(root)]
        paged = [h for h in links if re.search(r"/p/\d+/?(?:[?#].*)?$", h)]
        if not paged:
            return []
        base = paged[0][: re.search(r"/p/\d+", paged[0]).start()]
        nums = [int(x) for x in re.findall(r"/p/(\d+)", " ".join(paged))]
        last = max(nums + [expected, 1])
        images: list[str] = []
        for n in range(1, last + 1):
            p, _ = self.goto(f"{base}/p/{n}/", tok, referer=root)
            best = max(page_images(p), key=lambda c: (c.get("w") or 0) * (c.get("h") or 0),
                       default=None)
            if best and best["u"] not in images:
                images.append(best["u"])
            self.log(f"Scanned reader page {n}/{last}")
        return images

    # ---- nhentai.net ------------------------------------------------------- #
    def _get_json(self, urls: list[str], referer: str, tok) -> dict:
        errors = []
        sess = self._http_session(referer, accept="application/json")
        for u in urls:          # fast path: plain HTTP
            try:
                r = sess.get(u, timeout=30)
                if r.status_code == 200:
                    return r.json()
                errors.append(f"{u}: HTTP {r.status_code}")
                if r.status_code == 404:
                    continue
            except Exception as e:
                errors.append(f"{u}: {e}")
        for u in urls:          # Cloudflare / DNS trouble -> real browser
            try:
                page, resp = self.goto(u, tok)
                return json.loads(page.evaluate("() => document.body.innerText"))
            except Cancelled:
                raise
            except Exception as e:
                errors.append(f"{u} (browser): {e}")
        raise RuntimeError("nhentai API failed: " + " | ".join(errors[-3:]))

    def fetch_nhentai(self, url: str, tok) -> Gallery:
        m = re.search(r"/g/(\d+)", url)
        if not m:
            raise RuntimeError("Use a gallery link like https://nhentai.net/g/123456/")
        gid = m.group(1)
        root = f"https://nhentai.net/g/{gid}/"
        self.log(f"Opening {root}")
        data = self._get_json([f"https://nhentai.net/api/v2/galleries/{gid}",
                               f"https://nhentai.net/api/gallery/{gid}"], root, tok)
        t = data.get("title") or {}
        title = (t.get("english") or t.get("pretty") or t.get("japanese")
                 or f"nhentai {gid}") if isinstance(t, dict) else str(t)
        servers = ["i1", "i2", "i3", "i4", "i"]
        pages: list[list[str]] = []
        plist = data.get("pages") if isinstance(data.get("pages"), list) else None
        if plist and isinstance(plist[0], dict) and plist[0].get("path"):
            for n, p in enumerate(plist):
                path = p["path"]
                if path.startswith("http"):
                    pages.append([path])
                    continue
                order = servers[n % 4:4] + servers[:n % 4] + ["i"]
                pages.append([f"https://{s}.nhentai.net/{path.lstrip('/')}" for s in order])
        else:
            media = data["media_id"]
            exts = {"j": "jpg", "p": "png", "g": "gif", "w": "webp", "a": "avif"}
            for n, p in enumerate(data["images"]["pages"], 1):
                ext = exts.get(p.get("t"), "jpg")
                order = servers[n % 4:4] + servers[:n % 4] + ["i"]
                main = [f"https://{s}.nhentai.net/galleries/{media}/{n}.{ext}" for s in order]
                alt = [f"https://i.nhentai.net/galleries/{media}/{n}.{e}"
                       for e in ("jpg", "png", "webp") if e != ext]
                pages.append(main + alt)
        if not pages:
            raise RuntimeError("nhentai returned no pages for this gallery.")
        self.log(f"'{title}' - {len(pages)} pages")
        return Gallery(url=root, title=title, pages=pages, referer=root, expected=len(pages))

    # -- downloading --------------------------------------------------------- #
    def _resolve(self, hosts: set[str]) -> list[str]:
        """Resolve image hosts with Windows' own DNS (curl's built-in resolver failed
        on some PCs with 'Misformatted DNS reply'); DNS-over-HTTPS as backup."""
        entries = []
        for h in hosts:
            ip = None
            try:
                ip = socket.getaddrinfo(h, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
            except Exception:
                try:
                    from curl_cffi import requests as creq
                    j = creq.get(f"https://1.1.1.1/dns-query?name={h}&type=A",
                                 headers={"accept": "application/dns-json"}, timeout=10).json()
                    ip = next((a["data"] for a in j.get("Answer", []) if a.get("type") == 1),
                              None)
                except Exception:
                    pass
            if ip:
                entries += [f"{h}:443:{ip}", f"{h}:80:{ip}"]
        return entries

    def _http_session(self, referer: str, accept: str | None = None, resolve=None):
        ua = self.ua or DEFAULT_UA
        try:
            from curl_cffi import CurlOpt
            from curl_cffi import requests as creq
            opts = {CurlOpt.RESOLVE: resolve} if resolve else None
            s = creq.Session(impersonate="chrome", curl_options=opts)
        except Exception:
            import requests as creq  # type: ignore
            s = creq.Session()
        s.headers.update({"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9",
                          "Accept": accept or
                          "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                          "Referer": referer})
        if self.ctx is not None:
            try:
                for c in self.ctx.cookies():
                    s.cookies.set(c["name"], c["value"], domain=c.get("domain", ""),
                                  path=c.get("path", "/"))
            except Exception:
                pass
        return s

    def _browser_fetch_many(self, items, referer, tok) -> dict:
        """Download many images at once inside the browser (uses its DNS/cookies)."""
        self.start()
        if not self.page.url.startswith(referer):
            self.goto(referer, tok)
        got = {}
        for k in range(0, len(items), 6):
            if tok.is_set():
                raise Cancelled()
            chunk = items[k:k + 6]
            try:
                res = self.page.evaluate(JS_FETCH_MANY, [u for _i, u in chunk])
            except Exception as e:
                self.log(f"  browser batch failed: {e}")
                continue
            for (i, u), r in zip(chunk, res):
                if r and r.get("d"):
                    data = base64.b64decode(r["d"])
                    if sniff_ext(data):
                        got[i] = (u, data)
        return got

    def _browser_get(self, url: str, referer: str, tok) -> bytes:
        self.start()
        if self.img_page is None or self.img_page.is_closed():
            self.img_page = self.ctx.new_page()
            if not self.st.get("show_browser"):
                self._set_window(self.img_page, False)
        p = self.img_page
        resp = p.goto(url, referer=referer, wait_until="load", timeout=90000)
        if self.is_challenge(p):
            p = self.handle_challenge(p, tok, is_main=False)
            resp = p.goto(url, referer=referer, wait_until="load", timeout=90000)
        if resp is None or resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status if resp else '?'}")
        data = resp.body()
        if not sniff_ext(data):
            raise RuntimeError("response was not an image")
        return data

    def download(self, gal: Gallery, folder: Path, progress, tok) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        n = len(gal.pages)
        width = max(3, len(str(n)))
        have = {p.stem for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in IMG_EXTS and p.stat().st_size > 0}
        todo = [(i, c) for i, c in enumerate(gal.pages, 1) if f"{i:0{width}d}" not in have]
        done = n - len(todo)
        progress(done, n)
        if not todo:
            return
        retries = int(self.st.get("retries", 3))
        delay = float(self.st.get("delay", 0))
        hosts = {urlparse(u).hostname for _i, c in todo for u in c if urlparse(u).hostname}
        sess = gal.session or self._http_session(gal.referer, resolve=self._resolve(hosts))
        lock = threading.Lock()
        state = {"pref": None, "low": 0}
        t0 = time.time()

        def save(i: int, u: str, data: bytes) -> None:
            ext = sniff_ext(data) or (os.path.splitext(urlparse(u).path)[1] or ".jpg")
            dest = folder / f"{i:0{width}d}{ext}"
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(data)
            tmp.replace(dest)

        def ordered(cands: list[str]) -> list[tuple[int, str]]:
            idx = list(range(len(cands)))
            p = state["pref"]
            if p is not None and p < len(cands):
                idx.remove(p)
                idx.insert(0, p)
            return [(k, cands[k]) for k in idx]

        def grab(i: int, cands: list[str]) -> None:
            last = None
            for k, u in ordered(cands):
                tries = retries + 1 if k == state["pref"] or state["pref"] is None else 1
                for attempt in range(tries):
                    if tok.is_set():
                        raise Cancelled()
                    try:
                        r = sess.get(u, timeout=60)
                        if r.status_code == 200 and sniff_ext(r.content):
                            save(i, u, r.content)
                            with lock:
                                if state["pref"] is None:
                                    state["pref"] = k
                                if k >= gal.low_from:
                                    state["low"] += 1
                            if delay:
                                time.sleep(delay)
                            return
                        last = f"HTTP {r.status_code}"
                        if r.status_code in (403, 404, 410, 503):
                            break       # wrong address / blocked -> next candidate
                    except Exception as e:
                        last = str(e).split(". See https://curl")[0]
                        if re.search(r"resolve|connect to|Connection refused|SSL|certificate",
                                     last, re.I):
                            break       # network problem, retrying won't help
                    time.sleep(0.7 * (attempt + 1))
            raise RuntimeError(last or "failed")

        failed: list[tuple[int, list[str]]] = []

        def run(batch):
            nonlocal done
            with ThreadPoolExecutor(max_workers=max(1, int(self.st.get("threads", 8)))) as ex:
                futs = {ex.submit(grab, i, c): (i, c) for i, c in batch}
                for f in as_completed(futs):
                    try:
                        f.result()
                        done += 1
                    except Cancelled:
                        pass
                    except Exception as e:
                        failed.append(futs[f])
                        self.log(f"  page {futs[f][0]}: {e}")
                    progress(done, n)

        run(todo[:1])            # first page alone: learns which address works
        if failed and len(todo) > 1:
            # direct download doesn't work on this PC/site right now -> use the browser
            self.log("Direct download failed - switching to the browser for this gallery.")
            failed.extend(todo[1:])
        else:
            run(todo[1:])
        if tok.is_set():
            raise Cancelled()

        if failed:
            self.log(f"Fetching {len(failed)} page(s) through the browser...")
            pending = sorted(failed)
            for level in range(max(len(c) for _i, c in pending)):
                items = [(i, ordered(c)[level][1]) for i, c in pending if level < len(c)]
                got = self._browser_fetch_many(items, gal.referer, tok)
                for i, (u, data) in got.items():
                    save(i, u, data)
                    done += 1
                    cands = dict(pending)[i]
                    if cands.index(u) >= gal.low_from:
                        state["low"] += 1
                progress(done, n)
                pending = [(i, c) for i, c in pending if i not in got]
                if not pending:
                    break
            still = []
            for i, cands in pending:      # last resort: open each image in a tab
                ok = False
                for _k, u in ordered(cands):
                    try:
                        save(i, u, self._browser_get(u, gal.referer, tok))
                        done += 1
                        progress(done, n)
                        ok = True
                        break
                    except Cancelled:
                        raise
                    except Exception as e:
                        last = e
                if not ok:
                    still.append(i)
                    self.log(f"  page {i}: {last}")
            if still:
                raise RuntimeError(f"{len(still)} page(s) failed - press Start to retry them.")
        if state["low"]:
            self.log(f"Warning: {state['low']} page(s) were only available in preview quality.")
        self.log(f"Downloaded {n} pages in {time.time() - t0:.1f}s")


# --------------------------------------------------------------------------- #
# Worker thread
# --------------------------------------------------------------------------- #
class _Token:
    def __init__(self, worker: "Worker", gen: int):
        self.w, self.g = worker, gen

    def is_set(self) -> bool:
        return self.w.gen != self.g or self.w.quitting


class Worker(QThread):
    status = Signal(str, str)           # uid, text
    progress = Signal(str, int, int)    # uid, done, total
    meta = Signal(str, str, int)        # uid, title, pages
    cover = Signal(str, str, str)       # uid, image url, referer
    children = Signal(str, list)        # uid, gallery urls found on a list page
    finished_item = Signal(str, str)    # uid, folder
    failed_item = Signal(str, str)      # uid, error
    log = Signal(str)
    attention = Signal(str)

    IDLE_CLOSE = 120  # seconds before an idle browser is shut down

    def __init__(self, settings: dict):
        super().__init__()
        self.settings = settings
        self.jobs: queue.Queue = queue.Queue()
        self.manual_done = threading.Event()
        self.gen = 0
        self.quitting = False

    def enqueue(self, uid: str, url: str) -> None:
        self.jobs.put((self.gen, uid, url))

    def command(self, cmd: str) -> None:
        self.jobs.put((self.gen, "__cmd__", cmd))

    def stop_all(self) -> list[str]:
        self.gen += 1
        dropped = []
        while True:
            try:
                uid = self.jobs.get_nowait()[1]
            except queue.Empty:
                return dropped
            if uid and uid != "__cmd__":
                dropped.append(uid)

    def shutdown(self) -> None:
        self.quitting = True
        self.jobs.put((None, None, None))

    def run(self) -> None:
        sess = BrowserSession(self.settings, self.log.emit, self.attention.emit,
                              self.manual_done)
        idle_since = time.time()
        while not self.quitting:
            try:
                gen, uid, url = self.jobs.get(timeout=1)
            except queue.Empty:
                if sess.browser and time.time() - idle_since > self.IDLE_CLOSE:
                    self.log.emit("Idle - closing browser.")
                    sess.close()
                continue
            if uid is None:
                break
            if uid == "__cmd__":
                try:
                    if url in ("show", "hide"):
                        if url == "show":
                            sess.start()
                        sess.show_window(url == "show")
                except Exception as e:
                    self.log.emit(f"Browser: {e}")
                continue
            tok = _Token(self, gen)
            if tok.is_set():
                self.status.emit(uid, "Stopped")
                continue
            for attempt in (1, 2):
                try:
                    self._process(sess, uid, url, tok)
                    break
                except Cancelled:
                    self.status.emit(uid, "Stopped")
                    break
                except Exception as e:
                    if attempt == 1 and is_closed_error(e) and not tok.is_set():
                        self.log.emit("Browser tab closed unexpectedly - retrying this gallery...")
                        sess.close(kill=False)
                        continue
                    self.failed_item.emit(uid, str(e))
                    self.log.emit(f"ERROR {url}: {e}")
                    if not isinstance(e, RuntimeError):
                        self.log.emit(traceback.format_exc(limit=3))
                    if not sess.alive:
                        sess.close()
                    break
            idle_since = time.time()
        sess.close()

    def _process(self, sess, uid, url, tok):
        self.status.emit(uid, "Opening...")
        gal = sess.fetch_gallery(url, tok)
        if gal.children and not gal.pages:
            self.children.emit(uid, gal.children)
            return
        self.meta.emit(uid, gal.title, len(gal.images))
        if gal.cover:
            self.cover.emit(uid, gal.cover, gal.referer)
        out_dir = Path(self.settings["output_dir"])
        folder = out_dir / fit_name(gal.title, out_dir, reserve=15)
        self.status.emit(uid, "Downloading")
        sess.download(gal, folder, lambda d, t, uid=uid: self.progress.emit(uid, d, t), tok)
        self.finished_item.emit(uid, str(folder))
        self.log.emit(f"Saved: {folder}")


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
COLORS = [("Red", "#e5484d"), ("Orange", "#f76b15"), ("Yellow", "#ffc53d"),
          ("Green", "#30a46c"), ("Teal", "#12a594"), ("Blue", "#3e63dd"),
          ("Purple", "#8e4ec6"), ("Pink", "#d6409f"), ("Gray", "#8b8d98")]
COL_TITLE, COL_PAGES, COL_PROGRESS, COL_STATUS, COL_URL = range(5)
HEADERS = ["Title", "Pages", "Progress", "Status", "URL / Contents"]
ROLE_UID = Qt.UserRole
ROLE_FRAC = Qt.UserRole + 1
BUSY = ("Waiting", "Opening...", "Downloading")

DARK_QSS = """
QWidget { background: #1e1f22; color: #dfe1e5; font-size: 10pt; }
QMainWindow, QDialog { background: #1e1f22; }
QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox {
    background: #2b2d30; border: 1px solid #3c3f44; border-radius: 6px; padding: 5px 7px;
    selection-background-color: #3e63dd; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #3e63dd; }
QLineEdit#search { border-radius: 14px; padding-left: 10px; }
QPushButton { background: #2f3136; border: 1px solid #43464c; border-radius: 6px;
    padding: 6px 12px; }
QPushButton:hover { background: #393c42; }
QPushButton:pressed, QPushButton:checked { background: #26282c; }
QPushButton:disabled { color: #6b6e75; border-color: #34363b; }
QPushButton#primary { background: #3e63dd; border-color: #3e63dd; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #5174e6; }
QPushButton#danger:hover { background: #5c2328; border-color: #e5484d; }
QPushButton#cbz { border-color: #8e4ec6; }
QPushButton#auto:checked { background: #1d4d33; border-color: #30a46c; color: #8ff0b8; font-weight: 600; }
QComboBox { background: #2b2d30; border: 1px solid #3c3f44; border-radius: 6px; padding: 4px 8px; }
QComboBox QAbstractItemView { background: #2b2d30; selection-background-color: #3e63dd; }
QLabel#thumbpop { background: #18191b; border: 1px solid #4a4d55; border-radius: 6px; padding: 4px; }
QPushButton#cbz:hover { background: #3a2a4f; }
QWidget#topbar { background: #18191b; border-bottom: 1px solid #2c2e32; }
QTreeWidget, QListWidget { background: #232427; alternate-background-color: #26282b;
    border: 1px solid #34363b; border-radius: 6px; outline: 0; }
QHeaderView::section { background: #2b2d30; color: #a9acb3; border: none;
    border-right: 1px solid #34363b; padding: 6px; font-weight: 600; }
QMenu { background: #2b2d30; border: 1px solid #43464c; padding: 4px; }
QMenu::item { padding: 6px 22px; border-radius: 4px; }
QMenu::item:selected { background: #3e63dd; }
QMenu::item:disabled { color: #6b6e75; }
QMenu::separator { height: 1px; background: #43464c; margin: 4px 8px; }
QScrollBar:vertical { background: transparent; width: 11px; }
QScrollBar::handle:vertical { background: #45484e; border-radius: 5px; min-height: 30px; }
QScrollBar:horizontal { background: transparent; height: 11px; }
QScrollBar::handle:horizontal { background: #45484e; border-radius: 5px; min-width: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QCheckBox::indicator { width: 16px; height: 16px; }
QStatusBar { background: #18191b; color: #a9acb3; }
QLabel#banner { background: #4a3b10; color: #ffd772; border: 1px solid #8a6d1a;
    border-radius: 6px; padding: 7px 10px; font-weight: 600; }
QLabel#hint { color: #8b8d98; background: transparent; }
QSplitter::handle { background: #1e1f22; }
QToolTip { background: #2b2d30; color: #dfe1e5; border: 1px solid #43464c; }
QProgressDialog QLabel { padding: 6px; }
"""


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def app_icon() -> QIcon:
    p = resource_path("icon.ico")
    if p.exists():
        return QIcon(str(p))
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    qp = QPainter(pm)
    qp.setRenderHint(QPainter.Antialiasing)
    qp.setBrush(QColor("#c2408f"))
    qp.setPen(Qt.NoPen)
    qp.drawRoundedRect(2, 2, 60, 60, 14, 14)
    qp.setPen(QColor("white"))
    f = qp.font()
    f.setBold(True)
    f.setPixelSize(42)
    qp.setFont(f)
    qp.drawText(pm.rect(), Qt.AlignCenter, "P")
    qp.end()
    return QIcon(pm)


def apply_dark(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    for role, col in [
        (QPalette.Window, "#1e1f22"), (QPalette.WindowText, "#dfe1e5"),
        (QPalette.Base, "#232427"), (QPalette.AlternateBase, "#26282b"),
        (QPalette.ToolTipBase, "#2b2d30"), (QPalette.ToolTipText, "#dfe1e5"),
        (QPalette.Text, "#dfe1e5"), (QPalette.Button, "#2f3136"),
        (QPalette.ButtonText, "#dfe1e5"), (QPalette.Highlight, "#2e4a8f"),
        (QPalette.HighlightedText, "#ffffff"), (QPalette.Link, "#7c9cff"),
        (QPalette.PlaceholderText, "#7d8089")]:
        pal.setColor(role, QColor(col))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#6b6e75"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#6b6e75"))
    app.setPalette(pal)
    app.setStyleSheet(DARK_QSS)


def dark_title_bar(widget: QWidget) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass


def color_icon(hex_color: str | None) -> QIcon:
    pm = QPixmap(14, 14)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    if hex_color:
        p.setBrush(QColor(hex_color))
        p.setPen(Qt.NoPen)
    else:
        p.setBrush(Qt.NoBrush)
        p.setPen(QColor("#8b8d98"))
    p.drawEllipse(1, 1, 12, 12)
    p.end()
    return QIcon(pm)


def cbz_badge(color: str | None) -> QIcon:
    pm = QPixmap(30, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color or "#8e4ec6"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(0, 1, 30, 14, 4, 4)
    p.setPen(QColor("white"))
    f = p.font()
    f.setPixelSize(9)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "CBZ")
    p.end()
    return QIcon(pm)


def open_in_explorer(path: str) -> None:
    p = Path(path)
    if sys.platform == "win32":
        if p.is_file():
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            os.startfile(str(p))  # type: ignore[attr-defined]
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p if p.is_dir() else p.parent)))


# --------------------------------------------------------------------------- #
# CBZ helpers
# --------------------------------------------------------------------------- #
def folder_images(folder: str | Path) -> list[Path]:
    try:
        return sorted(p for p in Path(folder).iterdir()
                      if p.is_file() and p.suffix.lower() in IMG_EXTS)
    except Exception:
        return []


def zip_members(zpath: str, prefix: str) -> list[str]:
    with zipfile.ZipFile(zpath) as z:
        return sorted(n for n in z.namelist()
                      if n.startswith(prefix + "/") and n.lower().endswith(IMG_EXTS))


def first_page_bytes(entry: dict) -> bytes | None:
    try:
        src = entry["src"]
        if src[0] == "folder":
            imgs = folder_images(src[1])
            return imgs[0].read_bytes() if imgs else None
        names = zip_members(src[1], src[2])
        if names:
            with zipfile.ZipFile(src[1]) as z:
                return z.read(names[0])
    except Exception:
        pass
    return None


def thumb_icon(data: bytes | None, w: int = 104, h: int = 148) -> QIcon:
    canvas = QPixmap(w, h)
    canvas.fill(QColor("#2b2d30"))
    p = QPainter(canvas)
    img = QImage.fromData(data) if data else QImage()
    if not img.isNull():
        sc = img.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p.drawImage((w - sc.width()) // 2, (h - sc.height()) // 2, sc)
    else:
        p.setPen(QColor("#7d8089"))
        p.drawText(canvas.rect(), Qt.AlignCenter, "no\npreview")
    p.end()
    return QIcon(canvas)


def cbz_folder_names(titles: list[str]) -> list[str]:
    width = 2 if len(titles) < 100 else 3
    out = []
    for i, t in enumerate(titles, 1):
        out.append(f"{i:0{width}d} - " + fit_name(t, limit=110))
    return out


class CbzJob(QThread):
    """Builds / rebuilds a CBZ in the background.
    entries: ordered list of {"title", "src": ["folder", path] | ["cbz", zip, prefix]}
    extract: list of (zip, prefix, dest_folder) to unpack BEFORE rebuilding."""
    progress = Signal(int, int)
    done = Signal(str, list)   # error text ("" = ok), new prefixes

    def __init__(self, entries, dest, extract=(), remove_after=None):
        super().__init__()
        self.entries, self.dest = entries, Path(dest)
        self.extract, self.remove_after = list(extract), remove_after

    def run(self):
        try:
            for zpath, prefix, folder in self.extract:
                Path(folder).mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(zpath) as z:
                    for n in zip_members(zpath, prefix):
                        (Path(folder) / n.split("/", 1)[1]).write_bytes(z.read(n))
            prefixes = cbz_folder_names([e["title"] for e in self.entries])
            plan = []
            for e, pre in zip(self.entries, prefixes):
                if e["src"][0] == "folder":
                    plan += [("f", str(p), f"{pre}/{p.name}") for p in folder_images(e["src"][1])]
                else:
                    plan += [("z", (e["src"][1], n), f"{pre}/{n.split('/', 1)[1]}")
                             for n in zip_members(e["src"][1], e["src"][2])]
            tmp = self.dest.with_name(self.dest.name + ".tmp")
            opened: dict[str, zipfile.ZipFile] = {}
            try:
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as out:
                    for k, (kind, src, arc) in enumerate(plan, 1):
                        if kind == "f":
                            out.write(src, arc)
                        else:
                            zp, name = src
                            if zp not in opened:
                                opened[zp] = zipfile.ZipFile(zp)
                            out.writestr(arc, opened[zp].read(name))
                        if k % 10 == 0 or k == len(plan):
                            self.progress.emit(k, len(plan))
            finally:
                for z in opened.values():
                    z.close()
            os.replace(tmp, self.dest)
            if self.remove_after and Path(self.remove_after) != self.dest:
                try:
                    Path(self.remove_after).unlink()
                except Exception:
                    pass
            self.done.emit("", prefixes)
        except Exception as e:
            try:
                self.dest.with_name(self.dest.name + ".tmp").unlink()
            except Exception:
                pass
            self.done.emit(str(e), [])


def color_name(hx: str | None) -> str:
    return next((n for n, c in COLORS if c.lower() == (hx or "").lower()), hx or "no color")


class ColorCbzDialog(QDialog):
    """One CBZ per colour group: name each one."""

    def __init__(self, parent, groups: list[tuple[str, list[dict]]], out_dir: str):
        super().__init__(parent)
        self.setWindowTitle("One CBZ per color")
        self.setMinimumWidth(640)
        lay = QVBoxLayout(self)
        hint = QLabel("Each color becomes one CBZ, with the galleries in their current list "
                      "order (01, 02, 03 ...). Downloads without a color each become their "
                      "own CBZ. Edit the names if you like.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        form = QFormLayout()
        self.names: list[QLineEdit] = []
        for color, ents in groups:
            pages = sum(int(e["pages"] or 0) for e in ents)
            default = ents[0]["title"] + (f" + {len(ents) - 1} more" if len(ents) > 1 else "")
            ed = QLineEdit(default)
            ed.setToolTip("\n".join(f"{i:02d}  {e['title']}" for i, e in enumerate(ents, 1)))
            lbl = QLabel((f"{color_name(color)}  ({len(ents)} galleries, {pages} pages)"
                          if color else f"No color - on its own  ({pages} pages)"))
            row = QHBoxLayout()
            ic = QLabel()
            ic.setPixmap(color_icon(color).pixmap(14, 14))
            row.addWidget(ic)
            row.addWidget(lbl)
            row.addStretch(1)
            w = QWidget()
            w.setLayout(row)
            form.addRow(w, ed)
            self.names.append(ed)
        lay.addLayout(form)
        self.out = QLineEdit(out_dir)
        b = QPushButton("Browse...")
        b.clicked.connect(lambda: self.out.setText(
            QFileDialog.getExistingDirectory(self, "Save CBZ files in", self.out.text())
            or self.out.text()))
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Save in"))
        r2.addWidget(self.out, 1)
        r2.addWidget(b)
        lay.addLayout(r2)
        self.delete_cb = QCheckBox("Delete the original download folders after the CBZ files "
                                   "are created (permanent)")
        lay.addWidget(self.delete_cb)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = btns.button(QDialogButtonBox.Ok)
        ok.setText(f"Create {len(groups)} CBZ file{'s' if len(groups) > 1 else ''}")
        ok.setObjectName("primary")
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _accept(self):
        for ed in self.names:
            if not ed.text().strip():
                ed.setFocus()
                return
        if self.delete_cb.isChecked() and QMessageBox.question(
                self, APP_NAME, "The original download folders will be permanently deleted "
                "after the CBZ files are created. Continue?") != QMessageBox.Yes:
            return
        self.accept()


class PickDownloadsDialog(QDialog):
    def __init__(self, entries: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add downloads to the CBZ")
        self.resize(560, 460)
        lay = QVBoxLayout(self)
        lbl = QLabel("Select one or more finished downloads:")
        lay.addWidget(lbl)
        self.filter = QLineEdit(placeholderText="Filter...")
        lay.addWidget(self.filter)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.entries = entries
        for e in entries:
            it = QListWidgetItem(f"{e['title']}   ({e['pages']} pages)")
            it.setToolTip(e["title"])
            self.list.addItem(it)
        lay.addWidget(self.list, 1)
        self.filter.textChanged.connect(self._filter)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Add")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.list.itemDoubleClicked.connect(lambda _i: self.accept())

    def _filter(self, t):
        t = t.lower()
        for i, e in enumerate(self.entries):
            self.list.item(i).setHidden(bool(t) and t not in e["title"].lower())

    def chosen(self) -> list[dict]:
        return [self.entries[self.list.row(it)] for it in self.list.selectedItems()]


class CbzDialog(QDialog):
    """Arrange galleries inside a CBZ: first page + full name of each, reorder by
    drag & drop or buttons, remove, add more downloads."""

    def __init__(self, parent, entries: list[dict], name: str, out_dir: str,
                 available, editing: bool = False):
        super().__init__(parent)
        self.available, self.editing = available, editing
        self.setWindowTitle("Edit CBZ" if editing else "Create CBZ")
        self.resize(820, 640)
        lay = QVBoxLayout(self)

        form = QFormLayout()
        self.name = QLineEdit(name)
        form.addRow("CBZ name", self.name)
        self.out = QLineEdit(out_dir)
        b = QPushButton("Browse...")
        b.clicked.connect(lambda: self.out.setText(
            QFileDialog.getExistingDirectory(self, "Save CBZ in", self.out.text())
            or self.out.text()))
        row = QHBoxLayout()
        row.addWidget(self.out, 1)
        row.addWidget(b)
        form.addRow("Save in", row)
        lay.addLayout(form)

        hint = QLabel("Drag the galleries (or use Up / Down) to set their order. Inside the "
                      "CBZ each one becomes a numbered folder: 01 - Title, 02 - Title ...")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        body = QHBoxLayout()
        self.list = QListWidget()
        self.list.setIconSize(QSize(104, 148))
        self.list.setSpacing(4)
        self.list.setWordWrap(True)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.model().rowsMoved.connect(lambda *a: self.renumber())
        body.addWidget(self.list, 1)
        side = QVBoxLayout()
        for text, slot in [("Move up", lambda: self.move(-1)),
                           ("Move down", lambda: self.move(1)),
                           ("Move to top", lambda: self.move(-10**6)),
                           ("Move to bottom", lambda: self.move(10**6)),
                           (None, None),
                           ("Remove", self.remove),
                           ("Add download...", self.add)]:
            if text is None:
                side.addSpacing(14)
                continue
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            if text == "Remove":
                btn.setObjectName("danger")
            side.addWidget(btn)
        side.addStretch(1)
        body.addLayout(side)
        lay.addLayout(body, 1)

        self.delete_cb = QCheckBox("Delete the original download folders after the CBZ "
                                   "is created (permanent)")
        if not editing:
            lay.addWidget(self.delete_cb)
        self.summary = QLabel()
        self.summary.setObjectName("hint")
        lay.addWidget(self.summary)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_btn = btns.button(QDialogButtonBox.Ok)
        self.ok_btn.setText("Save CBZ" if editing else "Create CBZ")
        self.ok_btn.setObjectName("primary")
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        QShortcut(QKeySequence.Delete, self.list, activated=self.remove)
        for e in entries:
            self._add_item(e)
        self.renumber()

    def _add_item(self, e: dict) -> None:
        it = QListWidgetItem(thumb_icon(first_page_bytes(e)), "")
        it.setData(Qt.UserRole, e)
        it.setToolTip(e["title"])
        it.setFlags(it.flags() | Qt.ItemIsDragEnabled)
        self.list.addItem(it)

    def entries(self) -> list[dict]:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]

    def renumber(self) -> None:
        ents = self.entries()
        names = cbz_folder_names([e["title"] for e in ents])
        for i, (e, nm) in enumerate(zip(ents, names)):
            num, _sep, _rest = nm.partition(" - ")
            self.list.item(i).setText(f"{num}   {e['title']}\n      {e['pages']} pages")
        total = sum(int(e.get("pages") or 0) for e in ents)
        self.summary.setText(f"{len(ents)} galleries, {total} pages in this CBZ")
        self.ok_btn.setEnabled(bool(ents))

    def move(self, delta: int) -> None:
        rows = sorted(self.list.row(it) for it in self.list.selectedItems())
        if not rows:
            return
        n = self.list.count()
        items = [self.list.takeItem(r - k) for k, r in enumerate(rows)]
        anchor = max(0, min(n - len(items), rows[0] + delta))
        for k, it in enumerate(items):
            self.list.insertItem(anchor + k, it)
            it.setSelected(True)
        self.list.scrollToItem(items[0])
        self.renumber()

    def remove(self) -> None:
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))
        self.renumber()

    def add(self) -> None:
        have = {e.get("key") for e in self.entries()}
        options = [e for e in self.available() if e.get("key") not in have]
        if not options:
            QMessageBox.information(self, APP_NAME, "There are no other finished downloads "
                                    "to add.")
            return
        dlg = PickDownloadsDialog(options, self)
        dark_title_bar(dlg)
        if dlg.exec():
            for e in dlg.chosen():
                self._add_item(e)
            self.renumber()

    def _accept(self) -> None:
        if not self.name.text().strip():
            self.name.setFocus()
            return
        if not self.editing and self.delete_cb.isChecked():
            if QMessageBox.question(
                    self, APP_NAME, "The original download folders will be permanently "
                    "deleted after the CBZ is created. Continue?") != QMessageBox.Yes:
                return
        self.accept()


# --------------------------------------------------------------------------- #
# Quick title lookup (nhentai API, no browser) so new rows show their real name
# --------------------------------------------------------------------------- #
class TitleFetcher(QThread):
    got = Signal(str, str, int, str)

    def __init__(self, items: list[tuple[str, str]]):
        super().__init__()
        self.items = items

    def run(self):
        try:
            from curl_cffi import requests as creq
            s = creq.Session(impersonate="chrome")
        except Exception:
            return
        for uid, url in self.items:
            m = re.search(r"/g/(\d+)", url)
            if not m:
                continue
            for api in (f"https://nhentai.net/api/v2/galleries/{m.group(1)}",
                        f"https://nhentai.net/api/gallery/{m.group(1)}"):
                try:
                    r = s.get(api, timeout=15)
                    if r.status_code != 200:
                        continue
                    d = r.json()
                    t = d.get("title") or {}
                    title = (t.get("english") or t.get("pretty") or t.get("japanese")) \
                        if isinstance(t, dict) else str(t)
                    plist = d.get("pages") or (d.get("images") or {}).get("pages") or []
                    cover = ""
                    if plist and isinstance(plist[0], dict) and plist[0].get("path"):
                        cover = "https://i1.nhentai.net/" + plist[0]["path"].lstrip("/")
                    elif d.get("media_id") and plist:
                        ext = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}.get(
                            plist[0].get("t"), "jpg")
                        cover = f"https://i1.nhentai.net/galleries/{d['media_id']}/1.{ext}"
                    if title:
                        self.got.emit(uid, title, len(plist), cover)
                        break
                except Exception:
                    continue


class ThumbLoader(QThread):
    """Loads hover thumbnails off the UI thread (local folder / CBZ / web)."""
    loaded = Signal(str, QImage)

    def __init__(self):
        super().__init__()
        self.q: queue.Queue = queue.Queue()
        self._http = None

    def request(self, key: str, kind: str, src, referer: str = "") -> None:
        self.q.put((key, kind, src, referer))

    def _bytes(self, kind, src, referer) -> bytes | None:
        if kind == "folder":
            imgs = folder_images(src)
            return imgs[0].read_bytes() if imgs else None
        if kind == "cbz":
            return first_page_bytes({"src": ["cbz", src[0], src[1]]})
        if kind == "url":
            if self._http is None:
                from curl_cffi import requests as creq
                self._http = creq.Session(impersonate="chrome")
            r = self._http.get(src, headers={"Referer": referer or src}, timeout=20)
            return r.content if r.status_code == 200 and sniff_ext(r.content) else None
        return None

    def run(self):
        while True:
            key, kind, src, referer = self.q.get()
            if key is None:
                return
            img = QImage()
            try:
                data = self._bytes(kind, src, referer)
                if data:
                    buf = QBuffer()
                    buf.setData(QByteArray(data))
                    buf.open(QBuffer.ReadOnly)
                    rd = QImageReader(buf)
                    sz = rd.size()
                    if sz.isValid() and sz.height() > 700:   # decode small = fast
                        rd.setScaledSize(sz.scaled(10_000, 700, Qt.KeepAspectRatio))
                    img = rd.read()
            except Exception:
                img = QImage()
            self.loaded.emit(key, img)


class ThumbPopup(QLabel):
    """Floating preview at the far right of the hovered row, kept on-screen."""

    def __init__(self):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setObjectName("thumbpop")
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAlignment(Qt.AlignCenter)

    def show_image(self, img: QImage, height: int, row: QRect, cursor: QPoint) -> None:
        sc = img.scaled(int(height * 1.6), height, Qt.KeepAspectRatio,
                        Qt.SmoothTransformation)
        self.setPixmap(QPixmap.fromImage(sc))
        self.adjustSize()
        w, h = self.width(), self.height()
        screen = QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()

        def clamp(x, y):
            return (max(geo.left(), min(x, geo.right() - w + 1)),
                    max(geo.top(), min(y, geo.bottom() - h + 1)))
        x, y = clamp(row.right() - w - 6, row.center().y() - h // 2)
        if QRect(x, y, w, h).adjusted(-6, -6, 6, 6).contains(cursor):
            # would sit under the mouse -> put it beside the cursor instead
            x, y = clamp(cursor.x() - w - 24, row.center().y() - h // 2)
            if QRect(x, y, w, h).contains(cursor):
                x, y = clamp(cursor.x() + 24, row.center().y() - h // 2)
        self.move(x, y)
        self.show()
        self.raise_()


class SitesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Supported sites")
        self.resize(560, 620)
        lay = QVBoxLayout(self)
        info = QLabel("<b>Built in:</b> hentairead.com, nhentai.net<br>"
                      "<b>Through gallery-dl</b> (list below) - galleries, chapters and "
                      "also artist / tag / search pages, which are expanded into their "
                      "galleries.<br><b>Any other site:</b> the page is opened in the "
                      "browser and its large images are collected.")
        info.setWordWrap(True)
        lay.addWidget(info)
        flt = QLineEdit(placeholderText="Filter...")
        lay.addWidget(flt)
        lst = QListWidget()
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.TypeWriter)
        lst.setFont(mono)
        lay.addWidget(lst, 1)
        self.filter = flt
        rows = {}
        try:
            from gallery_dl import extractor as gex
            for cls in gex.extractors():
                root = getattr(cls, "root", "") or ""
                host = urlparse(root).hostname or ""
                rows.setdefault(cls.category, host)
        except Exception as e:
            rows = {f"gallery-dl not available: {e}": ""}
        for cat in sorted(rows):
            lst.addItem(f"{cat:<22} {rows[cat]}")
        cnt = QLabel(f"{len(rows)} site modules")
        cnt.setObjectName("hint")
        lay.addWidget(cnt)
        flt.textChanged.connect(lambda t: [lst.item(i).setHidden(
            t.lower() not in lst.item(i).text().lower()) for i in range(lst.count())])
        btn = QDialogButtonBox(QDialogButtonBox.Close)
        btn.rejected.connect(self.reject)
        lay.addWidget(btn)


# --------------------------------------------------------------------------- #
# Widgets
# --------------------------------------------------------------------------- #
class RowDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        sz = super().sizeHint(option, index)
        return QSize(sz.width(), max(sz.height(), 30))


class ProgressDelegate(RowDelegate):
    def paint(self, painter, option, index):
        frac = index.data(ROLE_FRAC)
        if frac is None:
            return super().paint(painter, option, index)
        self.initStyleOption(option, index)
        text = option.text
        option.text = ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, option, painter, option.widget)
        bar = QStyleOptionProgressBar()
        bar.rect = option.rect.adjusted(4, 5, -4, -5)
        bar.minimum, bar.maximum = 0, 1000
        bar.progress = int(max(0.0, min(1.0, frac)) * 1000)
        bar.text = text
        bar.textVisible = True
        bar.textAlignment = Qt.AlignCenter
        bar.palette = option.palette
        bar.palette.setColor(QPalette.Highlight, QColor("#30a46c" if frac >= 1 else "#3e63dd"))
        bar.palette.setColor(QPalette.Base, QColor("#2b2d30"))
        style.drawControl(QStyle.CE_ProgressBar, bar, painter, option.widget)


class QueueTree(QTreeWidget):
    """Left-drag: move files out to Explorer.  Right-drag: rearrange rows.
    Right-click without dragging: context menu."""
    urlsDropped = Signal(list)
    dragFinished = Signal(list)       # uids whose files were dragged out
    reordered = Signal()
    dragStarted = Signal()

    def __init__(self, path_for_uid, parent=None):
        super().__init__(parent)
        self.path_for_uid = path_for_uid
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setAllColumnsShowFocus(True)
        self.setContextMenuPolicy(Qt.DefaultContextMenu)   # menu opened on right-release
        self._rdrag = None
        self._scroll_timer = QTimer(self, interval=40)
        self._scroll_timer.timeout.connect(self._autoscroll)

    # ---- right-button drag to rearrange ------------------------------------ #
    def _visible_items(self) -> list:
        return [self.topLevelItem(i) for i in range(self.topLevelItemCount())
                if not self.topLevelItem(i).isHidden()]

    def _target(self, pos):
        """(insert index among ALL rows, y of the indicator line)"""
        vis = self._visible_items()
        if not vis:
            return 0, 0
        for it in vis:
            r = self.visualItemRect(it)
            if pos.y() < r.center().y():
                return self.indexOfTopLevelItem(it), r.top()
        last = vis[-1]
        return self.indexOfTopLevelItem(last) + 1, self.visualItemRect(last).bottom() + 1

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            pos = e.position().toPoint()
            it = self.itemAt(pos)
            if it is not None and not it.isSelected():
                self.clearSelection()
                it.setSelected(True)
            if it is not None:
                self.setCurrentItem(it, 0, QItemSelectionModel.NoUpdate)
            self._rdrag = {"start": pos, "active": False, "pos": pos}
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._rdrag is not None and (e.buttons() & Qt.RightButton):
            pos = e.position().toPoint()
            d = self._rdrag
            if not d["active"] and self.selectedItems() and \
                    (pos - d["start"]).manhattanLength() >= QApplication.startDragDistance():
                d["active"] = True
                self.dragStarted.emit()
                self.viewport().setCursor(Qt.SizeVerCursor)
                self._scroll_timer.start()
            d["pos"] = pos
            if d["active"]:
                self.viewport().update()
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.RightButton and self._rdrag is not None:
            d, self._rdrag = self._rdrag, None
            self._scroll_timer.stop()
            self.viewport().unsetCursor()
            self.viewport().update()
            if d["active"]:
                self.move_selected_to(self._target(e.position().toPoint())[0])
            else:
                self.customContextMenuRequested.emit(e.position().toPoint())
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        if e.reason() == QContextMenuEvent.Keyboard:   # Menu key / Shift+F10
            it = self.currentItem()
            pos = self.visualItemRect(it).center() if it else QPoint(10, 10)
            self.customContextMenuRequested.emit(pos)
        e.accept()                                      # mouse: handled on release

    def _autoscroll(self):
        if not self._rdrag or not self._rdrag["active"]:
            return
        y, h = self._rdrag["pos"].y(), self.viewport().height()
        sb = self.verticalScrollBar()
        if y < 28:
            sb.setValue(sb.value() - max(1, (28 - y) // 4))
        elif y > h - 28:
            sb.setValue(sb.value() + max(1, (y - h + 28) // 4))
        self.viewport().update()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._rdrag and self._rdrag["active"]:
            _idx, y = self._target(self._rdrag["pos"])
            p = QPainter(self.viewport())
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#7c9cff"))
            w = self.viewport().width()
            p.drawRect(4, y - 1, w - 8, 3)
            p.drawEllipse(QPoint(6, y), 4, 4)
            p.end()

    def move_selected_to(self, index: int) -> None:
        sel = [it for it in (self.topLevelItem(i) for i in range(self.topLevelItemCount()))
               if it.isSelected()]
        if not sel:
            return
        before = sum(1 for it in sel if self.indexOfTopLevelItem(it) < index)
        for it in sel:
            self.takeTopLevelItem(self.indexOfTopLevelItem(it))
        index = max(0, min(index - before, self.topLevelItemCount()))
        for k, it in enumerate(sel):
            self.insertTopLevelItem(index + k, it)
            it.setSelected(True)
        self.scrollToItem(sel[0])
        self.reordered.emit()

    def selected_uids(self) -> list[str]:
        return [it.data(COL_TITLE, ROLE_UID) for it in self.selectedItems()
                if not it.isHidden()]

    # drag OUT to Windows Explorer: MOVES the downloaded folders / CBZ files
    def startDrag(self, actions):
        pairs = [(u, self.path_for_uid(u)) for u in self.selected_uids()]
        pairs = [(u, p) for u, p in pairs if p and Path(p).exists()]
        if not pairs:
            return
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(os.path.normpath(p)) for _u, p in pairs])
        effect = struct.pack("<I", 2)  # DROPEFFECT_MOVE
        md.setData('application/x-qt-windows-mime;value="Preferred DropEffect"', effect)
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.exec(Qt.MoveAction | Qt.CopyAction, Qt.MoveAction)
        self.dragFinished.emit([u for u, _p in pairs])

    # drop IN: links dragged from a browser / text
    def _accepts(self, e) -> bool:
        md = e.mimeData()
        return e.source() is not self and (md.hasUrls() or md.hasText())

    def dragEnterEvent(self, e):
        e.acceptProposedAction() if self._accepts(e) else e.ignore()

    def dragMoveEvent(self, e):
        e.acceptProposedAction() if self._accepts(e) else e.ignore()

    def dropEvent(self, e):
        if not self._accepts(e):
            return e.ignore()
        md = e.mimeData()
        text = "\n".join(u.toString() for u in md.urls()) + "\n" + (md.text() or "")
        self.urlsDropped.emit(extract_urls(text))
        e.acceptProposedAction()


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self.st = settings
        f = QFormLayout(self)
        f.setVerticalSpacing(10)
        self.out = QLineEdit(settings["output_dir"])
        b = QPushButton("Browse...")
        b.clicked.connect(lambda: self.out.setText(
            QFileDialog.getExistingDirectory(self, "Output folder", self.out.text())
            or self.out.text()))
        row = QHBoxLayout()
        row.addWidget(self.out)
        row.addWidget(b)
        f.addRow("Download folder", row)
        self.threads = QSpinBox(minimum=1, maximum=16, value=int(settings["threads"]))
        f.addRow("Parallel pages", self.threads)
        self.retries = QSpinBox(minimum=0, maximum=10, value=int(settings["retries"]))
        f.addRow("Retries per page", self.retries)
        self.delay = QDoubleSpinBox(minimum=0, maximum=10, singleStep=0.1, decimals=1,
                                    value=float(settings["delay"]))
        f.addRow("Delay per page (s)", self.delay)
        self.browser = QLineEdit(settings.get("browser_path", ""))
        self.browser.setPlaceholderText("Auto-detect Microsoft Edge / Chrome")
        bb = QPushButton("Browse...")
        bb.clicked.connect(lambda: self.browser.setText(
            QFileDialog.getOpenFileName(self, "Browser", "", "Programs (*.exe)")[0]
            or self.browser.text()))
        row2 = QHBoxLayout()
        row2.addWidget(self.browser)
        row2.addWidget(bb)
        f.addRow("Browser", row2)
        self.show_browser = QCheckBox("Keep the browser window visible (troubleshooting)")
        self.show_browser.setChecked(bool(settings.get("show_browser")))
        f.addRow("", self.show_browser)
        hint = QLabel("Downloads go through your own Edge/Chrome so Cloudflare checks can be "
                      "passed. The browser stays off-screen and only appears if a check "
                      "needs a click. Changes to browser options apply after it restarts.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        f.addRow(hint)
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        f.addRow(btns)

    def values(self) -> dict:
        return {"output_dir": self.out.text().strip() or DEFAULT_SETTINGS["output_dir"],
                "threads": self.threads.value(), "retries": self.retries.value(),
                "delay": self.delay.value(), "browser_path": self.browser.text().strip(),
                "show_browser": self.show_browser.isChecked()}


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {VERSION}")
        self.setWindowIcon(app_icon())
        self.resize(1220, 700)
        self.settings = load_settings()
        self.items: dict[str, dict] = {}      # uid -> record
        self.rows: dict[str, QTreeWidgetItem] = {}
        self.by_url: dict[str, str] = {}      # normalized url -> uid (row or CBZ)
        self._last_clip = ""
        self._threads: list[QThread] = []
        self.thumbs: dict[str, QImage] = {}
        self._hover_uid = None
        self._hover_timer = QTimer(self, singleShot=True, interval=220)
        self._hover_timer.timeout.connect(self._show_hover)
        self.popup = ThumbPopup()
        self.thumb_loader = ThumbLoader()
        self.thumb_loader.loaded.connect(self._thumb_loaded)
        self.thumb_loader.start()
        self._save_timer = QTimer(self, singleShot=True, interval=800)
        self._save_timer.timeout.connect(self.save_queue)

        self.worker = Worker(self.settings)
        self.worker.status.connect(self.on_status)
        self.worker.progress.connect(self.on_progress)
        self.worker.meta.connect(self.on_meta)
        self.worker.cover.connect(self.on_cover)
        self.worker.children.connect(self.on_children)
        self.worker.finished_item.connect(self.on_done)
        self.worker.failed_item.connect(self.on_failed)
        self.worker.log.connect(self.log)
        self.worker.attention.connect(self.on_attention)
        self.worker.start()

        self._build_ui()
        self.load_queue()
        self.set_clipboard_watch(bool(self.settings.get("watch_clipboard")))
        if self.settings.get("autostart"):
            QTimer.singleShot(1200, lambda: self.start(None, quiet=True))

    # ------------------------------------------------------------------ UI --
    def _btn(self, text, slot, tip="", name="", shortcut=None):
        b = QPushButton(text)
        b.setToolTip(tip + (f"  ({shortcut})" if shortcut else ""))
        if name:
            b.setObjectName(name)
        b.clicked.connect(slot)
        return b

    def _build_ui(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- top bar with all options ---------------------------------------
        topbar = QWidget()
        topbar.setObjectName("topbar")
        tb = QHBoxLayout(topbar)
        tb.setContentsMargins(12, 8, 12, 8)
        tb.setSpacing(6)
        tb.addWidget(self._btn("Start", lambda: self.start(None),
                               "Download everything not finished yet", "primary"))
        self.auto_btn = QPushButton("Auto-start")
        self.auto_btn.setObjectName("auto")
        self.auto_btn.setCheckable(True)
        self.auto_btn.setChecked(bool(self.settings.get("autostart")))
        self.auto_btn.setToolTip("When on, new links start downloading as soon as they are "
                                 "added, and unfinished downloads resume when the app opens")
        self.auto_btn.toggled.connect(self.set_autostart)
        tb.addWidget(self.auto_btn)
        tb.addWidget(self._btn("Stop", self.stop, "Stop all downloads"))
        tb.addWidget(self._btn("Redownload", lambda: self.redownload(self.tree.selected_uids()),
                               "Delete the selected galleries' pages and download them again",
                               shortcut="Ctrl+R"))
        tb.addWidget(self._btn("Make CBZ", lambda: self.make_cbz(self.tree.selected_uids()),
                               "Put the selected downloads into one CBZ", "cbz",
                               shortcut="Ctrl+B"))
        tb.addWidget(self._btn("CBZ by color", lambda: self.cbz_by_color(self.tree.selected_uids()),
                               "One CBZ per color from the selected downloads (same-color "
                               "rows must be next to each other)", "cbz",
                               shortcut="Ctrl+Shift+B"))
        tb.addWidget(self._btn("Remove", self.remove_selected, "Remove from the list", "danger",
                               shortcut="Del"))
        tb.addWidget(self._btn("Clear finished", self.clear_done,
                               "Remove finished galleries from the list"))
        tip = QLabel("Right-drag rows to reorder")
        tip.setObjectName("hint")
        tb.addWidget(self._btn("Open folder", self.open_output, "Open the download folder"))
        tb.addStretch(1)
        tb.addWidget(tip)
        tb.addSpacing(8)
        self.thumb_cb = QCheckBox("Thumbnails")
        self.thumb_cb.setToolTip("Show a preview when the mouse is over a download")
        self.thumb_cb.setChecked(bool(self.settings.get("thumb_on", True)))
        self.thumb_cb.toggled.connect(self.set_thumb_on)
        tb.addWidget(self.thumb_cb)
        self.thumb_size = QComboBox()
        for label, px in (("Small", 140), ("Medium", 220), ("Large", 320), ("Huge", 460)):
            self.thumb_size.addItem(label, px)
        cur = int(self.settings.get("thumb_size", 220))
        self.thumb_size.setCurrentIndex(max(0, [140, 220, 320, 460].index(cur)
                                            if cur in (140, 220, 320, 460) else 1))
        self.thumb_size.setToolTip("Preview size")
        self.thumb_size.currentIndexChanged.connect(self.set_thumb_size)
        self.thumb_size.setEnabled(self.thumb_cb.isChecked())
        tb.addWidget(self.thumb_size)
        tb.addSpacing(8)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(self.apply_filter)
        tb.addWidget(self.search)
        self.browser_btn = QPushButton("Show browser")
        self.browser_btn.setCheckable(True)
        self.browser_btn.setToolTip("Bring the hidden Edge window on screen / hide it again")
        self.browser_btn.toggled.connect(self.toggle_browser)
        tb.addWidget(self.browser_btn)
        tb.addWidget(self._btn("Sites", self.show_sites, "List of supported sites"))
        tb.addWidget(self._btn("Settings", self.open_settings))
        outer.addWidget(topbar)

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(12, 10, 12, 6)
        lay.setSpacing(8)

        # --- url bar --------------------------------------------------------
        top = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "Paste gallery links from any site and press Enter...")
        self.url_edit.returnPressed.connect(self.add_from_edit)
        top.addWidget(self.url_edit, 1)
        top.addWidget(self._btn("Add", self.add_from_edit, "Add the URLs typed in the box"))
        top.addWidget(self._btn("Paste", self.paste_clipboard,
                                "Add every URL on the clipboard", shortcut="Ctrl+V"))
        self.watch_cb = QCheckBox("Watch clipboard")
        self.watch_cb.setToolTip("Automatically add supported URLs whenever you copy them")
        self.watch_cb.toggled.connect(self.set_clipboard_watch)
        top.addWidget(self.watch_cb)
        lay.addLayout(top)

        self.banner_box = QWidget()
        bl = QHBoxLayout(self.banner_box)
        bl.setContentsMargins(0, 0, 0, 0)
        self.banner = QLabel()
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        bl.addWidget(self.banner, 1)
        self.banner_btn = QPushButton("Continue")
        self.banner_btn.setObjectName("primary")
        self.banner_btn.setToolTip("Continue now (normally not needed - the program notices "
                                   "by itself when the check has passed)")
        self.banner_btn.clicked.connect(self.worker.manual_done.set)
        bl.addWidget(self.banner_btn)
        self.banner_box.hide()
        lay.addWidget(self.banner_box)

        self.tree = QueueTree(lambda uid: self.items.get(uid, {}).get("path"))
        self.tree.setHeaderLabels(HEADERS)
        self.tree.setIconSize(QSize(30, 16))
        self.tree.setItemDelegate(RowDelegate(self.tree))
        self.tree.setItemDelegateForColumn(COL_PROGRESS, ProgressDelegate(self.tree))
        h = self.tree.header()
        h.setSectionResizeMode(COL_TITLE, QHeaderView.Stretch)
        for c, w in [(COL_PAGES, 60), (COL_PROGRESS, 170), (COL_STATUS, 120), (COL_URL, 330)]:
            h.setSectionResizeMode(c, QHeaderView.Interactive)
            self.tree.setColumnWidth(c, w)
        h.setStretchLastSection(False)
        self.tree.customContextMenuRequested.connect(self.context_menu)
        self.tree.itemDoubleClicked.connect(
            lambda it, _c: self.open_item(it.data(COL_TITLE, ROLE_UID)))
        self.tree.urlsDropped.connect(lambda urls: self.add_urls(urls, "drop"))
        self.tree.dragFinished.connect(self.after_drag)
        self.tree.reordered.connect(lambda: (self.schedule_save(),
                                             self.flash("Order changed.", 2500)))
        self.tree.itemSelectionChanged.connect(self.update_counts)
        self.tree.setMouseTracking(True)
        self.tree.viewport().installEventFilter(self)
        self.tree.verticalScrollBar().valueChanged.connect(lambda _v: self._hide_hover())
        self.tree.dragStarted.connect(self._hide_hover)

        self.log_box = QPlainTextEdit(readOnly=True)
        self.log_box.setMaximumBlockCount(3000)
        self.log_box.setPlaceholderText("Log")
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.tree)
        split.addWidget(self.log_box)
        split.setSizes([500, 130])
        lay.addWidget(split, 1)
        outer.addWidget(body, 1)
        self.setCentralWidget(central)

        self.count_lbl = QLabel()
        self.statusBar().addPermanentWidget(self.count_lbl)

        QShortcut(QKeySequence.Find, self, activated=self.focus_search)
        QShortcut(QKeySequence("Escape"), self.search, activated=self.clear_search)
        QShortcut(QKeySequence.Paste, self.tree, activated=self.paste_clipboard)
        QShortcut(QKeySequence.Delete, self.tree, activated=self.remove_selected)
        QShortcut(QKeySequence("Ctrl+R"), self,
                  activated=lambda: self.redownload(self.tree.selected_uids()))
        QShortcut(QKeySequence("Ctrl+B"), self,
                  activated=lambda: self.make_cbz(self.tree.selected_uids()))
        QShortcut(QKeySequence("Ctrl+Shift+B"), self,
                  activated=lambda: self.cbz_by_color(self.tree.selected_uids()))
        QShortcut(QKeySequence("Return"), self.tree,
                  activated=lambda: [self.open_item(u) for u in self.tree.selected_uids()[:5]])
        for i, (_name, hx) in enumerate(COLORS[:9], 1):
            QShortcut(QKeySequence(f"Ctrl+{i}"), self.tree,
                      activated=lambda hx=hx: self.set_color(self.tree.selected_uids(), hx))
        QShortcut(QKeySequence("Ctrl+0"), self.tree,
                  activated=lambda: self.set_color(self.tree.selected_uids(), None))

    def showEvent(self, e):
        super().showEvent(e)
        dark_title_bar(self)

    # ------------------------------------------------------------- helpers --
    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.log_box.appendPlainText(line)
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2_000_000:
                LOG_FILE.replace(LOG_FILE.with_suffix(".old.txt"))
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d ") + line + "\n")
        except Exception:
            pass

    def flash(self, msg: str, ms: int = 6000) -> None:
        self.statusBar().showMessage(msg, ms)

    def schedule_save(self):
        self._save_timer.start()

    def ordered_uids(self) -> list[str]:
        return [self.tree.topLevelItem(i).data(COL_TITLE, ROLE_UID)
                for i in range(self.tree.topLevelItemCount())]

    def update_counts(self):
        recs = list(self.items.values())
        done = sum(1 for r in recs if r["status"] == "Done")
        cbz = sum(1 for r in recs if r.get("kind") == "cbz")
        sel = len(self.tree.selectedItems())
        shown = sum(1 for it in self.rows.values() if not it.isHidden())
        txt = f"{len(recs)} items  |  {done} downloaded"
        if cbz:
            txt += f"  |  {cbz} CBZ"
        if shown != len(recs):
            txt += f"  |  {shown} match search"
        if sel:
            txt += f"  |  {sel} selected"
        self.count_lbl.setText(txt)

    # ---------------------------------------------------------- auto-start --
    def set_autostart(self, on: bool):
        self.settings["autostart"] = on
        save_settings(self.settings)
        if on:
            self.start(None, quiet=True)
            self.flash("Auto-start on: new links download immediately.")
        else:
            self.flash("Auto-start off.")

    # ---------------------------------------------------------- thumbnails --
    def set_thumb_on(self, on: bool):
        self.settings["thumb_on"] = on
        self.thumb_size.setEnabled(on)
        save_settings(self.settings)
        if not on:
            self._hide_hover()

    def set_thumb_size(self, _i):
        self.settings["thumb_size"] = int(self.thumb_size.currentData())
        save_settings(self.settings)

    def _thumb_source(self, uid):
        r = self.items.get(uid)
        if not r:
            return None
        if r.get("kind") == "cbz" and r.get("members") and Path(r["path"]).exists():
            return f"cbz|{r['path']}|{r['members'][0]['prefix']}", "cbz", \
                (r["path"], r["members"][0]["prefix"]), ""
        if r.get("path") and Path(r["path"]).is_dir():
            return f"dir|{r['path']}", "folder", r["path"], ""
        if r.get("cover"):
            return f"url|{r['cover']}", "url", r["cover"], r.get("cover_ref") or r["url"]
        return None

    def eventFilter(self, obj, ev):
        if obj is self.tree.viewport():
            t = ev.type()
            if t == QEvent.MouseMove and not (ev.buttons() & Qt.RightButton):
                it = self.tree.itemAt(ev.position().toPoint())
                uid = it.data(COL_TITLE, ROLE_UID) if it else None
                if uid != self._hover_uid:
                    self._hover_uid = uid
                    self.popup.hide()
                    if uid and self.thumb_cb.isChecked():
                        self._hover_timer.start()
            elif t in (QEvent.Leave, QEvent.MouseButtonPress, QEvent.Wheel):
                self._hide_hover()
        return super().eventFilter(obj, ev)

    def _hide_hover(self):
        self._hover_timer.stop()
        self._hover_uid = None
        self.popup.hide()

    def _show_hover(self):
        uid = self._hover_uid
        src = self._thumb_source(uid) if uid else None
        if not src:
            return
        key = src[0]
        if key in self.thumbs:
            self._place_popup(uid, self.thumbs[key])
        else:
            self.thumbs[key] = QImage()          # mark as loading
            self.thumb_loader.request(key, *src[1:])

    def _thumb_loaded(self, key, img):
        self.thumbs[key] = img
        if len(self.thumbs) > 300:               # keep memory in check
            for k in list(self.thumbs)[:100]:
                self.thumbs.pop(k, None)
        uid = self._hover_uid
        src = self._thumb_source(uid) if uid else None
        if src and src[0] == key:
            self._place_popup(uid, img)

    def _place_popup(self, uid, img: QImage):
        if img.isNull() or uid not in self.rows or not self.thumb_cb.isChecked():
            return
        vp = self.tree.viewport()
        r = self.tree.visualItemRect(self.rows[uid])
        r.setLeft(0)
        r.setRight(vp.width() - 1)
        row = QRect(vp.mapToGlobal(r.topLeft()), r.size())
        self.popup.show_image(img, int(self.settings.get("thumb_size", 220)), row,
                              QCursor.pos())

    def on_cover(self, uid, url, referer):
        r = self.items.get(uid)
        if r:
            r["cover"], r["cover_ref"] = url, referer
            self.schedule_save()

    def on_children(self, uid, urls):
        r = self.items.get(uid)
        if r:
            r.update(status="List", title=r.get("title") or f"List: {title_from_url(r['url'])}",
                     error=f"{len(urls)} galleries were added from this page")
            self.refresh_row(uid)
        self.log(f"Expanded list page into {len(urls)} galleries")
        self.add_urls(urls[:500], "list")

    def show_sites(self):
        dlg = SitesDialog(self)
        dark_title_bar(dlg)
        dlg.exec()

    # -------------------------------------------------------------- search --
    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def clear_search(self):
        self.search.clear()
        self.tree.setFocus()

    def apply_filter(self, text: str = None):
        words = (self.search.text() if text is None else text).lower().split()
        for uid, it in self.rows.items():
            r = self.items[uid]
            hay = " ".join([r.get("title") or "", title_from_url(r.get("url") or ""),
                            r.get("url") or "", r.get("status") or ""]
                           + [m.get("title", "") for m in r.get("members", [])]).lower()
            it.setHidden(bool(words) and not all(w in hay for w in words))
        self.update_counts()

    # ----------------------------------------------------------- queue I/O --
    def load_queue(self):
        try:
            data = json.loads(QUEUE_FILE.read_text("utf-8"))
        except Exception:
            data = []
        need_titles = []
        for rec in data:
            if rec.get("status") in BUSY:
                rec["status"] = "Queued"
            rec.pop("dups", None)
            uid = self._insert(rec)
            if not rec.get("title") and "nhentai.net" in (rec.get("url") or ""):
                need_titles.append((uid, rec["url"]))
        self.fetch_titles(need_titles)
        self.update_counts()

    def save_queue(self):
        data = [self.items[u] for u in self.ordered_uids() if u in self.items]
        try:
            tmp = QUEUE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), "utf-8")
            tmp.replace(QUEUE_FILE)
        except Exception as e:
            self.log(f"Could not save queue: {e}")

    # -------------------------------------------------------------- adding --
    def _insert(self, rec: dict, index: int | None = None) -> str:
        rec.setdefault("uid", uuid.uuid4().hex)
        for k, v in (("kind", "gallery"), ("title", ""), ("pages", 0), ("done", 0),
                     ("status", "Queued"), ("color", None), ("path", ""), ("error", ""),
                     ("url", "")):
            rec.setdefault(k, v)
        uid = rec["uid"]
        it = QTreeWidgetItem()
        it.setData(COL_TITLE, ROLE_UID, uid)
        it.setFlags(it.flags() | Qt.ItemIsDragEnabled)
        self.items[uid] = rec
        self.rows[uid] = it
        if rec["kind"] == "cbz":
            for m in rec.get("members", []):
                if m.get("url"):
                    self.by_url[normalize_url(m["url"])] = uid
        elif rec["url"]:
            self.by_url[normalize_url(rec["url"])] = uid
        if index is None:
            self.tree.addTopLevelItem(it)
        else:
            self.tree.insertTopLevelItem(index, it)
        self.refresh_row(uid)
        if self.search.text():
            self.apply_filter()
        self.update_counts()
        return uid

    def _drop_row(self, uid: str) -> dict:
        it = self.rows.pop(uid)
        self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(it))
        rec = self.items.pop(uid)
        keys = [m.get("url") for m in rec.get("members", [])] + [rec.get("url")]
        for k in keys:
            if k and self.by_url.get(normalize_url(k)) == uid:
                self.by_url.pop(normalize_url(k), None)
        return rec

    def add_urls(self, urls: list[str], source: str = "") -> None:
        added, dup, unsup = [], [], 0
        for u in urls:
            if not is_supported(u):
                if source != "clipboard":
                    unsup += 1
                    self.log(f"Not a supported site, skipped: {u}")
                continue
            key = normalize_url(u)
            if key in self.by_url:
                uid = self.by_url[key]
                if uid not in dup:
                    dup.append(uid)
                rec = self.items[uid]
                where = f"inside CBZ '{rec['title']}'" if rec.get("kind") == "cbz" \
                    else f"status: {rec['status']}"
                self.log(f"Duplicate URL (already in the list, {where}): {key}")
                continue
            added.append((self._insert({"url": key}), key))
        if dup:  # show the duplicates: select + scroll to them
            self.tree.clearSelection()
            for uid in dup:
                self.rows[uid].setHidden(False)
                self.rows[uid].setSelected(True)
            self.tree.scrollToItem(self.rows[dup[-1]])
            QApplication.alert(self)
        self.fetch_titles([(uid, k) for uid, k in added if "nhentai.net" in k])
        if added and self.settings.get("autostart"):
            self.start([uid for uid, _k in added], quiet=True)
        parts = []
        if added:
            parts.append(f"Added {len(added)}")
        if dup:
            parts.append(f"{len(dup)} already in the list (highlighted)")
        if unsup:
            parts.append(f"{unsup} unsupported")
        if parts:
            self.flash(" | ".join(parts))
        elif source in ("paste", "edit", "drop"):
            self.flash("No URLs found.")
        self.update_counts()
        self.schedule_save()

    def fetch_titles(self, items: list[tuple[str, str]]):
        if not items:
            return
        t = TitleFetcher(items)
        t.got.connect(self.on_quick_title)
        t.finished.connect(lambda t=t: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t)
        t.start()

    def on_quick_title(self, uid, title, pages, cover=""):
        r = self.items.get(uid)
        if r and cover and not r.get("cover"):
            r["cover"], r["cover_ref"] = cover, "https://nhentai.net/"
        if r and not r.get("title"):
            r["title"] = title
            if pages and not r.get("pages"):
                r["pages"] = pages
            self.refresh_row(uid)
            self.schedule_save()

    def add_from_edit(self):
        self.add_urls(extract_urls(self.url_edit.text()), "edit")
        self.url_edit.clear()

    def paste_clipboard(self):
        self.add_urls(extract_urls(QGuiApplication.clipboard().text()), "paste")

    def set_clipboard_watch(self, on: bool):
        cb = QGuiApplication.clipboard()
        if getattr(self, "_clip_connected", False):
            cb.dataChanged.disconnect(self.on_clipboard)
            self._clip_connected = False
        if on:
            self._clip_connected = True
            self._last_clip = cb.text()
            cb.dataChanged.connect(self.on_clipboard)
        if self.watch_cb.isChecked() != on:
            self.watch_cb.setChecked(on)
        self.settings["watch_clipboard"] = on
        save_settings(self.settings)

    def on_clipboard(self):
        text = QGuiApplication.clipboard().text()
        if not text or text == self._last_clip:
            return
        self._last_clip = text
        urls = [u for u in extract_urls(text) if is_known_site(u)]
        if urls:
            self.add_urls(urls, "clipboard")

    # ------------------------------------------------------------ display --
    def refresh_row(self, uid: str) -> None:
        rec, it = self.items[uid], self.rows[uid]
        is_cbz = rec.get("kind") == "cbz"
        it.setText(COL_TITLE, rec["title"] or title_from_url(rec["url"]))
        it.setForeground(COL_TITLE, QBrush(QColor("#dfe1e5" if rec["title"] else "#9a9da5")))
        f = it.font(COL_TITLE)
        f.setItalic(not rec["title"])
        f.setBold(is_cbz)
        it.setFont(COL_TITLE, f)
        it.setText(COL_PAGES, str(rec["pages"] or ""))
        it.setTextAlignment(COL_PAGES, Qt.AlignCenter)
        if rec["pages"] and not is_cbz:
            it.setData(COL_PROGRESS, ROLE_FRAC, rec["done"] / rec["pages"])
            it.setText(COL_PROGRESS, f"{rec['done']}/{rec['pages']}")
        else:
            it.setData(COL_PROGRESS, ROLE_FRAC, None)
            it.setText(COL_PROGRESS, f"{len(rec.get('members', []))} galleries" if is_cbz else "")
        st = rec["status"]
        it.setText(COL_STATUS, st)
        st_col = {"Done": "#3dd68c", "Error": "#ff6369", "Stopped": "#ffc53d",
                  "CBZ": "#d19dff", "Moved": "#ffc53d", "List": "#7cd5ff"}.get(
            st, "#7c9cff" if st in BUSY else "#a9acb3")
        it.setForeground(COL_STATUS, QBrush(QColor(st_col)))
        it.setToolTip(COL_STATUS, rec.get("error") or st)
        if is_cbz:
            names = [f"{i}. {m['title']}" for i, m in enumerate(rec.get("members", []), 1)]
            it.setText(COL_URL, "  |  ".join(names))
            it.setToolTip(COL_URL, "\n".join(names))
        else:
            it.setText(COL_URL, rec["url"])
            it.setToolTip(COL_URL, rec["url"])
        it.setForeground(COL_URL, QBrush(QColor("#8b8d98")))
        it.setToolTip(COL_TITLE, (rec["title"] or rec["url"]) +
                      (f"\n{rec['path']}" if rec.get("path") else ""))
        color = rec.get("color")
        bg = QColor(color) if color else None
        if bg:
            bg.setAlpha(85)
        if is_cbz:
            it.setIcon(COL_TITLE, cbz_badge(color))
        else:
            it.setIcon(COL_TITLE, color_icon(color) if color else QIcon())
        for c in range(len(HEADERS)):
            it.setBackground(c, QBrush(bg) if bg else QBrush())

    # ------------------------------------------------------ worker signals --
    def on_status(self, uid, st):
        if uid in self.items:
            self.items[uid]["status"] = st
            self.refresh_row(uid)
            self.update_counts()
            self.schedule_save()

    def on_progress(self, uid, done, total):
        if uid in self.items:
            self.items[uid].update(done=done, pages=total)
            self.refresh_row(uid)

    def on_meta(self, uid, title, pages):
        if uid in self.items:
            self.items[uid].update(title=title, pages=pages)
            self.refresh_row(uid)
            self.schedule_save()

    def on_done(self, uid, path):
        if uid in self.items:
            r = self.items[uid]
            r.update(status="Done", path=path, done=r["pages"], error="")
            self.refresh_row(uid)
            self.update_counts()
            self.schedule_save()

    def on_failed(self, uid, err):
        if uid in self.items:
            self.items[uid].update(status="Error", error=err)
            self.refresh_row(uid)
            self.update_counts()
            self.schedule_save()

    def on_attention(self, msg):
        if msg:
            manual = msg.startswith("!manual:")
            self.banner.setText("\u26a0  " + (msg[8:] if manual else msg))
            self.banner_btn.setVisible(manual)
            self.banner_box.show()
            QApplication.alert(self)
        else:
            self.banner_box.hide()

    # ------------------------------------------------------------ actions --
    def start(self, uids: list[str] | None, quiet: bool = False):
        if uids is None:
            uids = [u for u in self.ordered_uids()
                    if self.items[u]["status"] not in ("Done", "List", "Moved")]
        n = 0
        for uid in uids:
            rec = self.items[uid]
            if rec.get("kind") == "cbz" or rec["status"] in BUSY:
                continue
            rec["status"], rec["error"] = "Waiting", ""
            self.refresh_row(uid)
            self.worker.enqueue(uid, rec["url"])
            n += 1
        if n or not quiet:
            self.flash(f"Queued {n} item(s)." if n else "Nothing to download.")
        self.schedule_save()

    def redownload(self, uids: list[str]):
        uids = [u for u in uids if self.items[u].get("kind") != "cbz"
                and self.items[u]["status"] not in BUSY]
        if not uids:
            self.flash("Select one or more downloads (not CBZs) to redownload.")
            return
        if QMessageBox.question(
                self, APP_NAME, f"Delete the downloaded pages of {len(uids)} gallery(s) "
                "and download them again?") != QMessageBox.Yes:
            return
        for u in uids:
            r = self.items[u]
            p = r.get("path")
            if p and Path(p).is_dir():
                for f in Path(p).iterdir():
                    if f.is_file() and (f.suffix.lower() in IMG_EXTS or f.name.endswith(".part")):
                        try:
                            f.unlink()
                        except Exception as e:
                            self.log(f"Could not delete {f}: {e}")
            r.update(status="Queued", done=0, error="")
            self.refresh_row(u)
        self.start(uids)

    def stop(self):
        for uid in self.worker.stop_all():
            self.on_status(uid, "Stopped")
        for uid, r in self.items.items():
            if r["status"] == "Waiting":
                self.on_status(uid, "Stopped")
        self.flash("Stopping...")

    def toggle_browser(self, on: bool):
        self.browser_btn.setText("Hide browser" if on else "Show browser")
        self.worker.command("show" if on else "hide")
        if any(r["status"] in ("Opening...", "Downloading") for r in self.items.values()):
            self.flash("The browser window will move once the current gallery is finished.")

    def remove_selected(self):
        uids = self.tree.selected_uids()
        busy = [u for u in uids if self.items[u]["status"] in ("Opening...", "Downloading")]
        if busy:
            self.flash("Stop the download before removing an item that is in progress.")
            uids = [u for u in uids if u not in busy]
        for uid in uids:
            self._drop_row(uid)
        self.update_counts()
        self.schedule_save()

    def clear_done(self):
        self.tree.clearSelection()
        for uid, r in self.items.items():
            if r["status"] == "Done" and r.get("kind") != "cbz":
                self.rows[uid].setSelected(True)
        self.remove_selected()

    def set_color(self, uids: list[str], color: str | None):
        for u in uids:
            self.items[u]["color"] = color
            self.refresh_row(u)
        self.schedule_save()

    def select_color(self, color):
        self.tree.clearSelection()
        for u, r in self.items.items():
            if r.get("color") == color:
                self.rows[u].setSelected(True)

    def open_item(self, uid: str):
        p = self.items.get(uid, {}).get("path")
        if p and Path(p).exists():
            open_in_explorer(p)
        else:
            self.flash("Not downloaded yet." if not p else
                       "The folder/file is no longer there (moved or deleted).")

    def open_output(self):
        sel = [self.items[u].get("path") for u in self.tree.selected_uids()]
        sel = [p for p in sel if p and Path(p).exists()]
        if sel:
            open_in_explorer(sel[0])
            return
        p = Path(self.settings["output_dir"])
        p.mkdir(parents=True, exist_ok=True)
        open_in_explorer(str(p))

    def after_drag(self, uids: list[str]):
        moved = 0
        for u in uids:
            r = self.items.get(u)
            if r and r.get("path") and not Path(r["path"]).exists():
                r.update(status="Moved", path="", error="Moved out of the app by drag & drop")
                self.refresh_row(u)
                moved += 1
        if moved:
            self.flash(f"Moved {moved} item(s) to the new location.")
            self.schedule_save()

    def context_menu(self, pos):
        uids = self.tree.selected_uids()
        m = QMenu(self)
        if uids:
            recs = [self.items[u] for u in uids]
            gal = [u for u in uids if self.items[u].get("kind") != "cbz"]
            done_gal = [u for u in gal if self.items[u]["status"] == "Done"]
            cbzs = [u for u in uids if self.items[u].get("kind") == "cbz"]
            if gal:
                m.addAction(f"Download selected ({len(gal)})", lambda: self.start(gal))
                m.addAction("Check for missing pages", lambda: self._requeue(gal))
                m.addAction("Redownload (replace files)\tCtrl+R", lambda: self.redownload(gal))
                m.addAction("Stop all", self.stop)
                m.addSeparator()
            a = m.addAction(f"Add to CBZ ({len(done_gal)})...\tCtrl+B",
                            lambda: self.make_cbz(done_gal))
            a.setEnabled(bool(done_gal))
            ncol = len({self.items[u].get("color") for u in gal if self.items[u].get("color")})
            a2 = m.addAction("One CBZ per color (no color = its own CBZ)...\tCtrl+Shift+B",
                             lambda: self.cbz_by_color(gal))
            a2.setEnabled(bool(done_gal))
            if len(cbzs) == 1:
                m.addAction("Edit CBZ (order / add / remove)...",
                            lambda: self.edit_cbz(cbzs[0]))
            m.addSeparator()
            m.addAction("Open folder\tEnter", lambda: [self.open_item(u) for u in uids[:10]])
            if any(r.get("url") for r in recs):
                m.addAction("Open in browser", lambda: [QDesktopServices.openUrl(
                    QUrl(self.items[u]["url"])) for u in uids[:10] if self.items[u]["url"]])
            m.addAction("Copy URL(s)", lambda: QGuiApplication.clipboard().setText(
                "\n".join(self._urls_of(uids))))
            m.addAction("Copy title(s)", lambda: QGuiApplication.clipboard().setText(
                "\n".join(self.items[u]["title"] or title_from_url(self.items[u]["url"])
                          for u in uids)))
            m.addSeparator()
            cm = m.addMenu("Color")
            for i, (name, hx) in enumerate(COLORS, 1):
                act = cm.addAction(color_icon(hx), f"{name}\tCtrl+{i}")
                act.triggered.connect(lambda _=False, hx=hx: self.set_color(uids, hx))
            cm.addAction("Custom...", lambda: self._custom_color(uids))
            cm.addSeparator()
            cm.addAction(color_icon(None), "No color\tCtrl+0",
                         lambda: self.set_color(uids, None))
            col = recs[0].get("color")
            if col:
                m.addAction(color_icon(col), "Select all with this color",
                            lambda: self.select_color(col))
            m.addSeparator()
            m.addAction("Remove from list\tDel", self.remove_selected)
        m.addAction("Paste URLs\tCtrl+V", self.paste_clipboard)
        m.addAction("Select all\tCtrl+A", self.tree.selectAll)
        m.addAction("Search\tCtrl+F", self.focus_search)
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _urls_of(self, uids):
        out = []
        for u in uids:
            r = self.items[u]
            out += [mm["url"] for mm in r.get("members", []) if mm.get("url")] or \
                ([r["url"]] if r.get("url") else [])
        return out

    def _requeue(self, uids):
        for u in uids:
            if self.items[u]["status"] not in BUSY:
                self.items[u]["status"] = "Queued"
        self.start(uids)

    def _custom_color(self, uids):
        c = QColorDialog.getColor(QColor("#3e63dd"), self, "Row color")
        if c.isValid():
            self.set_color(uids, c.name())

    # ---------------------------------------------------------------- CBZ --
    def _entry_from_row(self, uid: str) -> dict:
        r = self.items[uid]
        return {"key": "row:" + uid, "uid": uid, "title": r["title"] or title_from_url(r["url"]),
                "url": r["url"], "pages": r["pages"], "color": r.get("color"),
                "folder": r["path"], "src": ["folder", r["path"]]}

    def _available_rows(self, exclude=()) -> list[dict]:
        return [self._entry_from_row(u) for u in self.ordered_uids()
                if u not in exclude and self.items[u].get("kind") != "cbz"
                and self.items[u]["status"] == "Done" and self.items[u].get("path")
                and Path(self.items[u]["path"]).is_dir()]

    def _run_cbz_job(self, job: CbzJob, label: str, on_ok):
        dlg = QProgressDialog(label, None, 0, 100, self)
        dlg.setWindowTitle(APP_NAME)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)
        job.progress.connect(lambda k, n: dlg.setValue(int(k * 100 / max(1, n))))

        def finished(err, prefixes):
            dlg.close()
            self._threads.remove(job)
            if err:
                self.log(f"CBZ failed: {err}")
                QMessageBox.warning(self, APP_NAME, f"Could not write the CBZ:\n{err}")
            else:
                on_ok(prefixes)
        job.done.connect(finished)
        self._threads.append(job)
        job.start()

    def make_cbz(self, uids: list[str]):
        uids = [u for u in uids if self.items[u].get("kind") != "cbz"]
        ok = [u for u in uids if self.items[u]["status"] == "Done"
              and self.items[u].get("path") and Path(self.items[u]["path"]).is_dir()]
        if not ok:
            self.flash("Select one or more finished downloads first.")
            return
        if len(ok) < len(uids):
            self.flash(f"{len(uids) - len(ok)} selected item(s) are not downloaded yet "
                       "and were left out.")
        entries = [self._entry_from_row(u) for u in ok]
        name = entries[0]["title"] if len(entries) == 1 else \
            f"{entries[0]['title']} + {len(entries) - 1} more"
        dlg = CbzDialog(self, entries, name, self.settings["output_dir"],
                        available=lambda: self._available_rows())
        dark_title_bar(dlg)
        if not dlg.exec():
            return
        entries = dlg.entries()
        out_dir = Path(dlg.out.text().strip() or self.settings["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / (fit_name(dlg.name.text(), out_dir, reserve=8) + ".cbz")
        k = 2
        while dest.exists():
            dest = out_dir / (fit_name(f"{dlg.name.text()} ({k})", out_dir, reserve=8) + ".cbz")
            k += 1
        delete = dlg.delete_cb.isChecked()

        def ok_cb(prefixes):
            first_idx = min((self.tree.indexOfTopLevelItem(self.rows[e["uid"]])
                             for e in entries if e["uid"] in self.rows), default=None)
            members = []
            for e, pre in zip(entries, prefixes):
                members.append({"title": e["title"], "url": e["url"], "pages": e["pages"],
                                "prefix": pre, "folder": e["folder"], "color": e.get("color")})
                if e["uid"] in self.rows:
                    self._drop_row(e["uid"])
                if delete and e["folder"] and Path(e["folder"]).is_dir():
                    try:
                        shutil.rmtree(e["folder"])
                    except Exception as ex:
                        self.log(f"Could not delete {e['folder']}: {ex}")
            total = sum(int(m["pages"] or 0) for m in members)
            rec = {"kind": "cbz", "title": dest.stem, "path": str(dest), "pages": total,
                   "done": total, "status": "CBZ", "members": members}
            uid = self._insert(rec, first_idx)
            self.tree.clearSelection()
            self.rows[uid].setSelected(True)
            self.log(f"Created {dest} ({len(members)} galleries, {total} pages)")
            self.flash(f"CBZ created: {dest.name}")
            self.update_counts()
            self.schedule_save()

        self._run_cbz_job(CbzJob(entries, dest), "Creating CBZ...", ok_cb)

    def cbz_by_color(self, uids: list[str]):
        order = self.ordered_uids()
        chosen = set(uids)
        sel = [u for u in order if u in chosen and self.items[u].get("kind") != "cbz"]
        # same color -> one CBZ together; no color -> each download its own CBZ
        groups: dict[str, list[str]] = {}
        colors: dict[str, str | None] = {}
        for u in sel:
            c = self.items[u].get("color")
            key = c if c else f"solo:{u}"
            groups.setdefault(key, []).append(u)
            colors[key] = c
        if not groups:
            self.flash("Select one or more finished downloads first.")
            return
        problems, bad_rows = [], []
        for key, us in groups.items():
            c = colors[key]
            idx = [order.index(u) for u in us]
            if c and idx != list(range(idx[0], idx[0] + len(idx))):
                between = [order[i] for i in range(idx[0], idx[-1] + 1) if order[i] not in us]
                names = ", ".join(f"'{self.items[b]['title'] or title_from_url(self.items[b]['url'])}'"
                                  for b in between[:3]) + (" ..." if len(between) > 3 else "")
                problems.append(f"\u2022 {color_name(c)}: separated by {names}")
                bad_rows += between
            missing = [u for u in us if self.items[u]["status"] != "Done"
                       or not self.items[u].get("path") or not Path(self.items[u]["path"]).is_dir()]
            if missing:
                what = color_name(c) if c else \
                    f"'{self.items[us[0]]['title'] or title_from_url(self.items[us[0]]['url'])}'"
                problems.append(f"\u2022 {what}: {len(missing)} item(s) not downloaded "
                                "yet (or folder missing)")
                bad_rows += missing
        if problems:
            self.tree.clearSelection()
            for u in bad_rows:
                self.rows[u].setHidden(False)
                self.rows[u].setSelected(True)
            if bad_rows:
                self.tree.scrollToItem(self.rows[bad_rows[0]])
            QMessageBox.warning(
                self, APP_NAME, "Can't make the CBZ files yet:\n\n" + "\n".join(problems) +
                "\n\nRows of the same color must be next to each other. The rows in the way "
                "are now selected - right-drag them to rearrange, then try again.")
            return
        ordered_groups = sorted(groups.items(), key=lambda kv: order.index(kv[1][0]))
        ent_groups = [(colors[k], [self._entry_from_row(u) for u in us])
                      for k, us in ordered_groups]
        dlg = ColorCbzDialog(self, ent_groups, self.settings["output_dir"])
        dark_title_bar(dlg)
        if not dlg.exec():
            return
        out_dir = Path(dlg.out.text().strip() or self.settings["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        names = [ed.text().strip() for ed in dlg.names]
        delete = dlg.delete_cb.isChecked()
        made: list[str] = []

        def run(i: int):
            if i >= len(ent_groups):
                self.flash(f"Created {len(made)} CBZ file(s): " + ", ".join(made))
                return
            color, entries = ent_groups[i]
            dest = out_dir / (fit_name(names[i], out_dir, reserve=8) + ".cbz")
            k = 2
            while dest.exists():
                dest = out_dir / (fit_name(f"{names[i]} ({k})", out_dir, reserve=8) + ".cbz")
                k += 1

            def ok_cb(prefixes, entries=entries, dest=dest, color=color):
                first_idx = min((self.tree.indexOfTopLevelItem(self.rows[e["uid"]])
                                 for e in entries if e["uid"] in self.rows), default=None)
                members = []
                for e, pre in zip(entries, prefixes):
                    members.append({"title": e["title"], "url": e["url"], "pages": e["pages"],
                                    "prefix": pre, "folder": e["folder"],
                                    "color": e.get("color")})
                    if e["uid"] in self.rows:
                        self._drop_row(e["uid"])
                    if delete and e["folder"] and Path(e["folder"]).is_dir():
                        try:
                            shutil.rmtree(e["folder"])
                        except Exception as ex:
                            self.log(f"Could not delete {e['folder']}: {ex}")
                total = sum(int(m["pages"] or 0) for m in members)
                self._insert({"kind": "cbz", "title": dest.stem, "path": str(dest),
                              "pages": total, "done": total, "status": "CBZ",
                              "members": members, "color": color}, first_idx)
                self.log(f"Created {dest} ({len(members)} galleries, {color_name(color)})")
                made.append(dest.name)
                self.schedule_save()
                QTimer.singleShot(0, lambda: run(i + 1))

            self._run_cbz_job(CbzJob(entries, dest),
                              f"Creating CBZ {i + 1} of {len(ent_groups)}...",
                              ok_cb)
        run(0)

    def edit_cbz(self, uid: str):
        rec = self.items[uid]
        old = Path(rec["path"])
        if not old.exists():
            QMessageBox.warning(self, APP_NAME, f"The CBZ file is missing:\n{old}")
            return
        entries = [{"key": f"cbz:{m['prefix']}", "uid": None, "title": m["title"],
                    "url": m.get("url", ""), "pages": m.get("pages", 0),
                    "color": m.get("color"), "folder": m.get("folder", ""),
                    "prefix": m["prefix"], "src": ["cbz", str(old), m["prefix"]]}
                   for m in rec.get("members", [])]
        dlg = CbzDialog(self, entries, old.stem, str(old.parent),
                        available=lambda: self._available_rows(), editing=True)
        dark_title_bar(dlg)
        if not dlg.exec():
            return
        new_entries = dlg.entries()
        kept = {e.get("prefix") for e in new_entries if e.get("prefix")}
        removed = [e for e in entries if e["prefix"] not in kept]
        # galleries taken out of the CBZ go back to the list; unpack them if their
        # original folder no longer exists
        extract, restore = [], []
        out_dir = Path(self.settings["output_dir"])
        for e in removed:
            folder = e["folder"]
            if not folder or not Path(folder).is_dir():
                folder = str(out_dir / fit_name(e["title"], out_dir, reserve=15))
                k = 2
                while Path(folder).exists():
                    folder = str(out_dir / fit_name(f"{e['title']} ({k})", out_dir, reserve=15))
                    k += 1
                extract.append((str(old), e["prefix"], folder))
            restore.append((e, folder))
        new_dir = Path(dlg.out.text().strip() or old.parent)
        new_dir.mkdir(parents=True, exist_ok=True)
        dest = new_dir / (fit_name(dlg.name.text(), new_dir, reserve=8) + ".cbz")
        if dest != old and dest.exists():
            QMessageBox.warning(self, APP_NAME, f"A file with that name already exists:\n{dest}")
            return

        def ok_cb(prefixes):
            idx = self.tree.indexOfTopLevelItem(self.rows[uid])
            members = []
            for e, pre in zip(new_entries, prefixes):
                members.append({"title": e["title"], "url": e["url"], "pages": e["pages"],
                                "prefix": pre, "folder": e["folder"], "color": e.get("color")})
                if e.get("uid") and e["uid"] in self.rows:
                    self._drop_row(e["uid"])
            color = rec.get("color")
            self._drop_row(uid)
            total = sum(int(m["pages"] or 0) for m in members)
            new_rec = {"uid": uid, "kind": "cbz", "title": dest.stem, "path": str(dest),
                       "pages": total, "done": total, "status": "CBZ", "members": members,
                       "color": color}
            idx = min(idx, self.tree.topLevelItemCount())
            self._insert(new_rec, idx)
            for k, (e, folder) in enumerate(restore, 1):
                self._insert({"kind": "gallery", "title": e["title"], "url": e["url"],
                              "pages": e["pages"], "done": e["pages"], "status": "Done",
                              "path": folder, "color": e.get("color")}, idx + k)
            self.log(f"Updated {dest} ({len(members)} galleries"
                     + (f", {len(restore)} taken out" if restore else "") + ")")
            self.flash(f"CBZ saved: {dest.name}")
            self.update_counts()
            self.schedule_save()

        job = CbzJob(new_entries, dest, extract=extract,
                     remove_after=str(old) if dest != old else None)
        self._run_cbz_job(job, "Saving CBZ...", ok_cb)

    # -------------------------------------------------------------- misc --
    def open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        dark_title_bar(dlg)
        if dlg.exec():
            self.settings.update(dlg.values())  # same dict the worker reads
            save_settings(self.settings)
            self.flash("Settings saved.")

    def closeEvent(self, e):
        busy = any(r["status"] in BUSY for r in self.items.values())
        if busy and QMessageBox.question(
                self, APP_NAME, "Downloads are running. Quit anyway?\n"
                "(Unfinished galleries resume next time.)") != QMessageBox.Yes:
            e.ignore()
            return
        self.stop()
        self.save_queue()
        self._hide_hover()
        self.thumb_loader.request(None, None, None)
        self.thumb_loader.wait(2000)
        self.worker.shutdown()
        self.worker.wait(8000)
        e.accept()


def main() -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PHentaiDownloader.4")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_icon())
    apply_dark(app)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
