"""Tiny local web server: paste-link page, library, player, dictionary lookup, vocab book."""
from __future__ import annotations

import errno
import ipaddress
import json
import mimetypes
import re
import shutil
import threading
import sys
import time
import webbrowser
import zipfile
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlparse

from .config import HOST, LIBRARY_DIR, PORT, STATIC_DIR
from .dictionary import Dictionary
from .pipeline import Pipeline, ensure_annotation, list_library
from . import asr, backup, goals, graded, i18n, keep, languages, local_mt, placement, plan, reading, review, network, speaking, tts, usage, wordbook, words, writing
from .settings import delete_provider, load_settings, save_settings, settings_from

_vocab_lock = words._lock  # held while a deleted video's sentences are kept for its words
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
# Settings: one page per group. Sub-pages from before the groups go to their place in a group.
SETTINGS_GROUPS = ("study", "services", "playback", "appearance", "data")
OLD_SETTINGS = {"/settings/download": "data#download", "/settings/storage": "data#storage",
                "/settings/translation": "services#translation", "/settings/speech": "services#speech",
                "/settings/devices": "data#devices", "/settings/backup": "data#backup"}
MOVED = {**{old: f"/settings/{place}" for old, place in OLD_SETTINGS.items()}, "/level": "/goals"}


def load_vocab() -> list[dict]:
    """Every record in the word ledger (words.py); review and 词书 functions pick the ones they need."""
    return words.load()


_dictation_lock = threading.Lock()


