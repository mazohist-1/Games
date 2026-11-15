#!/usr/bin/env python3
"""Сервер игротеки: раздаёт игры и хранит аккаунты, сохранения и рейтинг.

Нужен только Python 3.8+ без сторонних пакетов.
Запуск:  python3 server.py            (порт 8000)
         python3 server.py 8080       (другой порт)
База лежит в data/games.db рядом с этим файлом.
"""
import contextlib
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from http import cookies
from urllib.parse import urlparse, unquote

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("GAMES_DATA") or os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "games.db")
COOKIE = "gh_session"
SESSION_DAYS = 60
MAX_BODY = 16 * 1024 * 1024          # одно сохранение «Царств» бывает крупным
MAX_USER_BYTES = 40 * 1024 * 1024    # сколько всего может хранить один игрок
PBKDF2_ROUNDS = 200_000
LOGIN_RE = re.compile(r"^[0-9A-Za-zА-Яа-яЁё_.-]{3,24}$")
HIDDEN = ("/data", "/.git", "/server.py", "/.env")

db_lock = threading.Lock()
fail_log = {}  # ip -> [время неудачных входов]


@contextlib.contextmanager
def db():
    """Соединение на одну операцию: коммит при успехе, откат при ошибке, всегда закрытие."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with db_lock, db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
              id INTEGER PRIMARY KEY,
              login TEXT NOT NULL,
              lkey TEXT UNIQUE NOT NULL,          -- логин в нижнем регистре (и для кириллицы)
              name TEXT NOT NULL,
              salt BLOB NOT NULL,
              hash BLOB NOT NULL,
              created INTEGER NOT NULL,
              seen INTEGER NOT NULL,
              stats TEXT NOT NULL DEFAULT '{}',
              score INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions(
              token TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              expires INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS kv(
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              k TEXT NOT NULL,
              v TEXT NOT NULL,
              updated INTEGER NOT NULL,
              PRIMARY KEY(user_id, k)
            );
            """
        )


def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)


def token_id(token):
    return hashlib.sha256(token.encode()).hexdigest()


