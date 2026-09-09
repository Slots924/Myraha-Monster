"""Myraha Monster: Meta Page webhook moderation dashboard."""

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DB_PATH = ROOT / "myraha.db"
MAX_BODY_SIZE = 1024 * 1024


def load_env_file(filename=ROOT / ".env"):
    try:
        with open(filename, encoding="utf-8-sig") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


load_env_file()
PORT = int(os.getenv("PORT", "80"))
VERIFY_TOKEN = os.getenv("FACEBOOK_VERIFY_TOKEN", "")
APP_SECRET = os.getenv("FACEBOOK_APP_SECRET", "")
# Keep compatibility with the typo already used in the local .env.
SYSTEM_USER_TOKEN = os.getenv("SYSTEM_USER_TOKEN") or os.getenv("SUSTEM_USER_TOKEN", "")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v24.0")
BUSINESS_ID = os.getenv("FACEBOOK_BUSINESS_ID", "")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connection():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_database():
    with db_connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pages (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT DEFAULT '',
                picture_url TEXT DEFAULT '',
                followers_count INTEGER DEFAULT 0,
                access_token TEXT DEFAULT '',
                subscribed INTEGER DEFAULT 0,
                note TEXT DEFAULT '',
                geo TEXT DEFAULT '',
                language TEXT DEFAULT '',
                creative_name TEXT DEFAULT '',
                page_url TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                archived INTEGER DEFAULT 0,
                last_sync_at TEXT,
                last_error TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS whitelist (
                user_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS comments (
                id TEXT PRIMARY KEY,
                page_id TEXT NOT NULL,
                post_id TEXT DEFAULT '',
                author_id TEXT DEFAULT '',
                author_name TEXT DEFAULT '',
                message TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'received',
                is_hidden INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT DEFAULT '',
                raw_json TEXT DEFAULT '',
                FOREIGN KEY(page_id) REFERENCES pages(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS comments_received_idx
                ON comments(received_at DESC);
            CREATE INDEX IF NOT EXISTS comments_page_idx
                ON comments(page_id, received_at DESC);
            """
        )
        page_columns = {row["name"] for row in db.execute("PRAGMA table_info(pages)")}
        for name, definition in {
            "note": "TEXT DEFAULT ''",
            "geo": "TEXT DEFAULT ''",
            "language": "TEXT DEFAULT ''",
            "creative_name": "TEXT DEFAULT ''",
            "page_url": "TEXT DEFAULT ''",
            "sort_order": "INTEGER DEFAULT 0",
            "archived": "INTEGER DEFAULT 0",
        }.items():
            if name not in page_columns:
                db.execute(f"ALTER TABLE pages ADD COLUMN {name} {definition}")
        unordered = db.execute(
            "SELECT id FROM pages WHERE sort_order IS NULL OR sort_order=0 ORDER BY rowid"
        ).fetchall()
        next_order = db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM pages").fetchone()[0]
        for row in unordered:
            next_order += 1
            db.execute("UPDATE pages SET sort_order=? WHERE id=?", (next_order, row["id"]))
        db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('auto_hide', '0')")
        db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('skip_page_comments', '1')")
        db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('whitelist_enabled', '0')")


class MetaAPIError(RuntimeError):
    def __init__(self, message, status=0, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


def meta_request(path, token, method="GET", params=None):
    if not token:
        raise MetaAPIError("У токена немає значення. Перевір .env")
    params = dict(params or {})
    params["access_token"] = token
    url = f"{GRAPH_BASE}/{path.lstrip('/')}"
    data = None
    if method == "GET":
        url += "?" + urlencode(params)
    else:
        data = urlencode(params).encode("utf-8")
    request = Request(url, data=data, method=method, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            message = payload.get("error", {}).get("message", str(exc))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
            message = str(exc)
        raise MetaAPIError(message, exc.code, payload) from exc
    except URLError as exc:
        raise MetaAPIError(f"Meta API недоступний: {exc.reason}") from exc


def collect_paginated(path, token, params=None, limit=500):
    result = meta_request(path, token, params=params)
    items = list(result.get("data", []))
    next_url = result.get("paging", {}).get("next")
    while next_url and len(items) < limit:
        request = Request(next_url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, json.JSONDecodeError) as exc:
            raise MetaAPIError(f"Не вдалося дочитати список сторінок: {exc}") from exc
        items.extend(result.get("data", []))
        next_url = result.get("paging", {}).get("next")
    return items[:limit]


def page_fields():
    return "id,name,category,link,picture.type(square){url},followers_count,access_token"


def sync_pages_from_meta():
    if not SYSTEM_USER_TOKEN:
        raise MetaAPIError("Додай SYSTEM_USER_TOKEN або SUSTEM_USER_TOKEN у .env")
    candidates, errors = [], []
    sources = [("me/accounts", {"fields": page_fields(), "limit": 100})]
    if BUSINESS_ID:
        sources.extend([
            (f"{BUSINESS_ID}/owned_pages", {"fields": page_fields(), "limit": 100}),
            (f"{BUSINESS_ID}/client_pages", {"fields": page_fields(), "limit": 100}),
        ])
    for path, params in sources:
        try:
            candidates.extend(collect_paginated(path, SYSTEM_USER_TOKEN, params))
        except MetaAPIError as exc:
            errors.append(str(exc))
    configured_ids = [value.strip() for value in os.getenv("FACEBOOK_PAGE_IDS", "").split(",") if value.strip()]
    for page_id in configured_ids:
        try:
            candidates.append(meta_request(page_id, SYSTEM_USER_TOKEN, params={"fields": page_fields()}))
        except MetaAPIError as exc:
            errors.append(f"{page_id}: {exc}")

    unique_pages = {str(page["id"]): page for page in candidates if page.get("id")}
    synced_at = utc_now()
    with db_connection() as db:
        next_order = db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM pages").fetchone()[0]
        for page in unique_pages.values():
            next_order += 1
            picture_url = page.get("picture", {}).get("data", {}).get("url", "")
            db.execute(
                """
                INSERT INTO pages(id, name, category, picture_url, followers_count,
                                  access_token, page_url, sort_order, last_sync_at, last_error)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, category=excluded.category,
                    picture_url=excluded.picture_url, followers_count=excluded.followers_count,
                    page_url=CASE WHEN excluded.page_url != '' THEN excluded.page_url ELSE pages.page_url END,
                    access_token=CASE WHEN excluded.access_token != '' THEN excluded.access_token ELSE pages.access_token END,
                    last_sync_at=excluded.last_sync_at, last_error=''
                """,
                (str(page["id"]), page.get("name", f"Page {page['id']}"), page.get("category", ""),
                 picture_url, int(page.get("followers_count") or 0), page.get("access_token", ""),
                 page.get("link", ""), next_order, synced_at),
            )
    if not unique_pages and errors:
        raise MetaAPIError(" · ".join(dict.fromkeys(errors)))
    return {"count": len(unique_pages), "warnings": list(dict.fromkeys(errors))}


def get_setting(key, default=""):
    with db_connection() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db_connection() as db:
        db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def list_pages(archived=False):
    with db_connection() as db:
        rows = db.execute(
            """
            SELECT p.id, p.name, p.category, p.picture_url, p.followers_count,
                   p.subscribed, p.note, p.geo, p.language, p.creative_name,
                   p.page_url, p.sort_order, p.last_sync_at, p.last_error,
                   COUNT(c.id) AS comment_count, COALESCE(SUM(c.is_hidden), 0) AS hidden_count
            FROM pages p LEFT JOIN comments c ON c.page_id=p.id
            WHERE p.archived=?
            GROUP BY p.id
            ORDER BY CASE WHEN p.geo='' THEN 1 ELSE 0 END, p.geo, p.name COLLATE NOCASE
            """, (1 if archived else 0,)
        ).fetchall()
    return [dict(row) for row in rows]


def list_whitelist():
    with db_connection() as db:
        return [row["user_id"] for row in db.execute("SELECT user_id FROM whitelist ORDER BY rowid")]


def replace_whitelist(user_ids):
    if not isinstance(user_ids, list):
        raise ValueError("Список ID має бути масивом")
    cleaned = []
    for raw_id in user_ids:
        user_id = str(raw_id).strip()
        if not user_id:
            continue
        if not user_id.isdigit() or len(user_id) > 64:
            raise ValueError(f"Некоректний Facebook User ID: {user_id[:40]}")
        if user_id not in cleaned:
            cleaned.append(user_id)
    if len(cleaned) > 10000:
        raise ValueError("Максимум 10 000 ID у білому списку")
    with db_connection() as db:
        db.execute("DELETE FROM whitelist")
        db.executemany(
            "INSERT INTO whitelist(user_id, created_at) VALUES(?, ?)",
            [(user_id, utc_now()) for user_id in cleaned],
        )
    return cleaned


def normalize_code(value, field_name):
    value = str(value or "").strip().upper()
    if value and (not value.isalpha() or len(value) not in {2, 3}):
        raise ValueError(f"{field_name} має бути кодом із 2–3 латинських літер")
    return value


def update_page_details(page_id, note, geo, language, creative_name):
    note = str(note or "").strip()
    creative_name = str(creative_name or "").strip()
    geo = normalize_code(geo, "GEO")
    language = normalize_code(language, "Мова")
    if len(note) > 160:
        raise ValueError("Примітка може містити максимум 160 символів")
    if len(creative_name) > 120:
        raise ValueError("Назва креативу може містити максимум 120 символів")
    with db_connection() as db:
        page = db.execute("SELECT geo FROM pages WHERE id=?", (page_id,)).fetchone()
        if not page:
            raise ValueError("Сторінку не знайдено")
        sort_order = None
        if geo != page["geo"]:
            sort_order = db.execute(
                "SELECT COALESCE(MAX(sort_order), 0)+1 FROM pages WHERE geo=?", (geo,)
            ).fetchone()[0]
        if sort_order is None:
            db.execute(
                "UPDATE pages SET note=?, geo=?, language=?, creative_name=? WHERE id=?",
                (note, geo, language, creative_name, page_id),
            )
        else:
            db.execute(
                "UPDATE pages SET note=?, geo=?, language=?, creative_name=?, sort_order=? WHERE id=?",
                (note, geo, language, creative_name, sort_order, page_id),
            )


def dashboard_state():
    with db_connection() as db:
        stats = dict(db.execute(
            """
            SELECT COUNT(*) AS total, COALESCE(SUM(is_hidden), 0) AS hidden,
                   COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END), 0) AS errors,
                   COALESCE(SUM(CASE WHEN received_at >= datetime('now', '-1 day') THEN 1 ELSE 0 END), 0) AS today
            FROM comments
            """
        ).fetchone())
    return {
        "auto_hide": get_setting("auto_hide", "0") == "1",
        "skip_page_comments": get_setting("skip_page_comments", "1") == "1",
        "whitelist_enabled": get_setting("whitelist_enabled", "0") == "1",
        "whitelist_count": len(list_whitelist()),
        "token_configured": bool(SYSTEM_USER_TOKEN), "graph_version": GRAPH_API_VERSION,
        "pages": list_pages(), "archived_pages": list_pages(archived=True), "stats": stats,
    }


def set_page_subscription(page_id, enabled):
    with db_connection() as db:
        page = db.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
    if not page:
        raise ValueError("Сторінку не знайдено. Спочатку синхронізуй список")
    token = page["access_token"] or SYSTEM_USER_TOKEN
    try:
        if enabled:
            meta_request(f"{quote(page_id)}/subscribed_apps", token, method="POST", params={"subscribed_fields": "feed"})
        else:
            meta_request(f"{quote(page_id)}/subscribed_apps", token, method="DELETE")
    except MetaAPIError as exc:
        with db_connection() as db:
            db.execute("UPDATE pages SET last_error=? WHERE id=?", (str(exc), page_id))
        raise
    with db_connection() as db:
        db.execute("UPDATE pages SET subscribed=?, last_error='' WHERE id=?", (1 if enabled else 0, page_id))


def archive_page(page_id):
    with db_connection() as db:
        page = db.execute("SELECT subscribed FROM pages WHERE id=?", (page_id,)).fetchone()
    if not page:
        raise ValueError("Сторінку не знайдено")
    if page["subscribed"]:
        set_page_subscription(page_id, False)
    with db_connection() as db:
        db.execute("UPDATE pages SET archived=1, subscribed=0 WHERE id=?", (page_id,))


def restore_page(page_id):
    with db_connection() as db:
        changed = db.execute("UPDATE pages SET archived=0 WHERE id=?", (page_id,)).rowcount
    if not changed:
        raise ValueError("Сторінку не знайдено")


def update_comment_visibility(comment_id, hidden):
    with db_connection() as db:
        comment = db.execute(
            "SELECT c.*, p.access_token FROM comments c JOIN pages p ON p.id=c.page_id WHERE c.id=?",
            (comment_id,),
        ).fetchone()
    if not comment:
        raise ValueError("Коментар не знайдено")
    token = comment["access_token"] or SYSTEM_USER_TOKEN
    with db_connection() as db:
        db.execute("UPDATE comments SET status=?, error='', updated_at=? WHERE id=?",
                   ("hiding" if hidden else "unhiding", utc_now(), comment_id))
    try:
        result = meta_request(quote(comment_id, safe="_"), token, method="POST",
                              params={"is_hidden": "true" if hidden else "false"})
        if result.get("success") is False:
            raise MetaAPIError("Meta не підтвердила зміну видимості")
    except MetaAPIError as exc:
        with db_connection() as db:
            db.execute("UPDATE comments SET status='error', error=?, updated_at=? WHERE id=?",
                       (str(exc), utc_now(), comment_id))
        raise
    with db_connection() as db:
        db.execute("UPDATE comments SET status=?, is_hidden=?, error='', updated_at=? WHERE id=?",
                   ("hidden" if hidden else "visible", 1 if hidden else 0, utc_now(), comment_id))


def save_webhook_comment(page_id, value, raw_payload):
    comment_id = str(value.get("comment_id") or value.get("id") or "")
    if not comment_id:
        return None
    author = value.get("from") or {}
    created = value.get("created_time")
    if isinstance(created, (int, float)):
        created = datetime.fromtimestamp(created, timezone.utc).isoformat(timespec="seconds")
    created, now = created or utc_now(), utc_now()
    author_id = str(author.get("id") or "")
    should_hide = get_setting("auto_hide", "0") == "1"
    whitelist_enabled = get_setting("whitelist_enabled", "0") == "1"
    with db_connection() as db:
        whitelisted = bool(author_id and db.execute(
            "SELECT 1 FROM whitelist WHERE user_id=?", (author_id,)
        ).fetchone())
    if whitelist_enabled and whitelisted:
        status, should_hide = "whitelisted", False
    elif author_id == str(page_id) and get_setting("skip_page_comments", "1") == "1":
        status, should_hide = "skipped", False
    else:
        status = "queued" if should_hide else "visible"
    with db_connection() as db:
        db.execute("INSERT OR IGNORE INTO pages(id, name) VALUES(?, ?)", (page_id, f"Page {page_id}"))
        db.execute(
            """
            INSERT INTO comments(id, page_id, post_id, author_id, author_name, message, status,
                                 is_hidden, created_at, received_at, updated_at, raw_json)
            VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET message=excluded.message, author_name=excluded.author_name,
                updated_at=excluded.updated_at, raw_json=excluded.raw_json
            """,
            (comment_id, str(page_id), str(value.get("post_id") or value.get("parent_id") or ""), author_id,
             author.get("name", "Невідомий користувач"), value.get("message", ""), status,
             created, now, now, json.dumps(raw_payload, ensure_ascii=False)),
        )
    return comment_id if should_hide else None


def process_webhook_payload(payload):
    if payload.get("object") != "page":
        return
    for entry in payload.get("entry", []):
        page_id = str(entry.get("id") or "")
        if not page_id:
            continue
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            if change.get("field") != "feed" or value.get("item") != "comment":
                continue
            if value.get("verb") not in (None, "add", "edited"):
                continue
            comment_id = save_webhook_comment(page_id, value, payload)
            if comment_id:
                threading.Thread(target=hide_comment_safely, args=(comment_id,), daemon=True,
                                 name=f"hide-{comment_id}").start()


def hide_comment_safely(comment_id):
    try:
        update_comment_visibility(comment_id, True)
    except (MetaAPIError, ValueError) as exc:
        print(f"Could not hide comment {comment_id}: {exc}")


def list_comments(query):
    page_id, status, search = query.get("page_id", ""), query.get("status", "all"), query.get("search", "").strip()
    try:
        limit = min(max(int(query.get("limit", 100)), 1), 300)
    except ValueError:
        limit = 100
    conditions, params = [], []
    if page_id:
        conditions.append("c.page_id=?"); params.append(page_id)
    if status == "hidden":
        conditions.append("c.is_hidden=1")
    elif status == "visible":
        conditions.append("c.is_hidden=0 AND c.status NOT IN ('error', 'skipped')")
    elif status == "error":
        conditions.append("c.status='error'")
    if search:
        conditions.append("(c.message LIKE ? OR c.author_name LIKE ? OR p.name LIKE ?)")
        pattern = f"%{search}%"; params.extend([pattern, pattern, pattern])
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    with db_connection() as db:
        rows = db.execute(
            f"""
            SELECT c.id, c.page_id, p.name AS page_name, p.picture_url, c.post_id, c.author_id,
                   c.author_name, c.message, c.status, c.is_hidden, c.created_at, c.received_at,
                   c.updated_at, c.error
            FROM comments c JOIN pages p ON p.id=c.page_id {where}
            ORDER BY c.received_at DESC LIMIT ?
            """, (*params, limit)).fetchall()
    return [dict(row) for row in rows]


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "MyrahaMonster/1.0"

    def log_message(self, format_string, *args):
        print(f"[{utc_now()}] {self.address_string()} {format_string % args}")

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "public, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers(); self.wfile.write(body)

    def send_json(self, status, payload):
        self.send_bytes(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def send_text(self, status, text):
        self.send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def dashboard_authorized(self):
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        if not DASHBOARD_PASSWORD:
            return False
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(username, "monster") and hmac.compare_digest(password, DASHBOARD_PASSWORD)

    def require_dashboard_auth(self):
        if self.dashboard_authorized():
            return True
        body = "Myraha Monster dashboard requires authorization".encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Myraha Monster", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
        return False

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_SIZE:
            raise ValueError("Тіло запиту завелике")
        raw_body = self.rfile.read(length)
        return (json.loads(raw_body.decode("utf-8")) if raw_body else {}), raw_body

    def do_GET(self):
        parsed = urlparse(self.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        if parsed.path == "/webhook":
            mode, token, challenge = query.get("hub.mode"), query.get("hub.verify_token"), query.get("hub.challenge")
            if mode == "subscribe" and token and challenge and hmac.compare_digest(token, VERIFY_TOKEN):
                self.send_text(200, challenge)
            else:
                self.send_text(403, "Verification token mismatch")
            return
        if not self.require_dashboard_auth():
            return
        if parsed.path == "/api/state":
            self.send_json(200, dashboard_state()); return
        if parsed.path == "/api/comments":
            self.send_json(200, {"comments": list_comments(query)}); return
        if parsed.path == "/api/whitelist":
            self.send_json(200, {"user_ids": list_whitelist()}); return
        if parsed.path == "/api/health":
            self.send_json(200, {"ok": True, "time": utc_now(), "token_configured": bool(SYSTEM_USER_TOKEN)}); return
        self.serve_static(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            data, raw_body = self.read_json()
            if parsed.path == "/webhook":
                if APP_SECRET:
                    received = self.headers.get("X-Hub-Signature-256", "")
                    expected = "sha256=" + hmac.new(APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(received, expected):
                        self.send_text(401, "Invalid signature"); return
                process_webhook_payload(data)
                self.send_text(200, "EVENT_RECEIVED"); return
            if not self.require_dashboard_auth():
                return
            if parsed.path == "/api/pages/sync":
                result = sync_pages_from_meta()
                self.send_json(200, {"ok": True, **result, "state": dashboard_state()}); return
            if parsed.path == "/api/settings":
                for key in {"auto_hide", "skip_page_comments", "whitelist_enabled"}.intersection(data):
                    set_setting(key, "1" if bool(data[key]) else "0")
                self.send_json(200, {"ok": True, "state": dashboard_state()}); return
            if parsed.path == "/api/whitelist":
                user_ids = replace_whitelist(data.get("user_ids", []))
                self.send_json(200, {"ok": True, "user_ids": user_ids, "state": dashboard_state()}); return
            if parsed.path.startswith("/api/pages/") and parsed.path.endswith("/details"):
                page_id = parsed.path.split("/")[3]
                update_page_details(
                    page_id, data.get("note", ""), data.get("geo", ""),
                    data.get("language", ""), data.get("creative_name", ""),
                )
                self.send_json(200, {"ok": True, "state": dashboard_state()}); return
            if parsed.path.startswith("/api/pages/") and parsed.path.endswith("/subscription"):
                page_id = parsed.path.split("/")[3]
                set_page_subscription(page_id, bool(data.get("enabled")))
                self.send_json(200, {"ok": True, "state": dashboard_state()}); return
            if parsed.path.startswith("/api/pages/") and parsed.path.endswith("/archive"):
                page_id = parsed.path.split("/")[3]
                archive_page(page_id)
                self.send_json(200, {"ok": True, "state": dashboard_state()}); return
            if parsed.path.startswith("/api/pages/") and parsed.path.endswith("/restore"):
                page_id = parsed.path.split("/")[3]
                restore_page(page_id)
                self.send_json(200, {"ok": True, "state": dashboard_state()}); return
            if parsed.path.startswith("/api/comments/") and parsed.path.endswith("/visibility"):
                comment_id = parsed.path[len("/api/comments/"):-len("/visibility")].strip("/")
                update_comment_visibility(comment_id, bool(data.get("hidden")))
                self.send_json(200, {"ok": True}); return
            self.send_json(404, {"error": "Endpoint не знайдено"})
        except (MetaAPIError, ValueError, json.JSONDecodeError) as exc:
            status = exc.status if isinstance(exc, MetaAPIError) and 400 <= exc.status < 600 else 400
            self.send_json(status, {"error": str(exc)})
        except Exception as exc:
            print(f"Unhandled request error: {exc}")
            self.send_json(500, {"error": "Внутрішня помилка сервера"})

    def serve_static(self, request_path):
        relative = "index.html" if request_path in ("", "/") else request_path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_text(403, "Forbidden"); return
        if not target.is_file():
            target = STATIC_DIR / "index.html"
        try:
            body = target.read_bytes()
        except FileNotFoundError:
            self.send_text(404, "Dashboard files are missing"); return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_bytes(200, body, content_type)


if __name__ == "__main__":
    initialize_database()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"Myraha Monster is live at http://localhost:{PORT}")
    print("Meta callback URL:", os.getenv("WEBHOOK_CALLBACK_URL", "set WEBHOOK_CALLBACK_URL in .env"))
    print("System user token:", "configured" if SYSTEM_USER_TOKEN else "missing")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")
    finally:
        server.server_close()