def load_dictation(vid: str) -> dict:
    try:
        data = json.loads((LIBRARY_DIR / vid / "dictation.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json(path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


RECENT_PATH = LIBRARY_DIR / "recent.json"
_recent_lock = threading.Lock()
DICT_PASS = 0.9  # same threshold as the player's "练错句"


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_recent() -> list[dict]:
    items = _read_json(RECENT_PATH, [])
    return items if isinstance(items, list) else []


def touch_recent(vid: str, t: float) -> None:
    with _recent_lock:
        items = [r for r in load_recent() if r.get("videoId") != vid]
        items.insert(0, {"videoId": vid, "time": round(max(0.0, t), 1), "at": int(time.time())})
        save_json(RECENT_PATH, items[:10])


def list_mistakes() -> list[dict]:
    """Every dictated sentence whose latest score is below the pass mark, across the library — including
    sentences kept from deleted videos. Each has a `clip` to hear it: {src, start, end}, or None."""
    out = []
    if not LIBRARY_DIR.exists():
        return out
    live = set()
    for d in LIBRARY_DIR.iterdir():
        records = _read_json(d / "dictation.json", {})
        wrong = {i: r for i, r in records.items() if isinstance(r, dict) and r.get("score", 1) < DICT_PASS}
        if not wrong:
            continue
        data = _read_json(d / "data.json", None)
        if not data:
            continue
        live.add(d.name)
        cues = data.get("cues", [])
        media = data.get("video") if data.get("video") and (d / data["video"]).is_file() else ""
        for i, r in wrong.items():
            if not i.isdigit() or int(i) >= len(cues):
                continue
            c = cues[int(i)]
            out.append({
                "videoId": d.name, "title": data.get("title") or d.name, "i": int(i),
                "text": c["text"], "zh": c.get("zh", ""), "start": c["start"],
                "clip": {"src": f"/media/{d.name}/{media}", "start": c["start"], "end": c["end"]} if media else None,
                "score": r.get("score", 0), "best": r.get("best", 0), "tries": r.get("tries", 1),
                "typed": r.get("typed", ""), "at": r.get("at", 0),
            })
    out += keep.mistakes(skip=live)
    out.sort(key=lambda m: -m["at"])
    return out


def vocab_with_sources() -> list[dict]:
    """The word ledger (我的生词), each word with where to hear its sentence and whether its video is still there."""
    items, cache = load_vocab(), {}
    for v in items:
        vid = v.get("videoId")
        if vid:
            v["videoGone"] = not (SAFE_ID.match(vid) and (LIBRARY_DIR / vid / "data.json").is_file())
            v["clip"] = review._clip(v, LIBRARY_DIR, cache)
    return items


def sentences_with_media() -> list[dict]:
    """句子收藏, each video sentence with the media file to hear it from (while the video is still there)."""
    out = []
    for s in words.load_sentences():
        vid = s.get("videoId") or ""
        data = _read_json(LIBRARY_DIR / vid / "data.json", None) if SAFE_ID.match(vid) else None
        media = data.get("video") if data and data.get("video") and (LIBRARY_DIR / vid / data["video"]).is_file() else ""
        out.append({**s, "media": f"/media/{vid}/{media}" if media else ""})
    return out


def sidebar() -> dict:
    recent = []
    for r in load_recent():
        vid = r.get("videoId", "")
        data = _read_json(LIBRARY_DIR / vid / "data.json", None) if SAFE_ID.match(vid) else None
        if not data:
            continue  # deleted video
        records = _read_json(LIBRARY_DIR / vid / "dictation.json", {})
        recent.append({
            "id": vid, "title": data.get("title") or vid, "time": r.get("time", 0),
            "duration": data.get("duration") or 0, "cueCount": len(data.get("cues", [])), "dictated": len(records),
        })
        if len(recent) == 3:
            break
    vocab = load_vocab()
    profile = placement.load_profile()
    today = plan.daily_plan(profile, vocab, dictated_counts()[1])
    goal = {"current": profile.get("current"), "target": profile.get("target"),
            "stageLabel": today["stage"]["name"],  # a UI key: shown in the interface language
            "done": sum(t["complete"] for t in today["tasks"]), "total": len(today["tasks"])}
    return {"mistakes": len(list_mistakes()), "recent": recent, "review": review_badge(vocab), "goal": goal}


def book_item(en: str, body: dict) -> dict:
    sentence = str(body.get("sentence") or "")[:500]
    item = {
        "key": "w:" + en, "type": "word", "en": en[:200], "book": body["book"],
        "zh": str(body.get("zh") or "")[:500], "note": str(body.get("note") or "")[:500],
        "phonetic": str(body.get("phonetic") or "")[:100],
        "sentence": sentence, "sentenceZh": str(body.get("sentenceZh") or "")[:500],
        "videoId": str(body.get("videoId") or "")[:80], "articleId": str(body.get("articleId") or "")[:40],
        "sentenceIndex": int(body.get("sentenceIndex") or 0), "time": float(body.get("time") or 0),
        "added": int(time.time()), "spans": _spans(body.get("spans"), len(sentence)),
    }
    if i18n.load()["zh"] == "zh-TW":
        for key in ("zh", "sentenceZh", "note"):
            item[key] = i18n.to_simplified(item[key])
    return item


def review_badge(vocab: list[dict]) -> int:
    """Reviews due plus today's new words still to learn (saved words and the chosen 词书)."""
    return review.due_count(vocab, new_per_day=new_quota(),
                            extra_new=wordbook.new_left(vocab, placement.load_profile()) or 0)


def review_queue(dic, vocab: list[dict]) -> dict:
    """单词复习: due cards and new saved words, topped up with today's words from the chosen 词书.
    `reserve` holds a few more book words to replace the ones marked 认识 during the session.
    `breakdown` says where today's cards come from, all counted from the word ledger."""
    quota = new_quota()
    q = review.queue(vocab, LIBRARY_DIR, new_per_day=quota)
    state = wordbook.load_state()
    q["book"] = None
    q["reserve"] = []
    q["breakdown"] = {"newSaved": q["new"], "newBook": 0, "due": q["due"], "bookLeft": None, "quota": quota}
    if not state["book"]:
        return q
    profile = placement.load_profile()
    rest = wordbook.remaining(dic, state["book"], vocab, profile)
    slots = max(0, quota - review.started_today(vocab) - q["new"])
    take = [w for w, _ in rest[:slots + 10]]
    cards = wordbook.cards(dic, state["book"], take, i18n.load()["native"], load_settings())
    preview, cache = review.previews(None, time.time()), {}
    for c in cards:
        c["preview"] = preview
        c["clip"] = review._clip(c, LIBRARY_DIR, cache)  # example from your own video: its original sound
    book = next(b for b in wordbook.BOOKS if b["id"] == state["book"])
    q["cards"] += cards[:slots]
    q["reserve"] = cards[slots:]
    q["new"] += min(slots, len(cards))
    q["newLeft"] += len(rest)
    q["breakdown"].update(newBook=min(slots, len(cards)), bookLeft=len(rest))
    q["book"] = {"id": book["id"], "bookTitle": book["bookTitle"], "left": len(rest)}
    return q


def wordbook_overview(dic, lang: str) -> dict:
    vocab, profile, state = load_vocab(), placement.load_profile(), wordbook.load_state()
    books = [{**{k: b[k] for k in ("id", "bookTitle", "bookDesc", "bookLevel")},
              **wordbook.progress(dic, b["id"], vocab, profile)}
             for b in wordbook.books_for(lang)]
    upcoming = []
    if state["book"]:
        rest = wordbook.remaining(dic, state["book"], vocab, profile)[:30]
        chinese = languages.is_chinese(languages.stored(lang))
        for w, _ in rest:
            info = dic.word(w) or {}
            upcoming.append({"en": w, "phonetic": info.get("phonetic", ""),
                             "zh": info.get("zh", "") if chinese else info.get("def", "")})
    return {"books": books, "state": state, "easyLimit": wordbook.easy_limit(profile), "perDay": new_quota(),
            "upcoming": upcoming}


def new_quota() -> int:
    """Today's new-word limit from the study plan (stage default, or what the exam date requires)."""
    return goals.new_per_day()


def dictated_counts() -> tuple[int, int]:
    """(all dictated sentences, dictated today) across the library."""
    today = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    total = today_count = 0
    if LIBRARY_DIR.exists():
        for d in LIBRARY_DIR.iterdir():
            records = _read_json(d / "dictation.json", {})
            if isinstance(records, dict):
                total += len(records)
                today_count += sum(1 for r in records.values() if isinstance(r, dict) and r.get("at", 0) >= today)
    return total, today_count


# Only this computer may change these (a phone in LAN mode can study, not read API keys or stop the server).
LOCAL_ONLY = ("/api/settings", "/api/usage", "/api/localmt", "/api/asr", "/api/network", "/api/backup", "/api/shutdown", "/api/restart")

# API responses whose Chinese is content (subtitles, meanings, saved words): shown in the translation
# language. Everything else is interface text and follows the interface language.
CONTENT_PATHS = ("/api/video/", "/api/reading", "/api/lookup", "/api/vocab", "/api/sentences", "/api/mistakes", "/api/dictation/",
                 "/api/library", "/api/home", "/api/sidebar", "/api/review", "/api/placement/check",
                 "/api/placement/result", "/api/profile", "/api/wordbook", "/api/speaking", "/api/writing")
UI_KEYS = {"error", "preview", "stageLabel", "log", "bookTitle", "bookDesc", "bookLevel"}  # interface text inside content responses


def localize(obj, path: str, lang: dict):
    ui = lambda text: i18n.ui_text(text, lang["ui"])  # noqa: E731
    if not path.startswith(CONTENT_PATHS):
        return i18n.walk(obj, ui)

    def content(value):
        if isinstance(value, dict):
            return {k: i18n.walk(v, ui) if k in UI_KEYS else content(v) for k, v in value.items()}
        if isinstance(value, list):
            return [content(v) for v in value]
        if isinstance(value, str):
            return i18n.content_text(value, lang["zh"])
        return value

    return content(obj)


def code_fingerprint() -> str:
    """Changes whenever a Python file of the app changes. Pages are read from disk on every request,
    but Python code only takes effect after a restart, so the two can drift apart."""
    import hashlib

    h = hashlib.sha1()
    package = Path(__file__).resolve().parent
    for f in sorted([*package.glob("*.py"), *package.glob("*.json"), *package.glob("i18n/*.json")]):
        st = f.stat()
        h.update(f"{f.name}:{st.st_mtime_ns}:{st.st_size};".encode())
    return h.hexdigest()[:12]


RUNNING_VERSION = code_fingerprint()


def version_info() -> dict:
    current = code_fingerprint()
    return {"running": RUNNING_VERSION, "current": current, "stale": current != RUNNING_VERSION}


def restart_in_place() -> None:
    """Replace this process with a fresh one running the current code (same port, no new browser tab)."""
    import os
    import sys

    time.sleep(0.3)  # let the HTTP response reach the page first
    os.environ["IELTS_NO_BROWSER"] = "1"
    args = [a for a in sys.argv if a != "--restart"]
    os.execv(sys.executable, [sys.executable, *args])  # the listening socket is not inherited


def _spans(value, limit: int) -> list:
    """Validated [[start, end], ...] character ranges inside the saved sentence."""
    out = []
    for pair in value if isinstance(value, list) else []:
        if (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, int) for x in pair)
                and 0 <= pair[0] < pair[1] <= limit):
            out.append(pair)
    return out[:6]


