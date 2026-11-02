# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp>=1.2,<2", "httpx>=0.27", "cryptography>=42", "selectolax>=1.0"]
# ///
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from mcp.server.fastmcp import FastMCP
from selectolax.lexbor import LexborHTMLParser as HTMLParser

BASE = os.environ.get("MOODLE_BASE_URL", "https://elearning.dhbw-stuttgart.de/moodle").rstrip("/")
HOST = urlparse(BASE).hostname
STATE_DIR = Path.home() / ".config" / "moodle-mcp"
SESSION_FILE = STATE_DIR / "session.json"
CHROME_DIR = Path.home() / "Library/Application Support/Google/Chrome"
KEEPALIVE_SECONDS = 15 * 60


class AuthError(Exception):
    pass


def _chrome_key() -> bytes:
    # Going through /usr/bin/security keeps the Keychain requester identity stable,
    # so "Always Allow" on the prompt actually persists.
    out = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise AuthError("Keychain access to 'Chrome Safe Storage' was denied.")
    return hashlib.pbkdf2_hmac("sha1", out.stdout.strip().encode(), b"saltysalt", 1003, 16)


def _decrypt(value: bytes, key: bytes, host_key: str) -> str:
    if value[:3] != b"v10":
        return value.decode()
    dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    raw = dec.update(value[3:]) + dec.finalize()
    raw = raw[: -raw[-1]]
    # Chrome >= 130 prefixes the plaintext with sha256(host_key).
    if raw[:32] == hashlib.sha256(host_key.encode()).digest():
        raw = raw[32:]
    return raw.decode()


def _chrome_cookie_candidates() -> list[str]:
    rows: list[tuple[bytes, str, int]] = []
    for db in CHROME_DIR.glob("*/Cookies"):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "Cookies"
            shutil.copy(db, copy)
            con = sqlite3.connect(copy)
            rows += con.execute(
                "SELECT encrypted_value, host_key, last_access_utc FROM cookies "
                "WHERE name = 'MoodleSession' AND host_key LIKE ?",
                (f"%{HOST}",),
            ).fetchall()
            con.close()
    if not rows:
        raise AuthError(f"No MoodleSession cookie in Chrome. Log in to {BASE} in Chrome, then retry.")
    key = _chrome_key()
    rows.sort(key=lambda r: r[2], reverse=True)
    return [_decrypt(v, key, h) for v, h, _ in rows]


class Moodle:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.client: httpx.Client | None = None
        self.sesskey = ""

    def _try(self, cookie: str) -> bool:
        client = httpx.Client(cookies={"MoodleSession": cookie}, follow_redirects=True, timeout=60)
        r = client.get(f"{BASE}/my/")
        m = re.search(r'"sesskey":"([^"]+)"', r.text)
        if "login/index.php" in str(r.url) or not m:
            client.close()
            return False
        self.client, self.sesskey = client, m.group(1)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps({"cookie": cookie}))
        SESSION_FILE.chmod(0o600)
        return True

    def ensure(self) -> httpx.Client:
        with self.lock:
            if self.client and self.touch():
                return self.client
            if SESSION_FILE.exists() and self._try(json.loads(SESSION_FILE.read_text())["cookie"]):
                return self.client
            for cookie in _chrome_cookie_candidates():
                if self._try(cookie):
                    return self.client
            raise AuthError(f"Chrome's Moodle session is expired. Open {BASE} in Chrome, log in, then retry.")

    def touch(self) -> bool:
        if not self.client:
            return False
        try:
            r = self.client.post(
                f"{BASE}/lib/ajax/service.php?sesskey={self.sesskey}",
                json=[{"index": 0, "methodname": "core_session_touch", "args": {}}],
            )
            return r.status_code == 200 and not r.json()[0].get("error")
        except (httpx.HTTPError, ValueError, KeyError, IndexError):
            return False

    def ajax(self, method: str, args: dict) -> object:
        client = self.ensure()
        r = client.post(
            f"{BASE}/lib/ajax/service.php?sesskey={self.sesskey}",
            json=[{"index": 0, "methodname": method, "args": args}],
        )
        res = r.json()[0]
        if res.get("error"):
            raise RuntimeError(res["exception"].get("message", str(res)))
        return res["data"]

    def html(self, path: str) -> HTMLParser:
        return HTMLParser(self.ensure().get(f"{BASE}/{path.lstrip('/')}").text)


moodle = Moodle()
mcp = FastMCP("moodle")


def _keepalive() -> None:
    while True:
        time.sleep(KEEPALIVE_SECONDS)
        with moodle.lock:
            if moodle.client and not moodle.touch():
                moodle.client = None


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.text(separator=" ")).strip()


@mcp.tool()
def list_courses() -> list[dict]:
    """List the courses the user is enrolled in (id, name, url)."""
    data = moodle.ajax(
        "core_course_get_enrolled_courses_by_timeline_classification",
        {"classification": "all", "limit": 0, "offset": 0, "sort": "fullname"},
    )
    return [{"id": c["id"], "name": c["fullname"], "url": c["viewurl"]} for c in data["courses"]]


@mcp.tool()
def course_contents(course_id: int) -> list[dict]:
    """List sections and activities (resources, folders, links, assignments) of a course."""
    doc = moodle.html(f"course/view.php?id={course_id}")
    sections = []
    for sec in doc.css("li.section"):
        title = sec.css_first(".sectionname, h3")
        items = []
        for act in sec.css("li.activity"):
            link = act.css_first("a.aalink, .activityname a, .activity-instance a")
            if not link:
                continue
            name = link.css_first(".instancename")
            label = _text(name) if name else _text(link)
            items.append({"name": label, "url": link.attributes.get("href", ""),
                          "type": next((c[8:] for c in act.attributes.get("class", "").split() if c.startswith("modtype_")), "")})
        sections.append({"section": _text(title) if title else "", "items": items})
    return [s for s in sections if s["items"]]


@mcp.tool()
def folder_contents(folder_url: str) -> list[dict]:
    """List files inside a Moodle folder activity (mod/folder/view.php?id=...)."""
    doc = HTMLParser(moodle.ensure().get(folder_url).text)
    return [{"name": _text(a), "url": a.attributes["href"]}
            for a in doc.css("a[href*='pluginfile.php']")]


@mcp.tool()
def download(url: str, dest_dir: str) -> str:
    """Download a Moodle file (resource view URL or pluginfile URL) into dest_dir. Returns the saved path."""
    client = moodle.ensure()
    r = client.get(url)
    if "text/html" in r.headers.get("content-type", ""):
        doc = HTMLParser(r.text)
        link = doc.css_first("a[href*='pluginfile.php'], object[data*='pluginfile.php'], iframe[src*='pluginfile.php']")
        if not link:
            raise RuntimeError(f"No file found behind {url}")
        attrs = link.attributes
        r = client.get(attrs.get("href") or attrs.get("data") or attrs.get("src"))
    name = unquote(urlparse(str(r.url)).path.rsplit("/", 1)[-1])
    dest = Path(dest_dir).expanduser() / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return str(dest)


if __name__ == "__main__":
    if sys.argv[1:] == ["login"]:
        moodle.ensure()
        print(f"OK, session cached in {SESSION_FILE}")
    else:
        threading.Thread(target=_keepalive, daemon=True).start()
        mcp.run()