def public_user(row):
    return {
        "login": row["login"],
        "name": row["name"],
        "created": row["created"],
        "stats": json.loads(row["stats"] or "{}"),
        "score": row["score"],
    }


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "GamesHub/1.0"
    extensions_map = dict(
        http.server.SimpleHTTPRequestHandler.extensions_map,
        **{
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".md": "text/plain; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
        },
    )

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            return  # не засоряем терминал синхронизацией
        super().log_message(fmt, *args)

    # ---------- статика ----------
    def send_head(self):
        path = unquote(urlparse(self.path).path)
        if any(path == h or path.startswith(h + "/") for h in HIDDEN):
            self.send_error(404)
            return None
        return super().send_head()

    def end_headers(self):
        p = urlparse(self.path).path
        if p.endswith(".html") or p.endswith("/") or p.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self.api("GET")
        return super().do_GET()

    def do_HEAD(self):
        if self.path.startswith("/api/"):
            self.send_error(405)
            return
        return super().do_HEAD()

    def do_POST(self):
        if self.path.startswith("/api/"):
            return self.api("POST")
        self.send_error(405)

    # ---------- API ----------
    def api(self, method):
        route = urlparse(self.path).path[len("/api/"):].strip("/")
        try:
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    raise ApiError(413, "Слишком большие данные")
                raw = self.rfile.read(n) if n else b""
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        raise ApiError(400, "Неверный запрос")
                if not isinstance(body, dict):
                    raise ApiError(400, "Неверный запрос")
            fn = getattr(self, "api_" + route.replace("-", "_"), None)
            if fn is None or route.startswith("_"):
                raise ApiError(404, "Нет такого метода")
            code, out, extra = fn(method, body)
            self.reply(code, out, extra)
        except ApiError as e:
            self.reply(e.code, {"error": e.msg})
        except Exception as e:  # noqa
            sys.stderr.write("API error: %r\n" % (e,))
            self.reply(500, {"error": "Ошибка сервера"})

    def reply(self, code, obj, headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def client_ip(self):
        return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()

    def cookie_token(self):
        c = cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except cookies.CookieError:
            return None
        m = c.get(COOKIE)
        return m.value if m else None

    def current_user(self, required=True):
        tok = self.cookie_token()
        if tok:
            with db_lock, db() as c:
                row = c.execute(
                    "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                    "WHERE s.token=? AND s.expires>?",
                    (token_id(tok), int(time.time())),
                ).fetchone()
                if row:
                    c.execute("UPDATE users SET seen=? WHERE id=?", (int(time.time()), row["id"]))
                    return row
        if required:
            raise ApiError(401, "Нужно войти")
        return None

    def new_session(self, c, user_id):
        tok = secrets.token_urlsafe(32)
        exp = int(time.time()) + SESSION_DAYS * 86400
        c.execute("INSERT INTO sessions(token,user_id,expires) VALUES(?,?,?)", (token_id(tok), user_id, exp))
        c.execute("DELETE FROM sessions WHERE expires<?", (int(time.time()),))
        cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (COOKIE, tok, SESSION_DAYS * 86400)
        return [("Set-Cookie", cookie)]

    def check_rate(self):
        ip = self.client_ip()
        now = time.time()
        recent = [t for t in fail_log.get(ip, []) if now - t < 600]
        fail_log[ip] = recent
        if len(recent) >= 10:
            raise ApiError(429, "Слишком много попыток. Подожди 10 минут")

    def note_fail(self):
        fail_log.setdefault(self.client_ip(), []).append(time.time())

    @staticmethod
    def need_post(method):
        if method != "POST":
            raise ApiError(405, "Нужен POST")

    # --- аккаунт ---
    def api_ping(self, method, body):
        return 200, {"ok": True}, None

    def api_register(self, method, body):
        self.need_post(method)
        self.check_rate()
        login = str(body.get("login", "")).strip()
        name = str(body.get("name", "")).strip()[:32] or login
        pw = str(body.get("password", ""))
        if not LOGIN_RE.match(login):
            raise ApiError(400, "Логин: 3–24 символа, буквы, цифры, точка, дефис или _")
        if len(pw) < 6 or len(pw) > 200:
            raise ApiError(400, "Пароль должен быть не короче 6 символов")
        salt = secrets.token_bytes(16)
        h = hash_pw(pw, salt)
        now = int(time.time())
        with db_lock, db() as c:
            if c.execute("SELECT 1 FROM users WHERE lkey=?", (login.casefold(),)).fetchone():
                self.note_fail()
                raise ApiError(409, "Такой логин уже занят")
            cur = c.execute(
                "INSERT INTO users(login,lkey,name,salt,hash,created,seen) VALUES(?,?,?,?,?,?,?)",
                (login, login.casefold(), name, salt, h, now, now),
            )
            hdr = self.new_session(c, cur.lastrowid)
            row = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        return 200, {"user": public_user(row), "fresh": True}, hdr

    def api_login(self, method, body):
        self.need_post(method)
        self.check_rate()
        login = str(body.get("login", "")).strip()
        pw = str(body.get("password", ""))
        with db_lock, db() as c:
            row = c.execute("SELECT * FROM users WHERE lkey=?", (login.casefold(),)).fetchone()
            if not row or not hmac.compare_digest(hash_pw(pw, row["salt"]), row["hash"]):
                self.note_fail()
                raise ApiError(401, "Неверный логин или пароль")
            hdr = self.new_session(c, row["id"])
            has_data = c.execute("SELECT COUNT(*) FROM kv WHERE user_id=?", (row["id"],)).fetchone()[0] > 0
        return 200, {"user": public_user(row), "hasData": has_data}, hdr

    def api_logout(self, method, body):
        self.need_post(method)
        tok = self.cookie_token()
        if tok:
            with db_lock, db() as c:
                c.execute("DELETE FROM sessions WHERE token=?", (token_id(tok),))
        return 200, {"ok": True}, [("Set-Cookie", "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % COOKIE)]

    def api_me(self, method, body):
        u = self.current_user(required=False)
        return 200, {"user": public_user(u) if u else None}, None

    def api_profile(self, method, body):
        self.need_post(method)
        u = self.current_user()
        name = str(body.get("name", "")).strip()[:32]
        if not name:
            raise ApiError(400, "Имя не может быть пустым")
        with db_lock, db() as c:
            c.execute("UPDATE users SET name=? WHERE id=?", (name, u["id"]))
            row = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        return 200, {"user": public_user(row)}, None

    def api_password(self, method, body):
        self.need_post(method)
        u = self.current_user()
        old = str(body.get("old", ""))
        new = str(body.get("new", ""))
        if not hmac.compare_digest(hash_pw(old, u["salt"]), u["hash"]):
            raise ApiError(403, "Старый пароль не подходит")
        if len(new) < 6 or len(new) > 200:
            raise ApiError(400, "Новый пароль должен быть не короче 6 символов")
        salt = secrets.token_bytes(16)
        tok = self.cookie_token()
        with db_lock, db() as c:
            c.execute("UPDATE users SET salt=?, hash=? WHERE id=?", (salt, hash_pw(new, salt), u["id"]))
            # выходим на всех других устройствах
            c.execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (u["id"], token_id(tok or "")))
        return 200, {"ok": True}, None

    def api_delete_account(self, method, body):
        self.need_post(method)
        u = self.current_user()
        if not hmac.compare_digest(hash_pw(str(body.get("password", "")), u["salt"]), u["hash"]):
            raise ApiError(403, "Пароль не подходит")
        with db_lock, db() as c:
            c.execute("DELETE FROM kv WHERE user_id=?", (u["id"],))
            c.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
            c.execute("DELETE FROM users WHERE id=?", (u["id"],))
        return 200, {"ok": True}, [("Set-Cookie", "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % COOKIE)]

    # --- сохранения ---
    def api_data(self, method, body):
        u = self.current_user()
        if method == "GET":
            with db_lock, db() as c:
                rows = c.execute("SELECT k,v FROM kv WHERE user_id=?", (u["id"],)).fetchall()
            return 200, {"data": {r["k"]: r["v"] for r in rows}}, None
        sets = body.get("set") or {}
        dels = body.get("del") or []
        if not isinstance(sets, dict) or not isinstance(dels, list):
            raise ApiError(400, "Неверный запрос")
        now = int(time.time())
        with db_lock, db() as c:
            if body.get("replace"):
                c.execute("DELETE FROM kv WHERE user_id=?", (u["id"],))
            for k, v in sets.items():
                if not isinstance(v, str) or len(k) > 200:
                    continue
                c.execute(
                    "INSERT INTO kv(user_id,k,v,updated) VALUES(?,?,?,?) "
                    "ON CONFLICT(user_id,k) DO UPDATE SET v=excluded.v, updated=excluded.updated",
                    (u["id"], k, v, now),
                )
            for k in dels:
                c.execute("DELETE FROM kv WHERE user_id=? AND k=?", (u["id"], str(k)))
            total = c.execute("SELECT COALESCE(SUM(LENGTH(v)),0) FROM kv WHERE user_id=?", (u["id"],)).fetchone()[0]
            if total > MAX_USER_BYTES:
                raise ApiError(413, "Сохранений слишком много для одного аккаунта")
        return 200, {"ok": True}, None

    # --- рейтинг ---
    def api_stats(self, method, body):
        self.need_post(method)
        u = self.current_user()
        stats = body.get("stats")
        if not isinstance(stats, dict):
            raise ApiError(400, "Неверный запрос")
        raw = json.dumps(stats, ensure_ascii=False)[:20000]
        try:
            score = max(0, min(10 ** 7, int(body.get("score", 0))))
        except (TypeError, ValueError):
            score = 0
        with db_lock, db() as c:
            c.execute("UPDATE users SET stats=?, score=? WHERE id=?", (raw, score, u["id"]))
        return 200, {"ok": True}, None

    def api_leaders(self, method, body):
        with db_lock, db() as c:
            rows = c.execute(
                "SELECT login,name,score,stats FROM users WHERE score>0 ORDER BY score DESC, created ASC LIMIT 50"
            ).fetchall()
            players = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return 200, {
            "players": players,
            "top": [
                {"login": r["login"], "name": r["name"], "score": r["score"], "stats": json.loads(r["stats"] or "{}")}
                for r in rows
            ],
        }, None


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8000))
    init_db()
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("Игротека запущена: http://localhost:%d  (Ctrl+C — остановить)" % port, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