def api_ready() -> bool:
    try:
        settings = load_settings()
    except ValueError:
        return False
    return settings.engine == "api" and bool(settings.base_url and settings.model)


def home_stats() -> dict:
    """Numbers for the home page: what is in each section and what was practised today."""
    today = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    dictated, dictated_today = dictated_counts()
    vocab = load_vocab()
    articles = reading.list_articles()
    info = sidebar()
    return {
        "videos": len(list_library()), "articles": len(articles), "latestArticle": articles[0] if articles else None,
        "words": sum(1 for v in vocab if v.get("type") != "phrase" and v["status"] != "known"),
        "phrases": sum(1 for v in vocab if v.get("type") == "phrase" and v["status"] != "known"),
        "savedToday": sum(1 for v in vocab if v.get("added", 0) >= today),
        "dictated": dictated, "dictatedToday": dictated_today, "mistakes": info["mistakes"], "recent": info["recent"],
        "reviewDue": info["review"],
    }


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


class App:
    def __init__(self):
        self.dic = Dictionary(log=say)  # first run: downloads and indexes the dictionary
        merged = words.migrate()  # once: fold vocab.json, the known list and wordbook.json into words.json
        if merged:
            say("单词记录已合并：", str(merged["words"]), "个词，其中", str(merged["known"]), "个已掌握")
        asr.migrate_keys()  # once: keys typed into the old recognition form join the one provider list
        wordbook.attach(self.dic)
        self.pipeline = Pipeline(self.dic)


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ielts-study"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        # ------------------------------------------------------------ helpers
        def _json(self, obj, status=200):
            lang = i18n.load(self.headers.get("Accept-Language"))
            if lang["ui"] != "zh-CN" or lang["zh"] != "zh-CN":
                obj = localize(obj, urlparse(self.path).path, lang)
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                body = json.loads(self.rfile.read(n))
                return body if isinstance(body, dict) else {}
            except ValueError:
                return {}

        def _loopback_client(self) -> bool:
            try:
                ip = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            return ip.is_loopback or (ip.version == 6 and bool(ip.ipv4_mapped) and ip.ipv4_mapped.is_loopback)

        def _local_host(self) -> bool:
            """Sent from this computer to 127.0.0.1 / localhost (not a website using DNS rebinding)."""
            port = self.server.server_port
            return self._loopback_client() and self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _local_request(self) -> bool:
            host = self.headers.get("Host", "")
            return self._local_host() and self.headers.get("Origin") in (None, f"http://{host}")

        def _lan_request(self) -> bool:
            """A phone on the local network that has the access key (LAN mode only)."""
            return (network.STARTED["lan"] and network.private_host(self.headers.get("Host", ""))
                    and network.key_ok(network.cookie_key(self.headers.get("Cookie"))))

        def _admit(self) -> bool:
            """Let the request through, or answer it here (refused, or a phone being given the access key)."""
            if self._local_host() or self._lan_request():
                return True
            if network.STARTED["lan"] and network.private_host(self.headers.get("Host", "")):
                u = urlparse(self.path)
                key = (parse_qs(u.query).get("key") or [""])[0]
                if network.key_ok(key):  # the link / QR code from Settings: remember the key, drop it from the URL
                    self.send_response(303)
                    rest = urlencode([(k, v) for k, v in parse_qsl(u.query) if k != "key"])
                    self.send_header("Location", (u.path or "/") + (f"?{rest}" if rest else ""))
                    self.send_header("Set-Cookie", network.set_cookie(key))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
                body = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                        "<title>IELTS Study</title><body style='font:16px/1.6 system-ui;padding:32px;max-width:520px'>"
                        "<h2>需要访问密钥</h2><p>请在电脑上打开 IELTS Study 的「设置 → 手机访问」，用手机扫描二维码，"
                        "或打开那里显示的链接。</p></body>")
                data = i18n.to_ui(body, i18n.load(self.headers.get("Accept-Language"))["ui"]).encode()
                self.send_response(403)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return False
            self._json({"error": "forbidden"}, 403)
            return False

        def _redirect(self, target: str, query: str = ""):
            path, _, anchor = target.partition("#")
            self.send_response(302)
            self.send_header("Location", path + (f"?{query}" if query else "") + (f"#{anchor}" if anchor else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _file(self, path, ctype=None, download_name=None):
            if not path.exists() or not path.is_file():
                return self._json({"error": "not found"}, 404)
            if path.suffix in (".html", ".js") and STATIC_DIR.resolve() in path.resolve().parents:
                # pages and scripts in the interface language; never cached, so a language switch shows at once
                body = i18n.page(path, i18n.load(self.headers.get("Accept-Language"))["ui"])
                self.send_response(200)
                self.send_header("Content-Type", ctype or mimetypes.guess_type(path.name)[0] or "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
                return None
            size = path.stat().st_size
            ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            status = HTTPStatus.OK
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m:
                    if m.group(1):
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else size - 1
                    elif m.group(2):
                        start = max(0, size - int(m.group(2)))
                    end = min(end, size - 1)
                    if start > end:
                        self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    status = HTTPStatus.PARTIAL_CONTENT
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download_name:
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(download_name)}")
            self.end_headers()
            try:
                with path.open("rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _video_data(self, vid):
            if not SAFE_ID.match(vid):
                return None
            f = LIBRARY_DIR / vid / "data.json"
            if not f.exists():
                return None
            return json.loads(f.read_text(encoding="utf-8"))

        # ------------------------------------------------------------ routes
        def do_GET(self):
            if not self._admit():
                return None
            u = urlparse(self.path)
            p = unquote(u.path)
            q = parse_qs(u.query)
            if p.startswith(LOCAL_ONLY) and not self._local_request():
                return self._json({"error": "这个设置只能在电脑上查看和修改。"}, 403)
            if p == "/":
                return self._file(STATIC_DIR / "home.html", "text/html; charset=utf-8")
            if p in ("/listening", "/index.html"):
                return self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            if p == "/reading":
                return self._file(STATIC_DIR / "reading.html", "text/html; charset=utf-8")
            if p.startswith("/reading/"):
                return self._file(STATIC_DIR / "article.html", "text/html; charset=utf-8")
            if p == "/api/home":
                return self._json(home_stats())
            if p == "/api/version":  # "local": only this computer can restart the server
                return self._json({**version_info(), "local": self._local_request()})
            if p == "/api/language":
                return self._json(i18n.public(self.headers.get("Accept-Language")))
            if p == "/api/storage":
                from . import storage

                return self._json(storage.overview())
            if p == "/api/reading":
                return self._json(reading.list_articles())
            if p == "/api/graded":
                return self._json(graded.overview(i18n.load()["native"]))
            if p == "/api/tts/status":
                return self._json(tts.status())
            if p == "/speaking":
                return self._file(STATIC_DIR / "speaking.html", "text/html; charset=utf-8")
            if p == "/writing":
                return self._file(STATIC_DIR / "writing.html", "text/html; charset=utf-8")
            if p == "/api/speaking":
                return self._json({**speaking.overview(), "questions": speaking.questions(),
                                   "hasApi": api_ready(), "recognizer": asr.load_settings().engine})
            if p == "/api/speaking/sentences":
                source = (q.get("source") or ["video"])[0]
                return self._json({"sentences": speaking.sentences("video" if source == "video" else "passage")})
            if p.startswith("/api/speaking/rec/"):
                path = speaking.recording_path(p.rsplit("/", 1)[1])
                return self._file(path) if path else self._json({"error": "not found"}, 404)
            if p == "/api/writing":
                return self._json({"tasks": writing.tasks(), "history": writing.history(), "hasApi": api_ready(),
                                   "minWords": writing.MIN_WORDS})
            if p.startswith("/api/writing/"):
                entry = writing.load(p.rsplit("/", 1)[1])
                return self._json(entry) if entry else self._json({"error": "not found"}, 404)
            if p == "/api/asr":
                return self._json(asr.load_settings().public())
            if p == "/api/network":
                return self._json(network.status(self.server.server_port))
            if p == "/api/network/qr.svg":
                url = network.phone_url(self.server.server_port)
                if not url:
                    return self._json({"error": "not available"}, 404)
                svg = network.qr_svg(url)
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(svg)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(svg)
                return None
            if p == "/api/backup":
                return self._json(backup.status())
            if p == "/api/backup/export":
                videos = (q.get("videos") or ["0"])[0] == "1"
                backup.RESTORE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = backup.RESTORE_DIR / f"export-{threading.get_ident()}.zip"
                try:
                    backup.write_zip(tmp, videos)
                    return self._file(tmp, "application/zip", backup.file_name())
                finally:
                    tmp.unlink(missing_ok=True)
            if p == "/api/tts":  # a sentence read by the neural voice (WAV, cached)
                q = parse_qs(urlparse(self.path).query)
                try:
                    audio = tts.synthesize((q.get("text") or [""])[0], (q.get("accent") or ["uk"])[0],
                                           float((q.get("speed") or ["1"])[0]))
                except (RuntimeError, ValueError) as error:
                    return self._json({"error": str(error)}, 409 if not tts.ready() else 400)
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio)))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.end_headers()
                self.wfile.write(audio)
                return None
            if p.startswith("/api/reading/"):
                data = reading.load_article(p.rsplit("/", 1)[1])
                if not data:
                    return self._json({"error": "not found"}, 404)
                return self._json(reading.ensure_article_annotation(data, app.dic))
            if p.startswith("/player/"):
                return self._file(STATIC_DIR / "player.html", "text/html; charset=utf-8")
            if p.rstrip("/") in MOVED:
                return self._redirect(MOVED[p.rstrip("/")], urlparse(self.path).query)
            if p == "/goals":
                return self._file(STATIC_DIR / "goals.html", "text/html; charset=utf-8")
            if p == "/api/goals":
                return self._json(goals.today())
            if p == "/api/usage":
                return self._json(usage.summary())
            if p == "/api/localmt":
                return self._json(local_mt.status())
            if p == "/api/profile":
                return self._json(placement.public_profile(load_vocab()))
            if p == "/api/placement":
                return self._json({"passages": [
                    {"title": x["title"], "level": x["level"], "text": x["text"],
                     "words": [{"id": w["id"], "s": w["s"], "e": w["e"], "en": w["en"]} for w in x["words"]]}
                    for x in placement.passages(app.dic, i18n.load()["native"])]})
            if p == "/review":
                return self._file(STATIC_DIR / "review.html", "text/html; charset=utf-8")
            if p == "/api/review":
                return self._json(review_queue(app.dic, load_vocab()))
            if p == "/wordbook":
                return self._file(STATIC_DIR / "wordbook.html", "text/html; charset=utf-8")
            if p == "/api/wordbook":
                return self._json(wordbook_overview(app.dic, i18n.load()["native"]))
            if p == "/api/plan":
                return self._json(plan.daily_plan(placement.load_profile(), load_vocab(), dictated_counts()[1]))
            if p == "/mistakes":
                return self._file(STATIC_DIR / "mistakes.html", "text/html; charset=utf-8")
            if p == "/api/mistakes":
                return self._json(list_mistakes())
            if p == "/api/sidebar":
                return self._json(sidebar())
            if p == "/sentences":
                return self._file(STATIC_DIR / "sentences.html", "text/html; charset=utf-8")
            if p == "/vocab":
                return self._file(STATIC_DIR / "vocab.html", "text/html; charset=utf-8")
            if p.rstrip("/") == "/settings" or p.rstrip("/") in {f"/settings/{g}" for g in SETTINGS_GROUPS}:
                return self._file(STATIC_DIR / "settings.html", "text/html; charset=utf-8")
            if p.startswith("/static/"):
                target = (STATIC_DIR / p[len("/static/") :]).resolve()
                if STATIC_DIR.resolve() not in target.parents:
                    return self._json({"error": "forbidden"}, 403)
                return self._file(target)
            if p == "/api/library":
                return self._json(list_library())
            if p == "/api/settings":
                if not self._local_request():
                    return self._json({"error": "请从本机页面打开设置。"}, 403)
                try:
                    return self._json(load_settings().public())
                except ValueError as error:
                    return self._json({"error": str(error)}, 500)
            if p.startswith("/api/video/"):
                vid = p.rsplit("/", 1)[1]
                data = self._video_data(vid)
                if not data:
                    return self._json({"error": "not found"}, 404)
                from .media import local_thumbnail

                data = ensure_annotation(LIBRARY_DIR / vid, data, app.dic)
                thumb = local_thumbnail(LIBRARY_DIR / vid)
                return self._json({**data, "poster": f"/media/{vid}/{thumb.name}" if thumb else ""})
            if p.startswith("/api/dictation/"):
                vid = p.rsplit("/", 1)[1]
                if not SAFE_ID.match(vid):
                    return self._json({"error": "not found"}, 404)
                return self._json(load_dictation(vid))
            if p.startswith("/media/"):
                parts = p.split("/")
                if len(parts) == 4 and SAFE_ID.match(parts[2]) and re.match(r"^(video|thumb)\.\w+$", parts[3]):
                    return self._file(LIBRARY_DIR / parts[2] / parts[3])
                return self._json({"error": "not found"}, 404)
            if p.startswith("/kept/"):  # sentence clips kept from deleted videos
                parts = p.split("/")
                path = keep.audio_path(parts[2], parts[3]) if len(parts) == 4 and SAFE_ID.match(parts[2]) else None
                return self._file(path, "audio/mp4") if path else self._json({"error": "not found"}, 404)
            if p.startswith("/api/jobs/"):
                job = app.pipeline.jobs.get(p.rsplit("/", 1)[1])
                return self._json(job.to_json()) if job else self._json({"error": "not found"}, 404)
            if p == "/api/lookup":
                w = (q.get("w") or [""])[0].strip().lower()
                w = re.sub(r"[^a-z'-]", "", w)
                info = app.dic.lookup(w) if w else None
                if info:
                    from . import meanings

                    info = meanings.lookup(info, i18n.load()["native"], load_settings())
                return self._json(info or {"en": w, "zh": "", "phonetic": ""})
            if p == "/api/vocab":
                return self._json(vocab_with_sources())
            if p == "/api/sentences":
                return self._json(sentences_with_media())
            if p.startswith("/api/export/"):
                vid = p.rsplit("/", 1)[1].removesuffix(".srt")
                data = self._video_data(vid)
                if not data:
                    return self._json({"error": "not found"}, 404)
                mode = (q.get("mode") or ["bilingual"])[0]
                lines = []
                for n, c in enumerate(data["cues"], 1):
                    text = c["text"] if mode == "en" else (c.get("zh", "") if mode == "zh" else f'{c["text"]}\n{c.get("zh", "")}')
                    lines.append(f"{n}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{text}\n")
                body = "\n".join(lines).encode("utf-8")
                name = re.sub(r'[\\/:*?"<>|]+', "_", data.get("title") or vid)[:80] + ".srt"
                self.send_response(200)
                self.send_header("Content-Type", "application/x-subrip; charset=utf-8")
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._json({"error": "not found"}, 404)

        def _refuse_foreign(self) -> bool:
            """Writes (POST / DELETE) are only accepted from this app's own pages on this computer.

            Blocks other websites (a form or text/plain fetch needs no CORS preflight) and DNS rebinding."""
            if self._local_request():
                return False
            p = urlparse(self.path).path
            if (self._lan_request() and not p.startswith(LOCAL_ONLY)
                    and self.headers.get("Origin") in (None, f"http://{self.headers.get('Host', '')}")):
                return False  # a phone with the key, from this app's own pages
            self._json({"error": "请求需要来自本机页面。"}, 403)
            return True

        def do_POST(self):
            p = urlparse(self.path).path
            if not self._admit() or self._refuse_foreign():
                return None
            if p == "/api/backup/import":  # a backup zip as the raw body
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    return self._json({"error": "请选择备份文件。"}, 400)
                backup.RESTORE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = backup.RESTORE_DIR / f"upload-{int(time.time())}.zip"
                try:
                    with tmp.open("wb") as f:
                        left = n
                        while left > 0:
                            chunk = self.rfile.read(min(1 << 20, left))
                            if not chunk:
                                break
                            f.write(chunk)
                            left -= len(chunk)
                    return self._json(backup.restore(tmp))
                except (ValueError, zipfile.BadZipFile) as error:
                    msg = str(error) if isinstance(error, ValueError) else "这不是有效的 zip 备份文件。"
                    return self._json({"error": msg}, 400)
                finally:
                    tmp.unlink(missing_ok=True)
            if p == "/api/speaking/recognize":  # a recording from the speaking page (raw audio body)
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= speaking.MAX_UPLOAD:
                    return self._json({"error": "录音是空的或太长了。"}, 400)
                try:
                    path = speaking.save_recording(self.rfile.read(n), self.headers.get("Content-Type", ""))
                    result = asr.recognize(path)
                except ValueError as error:
                    return self._json({"error": str(error)}, 400)
                except Exception as error:  # noqa: BLE001 - recognition failed; say why
                    return self._json({"error": f"语音识别失败：{error}"}, 500)
                return self._json({**result, "rec": path.name,
                                   "stats": speaking.delivery(result["words"], result["duration"])})
            settings_routes = ("/api/settings", "/api/settings/test", "/api/settings/models")
            body = self._body()
            if p == "/api/settings/delete":  # forget one provider's address, model and key
                try:
                    provider = str(body.get("provider") or "") if isinstance(body, dict) else ""
                    settings = delete_provider(provider)
                    asr.forget_key(provider)
                    return self._json(settings.public())
                except ValueError as error:
                    return self._json({"error": str(error)}, 400)
                except OSError:
                    return self._json({"error": "无法保存本机设置。"}, 500)
            if p in settings_routes:
                if not body:
                    return self._json({"error": "请提交有效的设置。"}, 400)
                try:
                    if p == "/api/settings":
                        return self._json(save_settings(body).public())
                    from .translate import list_models, translate_batch

                    discover = p == "/api/settings/models"
                    settings = settings_from({**body, "engine": "api"}, load_settings(), require_model=not discover)
                    if discover:
                        return self._json({"models": list_models(settings)})
                    translate_batch(["The subtitles are ready."], settings)
                    return self._json({"ok": True})
                except ValueError as error:
                    return self._json({"error": str(error)}, 400)
                except RuntimeError as error:
                    return self._json({"error": str(error)}, 502)
                except OSError:
                    return self._json({"error": "无法保存本机设置。"}, 500)
            if p == "/api/import":
                url = (body.get("url") or "").strip()
                if not re.match(r"^https?://", url):
                    return self._json({"error": "请粘贴以 http(s):// 开头的视频链接"}, 400)
                job = app.pipeline.submit(url, str(body.get("quality") or "720"), bool(body.get("overwrite")))
                return self._json(job.to_json())
            if p == "/api/language":
                i18n.save(body)
                return self._json(i18n.public())
            if p == "/api/shutdown":  # `ielts-study --stop` on every system
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return None
            if p == "/api/restart":
                self._json({"ok": True})
                threading.Thread(target=restart_in_place, daemon=True).start()
                return None
            if p == "/api/reading":
                text = str(body.get("text") or "").strip()
                if len(re.findall(r"[A-Za-z]+", text)) < 3:
                    return self._json({"error": "请粘贴一段英文文章。"}, 400)
                if len(text) > reading.MAX_CHARS:
                    return self._json({"error": f"文章太长了，请控制在 {reading.MAX_CHARS // 1000}k 字符以内，或分几次粘贴。"}, 400)
                title = re.sub(r"\s+", " ", str(body.get("title") or "")).strip()[:120]
                return self._json(reading.submit_article(app.pipeline, text, title).to_json())
            if p == "/api/tts/download":
                return self._json(tts.start_download())
            if p == "/api/localmt/download":
                return self._json(local_mt.start_download())
            if p == "/api/speaking/shadow":
                text = str(body.get("text") or "")[:600]
                try:
                    score = min(1.0, max(0.0, float(body.get("score"))))
                except (TypeError, ValueError):
                    return self._json({"error": "bad score"}, 400)
                speaking.add_history({"kind": "shadow", "text": text, "score": round(score, 3),
                                      "source": str(body.get("source") or "")[:20],
                                      "rec": str(body.get("rec") or "")[:40]})
                return self._json(speaking.overview())
            if p == "/api/speaking/feedback":
                settings = load_settings()
                if settings.engine != "api":
                    return self._json({"error": "口语评分需要先在设置里配置 API。"}, 400)
                try:
                    part = int(body.get("part") or 1)
                    stats = body.get("stats") if isinstance(body.get("stats"), dict) else {}
                    question = str(body.get("question") or "")[:600]
                    transcript = str(body.get("transcript") or "").strip()[:6000]
                    if len(transcript.split()) < 3:
                        return self._json({"error": "录音里没有识别到足够的英文。"}, 400)
                    result = speaking.feedback(part, question, transcript, stats, settings, i18n.load()["native"])
                except (ValueError, TypeError):
                    return self._json({"error": "bad request"}, 400)
                except RuntimeError as error:
                    return self._json({"error": str(error)}, 502)
                entry = speaking.add_history({"kind": "test", "part": part, "question": question,
                                              "transcript": transcript, "stats": stats, "result": result,
                                              "rec": str(body.get("rec") or "")[:40]})
                return self._json(entry)
            if p == "/api/writing/mark":
                settings = load_settings()
                if settings.engine != "api":
                    return self._json({"error": "作文批改需要先在设置里配置 API。"}, 400)
                essay = str(body.get("essay") or "").strip()
                found = writing.find_task(str(body.get("taskId") or ""))
                kind, task = found if found else (str(body.get("kind") or "task2"), {})
                if kind not in writing.MIN_WORDS:
                    return self._json({"error": "bad kind"}, 400)
                prompt = task.get("prompt") or str(body.get("prompt") or "").strip()[:2000]
                if not prompt:
                    return self._json({"error": "请先选一道题，或粘贴你自己的题目。"}, 400)
                if writing.word_count(essay) < 40:
                    return self._json({"error": "作文太短了，至少写 40 个词再批改。"}, 400)
                if len(essay) > writing.MAX_CHARS:
                    return self._json({"error": "作文太长了。"}, 400)
                try:
                    result = writing.mark(kind, prompt, essay, settings, i18n.load()["native"], task.get("chart"))
                except RuntimeError as error:
                    return self._json({"error": str(error)}, 502)
                seconds = body.get("seconds")
                entry = writing.save({"kind": kind, "taskId": task.get("id", ""), "prompt": prompt,
                                      "chart": task.get("chart"), "essay": essay, "words": writing.word_count(essay),
                                      "seconds": int(seconds) if isinstance(seconds, (int, float)) else None,
                                      "result": result})
                return self._json(entry)
            if p.startswith(LOCAL_ONLY) and not self._local_request():
                return self._json({"error": "这个设置只能在电脑上修改。"}, 403)
            if p == "/api/network":
                network.save(bool(body.get("lan")), new_key=body.get("newKey") is True)
                return self._json(network.status(self.server.server_port))
            if p == "/api/backup/settings":
                try:
                    backup.set_folder(str(body.get("folder") or ""), body.get("keep"))
                except backup.MissingFolder as error:  # the page offers to create it
                    return self._json({"error": str(error)}, 404)
                except (ValueError, TypeError) as error:
                    return self._json({"error": str(error)}, 400)
                return self._json(backup.status())
            if p == "/api/backup/mkdir":  # create the chosen backup folder
                folder = Path(str(body.get("folder") or "")).expanduser()
                if not folder.is_absolute() or not folder.parent.is_dir():
                    return self._json({"error": "找不到这个文件夹的上一级，请填写完整路径。"}, 400)
                try:
                    folder.mkdir(exist_ok=True)
                except OSError as error:
                    return self._json({"error": f"无法新建文件夹：{error}"}, 400)
                return self._json({"ok": True})
            if p == "/api/backup/now":
                try:
                    backup.backup_now()
                except ValueError as error:
                    return self._json({"error": str(error)}, 400)
                return self._json(backup.status())
            if p in ("/api/asr", "/api/asr/download"):
                try:
                    if p == "/api/asr/download":
                        size = str(body.get("size") or "")
                        if size not in asr.MODELS:
                            return self._json({"error": "bad size"}, 400)
                        asr.download(size)
                        return self._json(asr.load_settings().public())
                    return self._json(asr.save_settings(body).public())
                except ValueError as error:
                    return self._json({"error": str(error)}, 400)
            if p == "/api/graded/open":
                out = graded.open_passage(app.pipeline, str(body.get("id") or ""))
                return self._json(out, 404 if out.get("error") else 200)
            if p == "/api/graded/generate":
                level = str(body.get("level") or "")
                if level not in graded.LEVEL_IDS:
                    return self._json({"error": "bad level"}, 400)
                settings = load_settings()
                if settings.engine != "api":
                    return self._json({"error": "用 API 写文章需要先在设置里配置翻译 API。"}, 400)
                return self._json(graded.generate(app.pipeline, level, str(body.get("topic") or ""), settings).to_json())
            if p.startswith("/api/reading/") and p.endswith("/retranslate"):
                aid = p.split("/")[3]
                if not reading.load_article(aid):
                    return self._json({"error": "not found"}, 404)
                return self._json(reading.retranslate_article(app.pipeline, aid).to_json())
            if p.startswith("/api/video/") and p.endswith("/shrink"):
                from .storage import SHRINK_MODES

                vid = p.split("/")[3]
                if not SAFE_ID.match(vid) or not (LIBRARY_DIR / vid / "data.json").exists():
                    return self._json({"error": "not found"}, 404)
                mode = str(body.get("mode") or "")
                if mode not in SHRINK_MODES:
                    return self._json({"error": "请选择压缩方式。"}, 400)
                return self._json(app.pipeline.shrink(vid, mode).to_json())
            if p.startswith("/api/video/") and p.endswith("/retranslate"):
                vid = p.split("/")[3]
                if not SAFE_ID.match(vid) or not (LIBRARY_DIR / vid / "data.json").exists():
                    return self._json({"error": "not found"}, 404)
                return self._json(app.pipeline.retranslate(vid).to_json())
            if p == "/api/recent":
                vid = str(body.get("videoId") or "")
                if not SAFE_ID.match(vid) or not (LIBRARY_DIR / vid / "data.json").exists():
                    return self._json({"error": "not found"}, 404)
                try:
                    t = float(body.get("time") or 0)
                except (TypeError, ValueError):
                    t = 0.0
                touch_recent(vid, t)
                return self._json({"ok": True})
            if p.startswith("/api/dictation/"):
                vid = p.rsplit("/", 1)[1]
                if not SAFE_ID.match(vid):
                    return self._json({"error": "not found"}, 404)
                try:
                    i = int(body.get("i"))
                    score = min(1.0, max(0.0, float(body.get("score"))))
                except (TypeError, ValueError):
                    return self._json({"error": "bad request"}, 400)

                def attempt(prev):
                    return {
                        "score": round(score, 3),
                        "best": round(max(score, prev.get("best", 0)), 3),
                        "tries": prev.get("tries", 0) + 1,
                        "typed": str(body.get("typed") or "")[:1000],
                        "at": int(time.time()),
                    }

                with _dictation_lock:
                    if (LIBRARY_DIR / vid / "data.json").exists():
                        records = load_dictation(vid)
                        record = records[str(i)] = attempt(records.get(str(i), {}))
                        save_json(LIBRARY_DIR / vid / "dictation.json", records)
                    else:  # a sentence kept from a deleted video
                        record = keep.record_dictation(vid, i, attempt)
                        if record is None:
                            return self._json({"error": "not found"}, 404)
                plan.log_activity("dictate")
                return self._json(record)
            if p == "/api/placement/check":
                unknown = {str(i) for i in body.get("unknown") or []}
                return self._json({"questions": placement.spot_check(app.dic, unknown, lang=i18n.load()["native"],
                                                                     settings=load_settings())})
            if p == "/api/placement/result":
                ids = lambda key: {str(i) for i in body.get(key) or []}  # noqa: E731
                result = placement.score_test(app.dic, ids("unknown"), ids("wrong"), ids("checked"),
                                              lang=i18n.load()["native"], settings=load_settings())
                placement.record_test(result)
                return self._json({**result, "profile": placement.public_profile(load_vocab())})
            if p == "/api/profile":
                placement.update_profile(body)
                return self._json(placement.public_profile(load_vocab()))
            if p == "/api/wordbook":
                wordbook.save_state(body)
                return self._json(wordbook_overview(app.dic, i18n.load()["native"]))
            if p == "/api/wordbook/examples":  # example sentences for book words, written by the API (cached)
                items = [str(w).lower()[:60] for w in (body.get("words") or [])[:40] if isinstance(w, str)]
                return self._json(wordbook.api_examples(items, i18n.load()["native"], load_settings(), say))
            if p == "/api/known":
                words.set_known(str(body.get("en") or ""), bool(body.get("known", True)))
                return self._json(placement.public_profile(load_vocab()))
            if p == "/api/review":
                en = str(body.get("en") or "").lower()
                grade = body.get("grade")
                if grade not in review.GRADES:
                    return self._json({"error": "bad grade"}, 400)
                before = words.find(load_vocab(), en)
                was_new = not (before and before.get("srs"))
                fresh = book_item(en, body) if not before and body.get("book") in wordbook.BOOK_IDS and en else None
                item = words.grade(en, grade, fresh)  # a 词书 word met for the first time joins the ledger
                if not item:
                    return self._json({"error": "not found"}, 404)
                plan.log_activity("review")
                if was_new:
                    plan.log_activity("new")
                return self._json({"srs": item["srs"], "dueCount": review_badge(load_vocab())})
            if p == "/api/activity":
                kind = body.get("kind")
                if kind not in ("listen", "read"):
                    return self._json({"error": "bad kind"}, 400)
                try:
                    seconds = min(120.0, max(0.0, float(body.get("seconds") or 0)))
                except (TypeError, ValueError):
                    return self._json({"error": "bad seconds"}, 400)
                return self._json(plan.log_activity(kind, seconds))
            if p == "/api/vocab":
                if i18n.load()["zh"] == "zh-TW":  # the page shows Traditional; store Simplified like everything else
                    for key in ("zh", "sentenceZh", "note"):
                        if isinstance(body.get(key), str):
                            body[key] = i18n.to_simplified(body[key])
                item = {
                    "key": str(body.get("key") or "")[:200],
                    "type": body.get("type") if body.get("type") in ("word", "phrase") else "word",
                    "en": str(body.get("en") or "")[:200],
                    "zh": str(body.get("zh") or "")[:500],
                    "note": str(body.get("note") or "")[:500],
                    "phonetic": str(body.get("phonetic") or "")[:100],
                    "sentence": str(body.get("sentence") or "")[:500],
                    "sentenceZh": str(body.get("sentenceZh") or "")[:500],
                    "videoId": str(body.get("videoId") or "")[:80],
                    "articleId": str(body.get("articleId") or "")[:40],
                    "sentenceIndex": int(body.get("sentenceIndex") or 0),
                    "time": float(body.get("time") or 0),
                    "added": int(time.time()),
                    # where the word sits in the sentence, for the cloze card in 单词复习: [[start, end], ...]
                    "spans": _spans(body.get("spans"), len(str(body.get("sentence") or "")[:500])),
                }
                if not item["en"]:
                    return self._json({"error": "empty"}, 400)
                source = "video" if item["videoId"] else "article" if item["articleId"] else "saved"
                words.add(item, source)  # straight into 单词复习; saving again keeps its review progress
                return self._json({"ok": True, "count": len(load_vocab())})
            if p == "/api/sentences":  # 句子收藏: a whole sentence, kept apart from the words
                text = re.sub(r"\s+", " ", str(body.get("text") or "")).strip()[:600]
                if not text:
                    return self._json({"error": "empty"}, 400)
                zh = str(body.get("zh") or "")[:600]
                if i18n.load()["zh"] == "zh-TW":
                    zh = i18n.to_simplified(zh)
                entry = words.add_sentence({
                    "text": text, "zh": zh, "title": str(body.get("title") or "")[:200],
                    "videoId": str(body.get("videoId") or "")[:80], "time": float(body.get("time") or 0),
                    "end": float(body.get("end") or 0), "articleId": str(body.get("articleId") or "")[:40],
                    "sentenceIndex": int(body.get("sentenceIndex") or 0)})
                return self._json(entry)
            return self._json({"error": "not found"}, 404)

        def do_DELETE(self):
            p = unquote(urlparse(self.path).path)
            if not self._admit() or self._refuse_foreign():
                return None
            if p.startswith("/api/vocab/"):
                words.remove(p[len("/api/vocab/") :])
                return self._json({"ok": True})
            if p.startswith("/api/sentences/"):
                ok = words.remove_sentence(p.rsplit("/", 1)[1])
                return self._json({"ok": True}) if ok else self._json({"error": "not found"}, 404)
            if p.startswith("/api/writing/"):
                ok = writing.delete(p.rsplit("/", 1)[1])
                return self._json({"ok": True}) if ok else self._json({"error": "not found"}, 404)
            if p.startswith("/api/reading/"):
                ok = reading.delete_article(p.rsplit("/", 1)[1])
                return self._json({"ok": True}) if ok else self._json({"error": "not found"}, 404)
            if p.startswith("/api/video/"):
                vid = p.rsplit("/", 1)[1]
                if SAFE_ID.match(vid) and (LIBRARY_DIR / vid).is_dir():
                    # mistakes and saved words keep their sentence and its sound
                    with _dictation_lock, _vocab_lock:
                        try:
                            kept = keep.archive(LIBRARY_DIR / vid, load_vocab())
                        except Exception as error:  # never block the delete; the words themselves stay in vocab
                            say("保留错句时出错：", str(error))
                            kept = {"sentences": 0, "audio": 0}
                        shutil.rmtree(LIBRARY_DIR / vid)
                    return self._json({"ok": True, "kept": kept})
                return self._json({"error": "not found"}, 404)
            return self._json({"error": "not found"}, 404)

    return Handler


def _already_running(url: str) -> bool:
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    # Identify the listener even if a previous version cannot serve the library.
    try:
        response = urlopen(Request(url, method="HEAD"), timeout=2)
    except HTTPError as error:
        response = error  # older servers respond to HEAD with 501 and still identify themselves
    except OSError:
        return False
    with response:
        return response.headers.get("Server", "").startswith(("ielts-study", "whatsub-lite"))  # also the pre-rename build


def _stop_running_instance() -> bool:
    """Stop the IELTS Study server listening on PORT (only if it really is ours, including the old whatsub-lite build)."""
    import os
    import signal
    import subprocess

    try:
        pids = subprocess.run(["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN", "-t"],
                              capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return False
    stopped = False
    for pid in map(int, pids):
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        if ("ielts-study" in command or "whatsub-lite" in command) and pid != os.getpid():
            os.kill(pid, signal.SIGTERM)
            stopped = True
    if stopped:
        for _ in range(50):  # wait up to 5 s for the port to be released
            if not _already_running(f"http://{HOST}:{PORT}/"):
                break
            time.sleep(0.1)
    return stopped


def _running_is_stale(url: str) -> bool:
    """True when the server on the port runs older code than this checkout (or predates /api/version)."""
    from urllib.error import HTTPError
    from urllib.request import urlopen

    try:
        with urlopen(url + "api/version", timeout=2) as response:
            return json.loads(response.read()).get("running") != RUNNING_VERSION
    except HTTPError as error:
        return error.code == 404
    except (OSError, ValueError):
        return False


def cli_language() -> str:
    """Language for terminal messages: the saved interface language, else the system locale, else English."""
    import os

    saved = i18n._read()
    if saved:
        return i18n._normalize(saved)["ui"]
    env = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    return i18n.from_browser(env.split(".")[0].replace("_", "-")) or "en"


def say(*parts: str) -> None:
    """Print a terminal message in the user's language. Pass addresses and numbers as separate parts, so the
    translatable text around them stays recognizable."""
    lang = cli_language()
    print(" ".join(i18n.to_ui(str(part), lang) for part in parts), flush=True)


def _request_shutdown(url: str) -> bool:
    """Ask the running server to quit (works on every system); False for builds without /api/shutdown."""
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    origin = url.rstrip("/")
    try:
        with urlopen(Request(url + "api/shutdown", data=b"{}", method="POST",
                             headers={"Origin": origin, "Content-Type": "application/json"}), timeout=3):
            pass
    except (URLError, OSError):
        return False
    for _ in range(50):  # wait up to 5 s for the port to be released
        if not _already_running(url):
            return True
        time.sleep(0.1)
    return False


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind the same port, so a second start would not notice
    # the first one; elsewhere it only allows a quick restart.
    allow_reuse_address = sys.platform != "win32"


def stop() -> int:
    """`ielts-study --stop`: quit the server that is running in the background (e.g. started by the desktop app)."""
    url = f"http://{HOST}:{PORT}/"
    if not _already_running(url):
        say("IELTS Study 没有在运行。")
        return 0
    if _request_shutdown(url) or _stop_running_instance():
        say("已停止 IELTS Study。")
        return 0
    say("没能停止 IELTS Study。")
    return 1


def main(open_browser: bool = True, restart: bool = False):
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    say("IELTS Study 启动中…")
    url = f"http://{HOST}:{PORT}/"
    if _already_running(url) and (restart or _running_is_stale(url)):
        if not restart:
            say("后台运行的是旧版本，正在换成最新代码…")
        if _request_shutdown(url) or _stop_running_instance():
            say("已停止旧的 IELTS Study，正在用最新代码重新启动…")
    try:
        httpd = Server((network.bind_host(), PORT), make_handler(App()))
    except OSError as error:
        if error.errno not in (errno.EADDRINUSE, 10048):  # 10048: WSAEADDRINUSE on Windows
            raise
        if _already_running(url):
            say("IELTS Study 已经在运行：", url)
            if open_browser:
                webbrowser.open(url)
            raise SystemExit(0) from None  # not an error: the page is open
        say("端口", str(PORT), "被其他程序占用。可以设置环境变量 IELTS_PORT 换一个端口，例如 8766。")
        raise SystemExit(1) from None
    say("已启动：", url, "（按 Ctrl+C 退出）")
    threading.Thread(target=asr._start_upgrade, daemon=True).start()  # finish a large speech model left half-way
    backup.start_auto()
    if network.STARTED["lan"]:
        phone = network.phone_url(PORT)
        say("手机访问已开启：", phone or "（没有找到局域网地址）")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        say("已退出")
