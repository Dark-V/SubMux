from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import time
from urllib.parse import parse_qsl, urlsplit
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl, field_validator

APP_NAME = "SubMux"
ADMIN_PORT = int(os.getenv("ADMIN_PORT", os.getenv("PORT", "8080")))
PUBLIC_PORT = int(os.getenv("PUBLIC_PORT", "8081"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "submux.db"
ADMIN_TOKEN_FILE = DATA_DIR / "admin_token"
COOKIE_NAME = "submux_admin"
COOKIE_SECURE_MODE = os.getenv("COOKIE_SECURE", "auto").strip().lower()
CACHE_TTL_SECONDS = max(60, int(os.getenv("CACHE_TTL_SECONDS", "1800")))
BASE_DIR = Path(__file__).resolve().parent

_SOURCE_LOCKS: dict[int, asyncio.Lock] = {}
_BUNDLE_LOCKS: dict[int, asyncio.Lock] = {}
_CACHE_REFRESH_TASK: asyncio.Task | None = None

FORWARDED_HEADERS = (
    "Content-Type",
    "Content-Disposition",
    "Subscription-Userinfo",
    "Profile-Update-Interval",
    "Profile-Title",
)

DATA_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    with db() as con:
        con.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                user_agent TEXT NOT NULL,
                hwid TEXT NOT NULL,
                device_os TEXT NOT NULL DEFAULT 'Android',
                ver_os TEXT NOT NULL DEFAULT '',
                device_model TEXT NOT NULL DEFAULT '',
                app_version TEXT NOT NULL DEFAULT '',
                format TEXT NOT NULL DEFAULT 'auto',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                scope_all INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS token_subscriptions (
                token_id INTEGER NOT NULL REFERENCES tokens(id) ON DELETE CASCADE,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                PRIMARY KEY(token_id, subscription_id)
            );
            CREATE TABLE IF NOT EXISTS subscription_cache (
                subscription_id INTEGER PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
                config_hash TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                status INTEGER NOT NULL,
                body BLOB NOT NULL,
                headers_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bundle_cache (
                token_id INTEGER PRIMARY KEY REFERENCES tokens(id) ON DELETE CASCADE,
                fingerprint TEXT NOT NULL,
                generated_at INTEGER NOT NULL,
                status INTEGER NOT NULL,
                body BLOB NOT NULL,
                headers_json TEXT NOT NULL
            );
            """
        )


def get_admin_token() -> str:
    env_token = os.getenv("ADMIN_TOKEN", "").strip()
    if env_token:
        return env_token
    if ADMIN_TOKEN_FILE.exists():
        value = ADMIN_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(32)
    ADMIN_TOKEN_FILE.write_text(value, encoding="utf-8")
    try:
        os.chmod(ADMIN_TOKEN_FILE, 0o600)
    except OSError:
        pass
    print(f"[SubMux] ADMIN_TOKEN was not set. Generated persistent token: {value}", flush=True)
    return value


init_db()
ADMIN_TOKEN = get_admin_token()
ADMIN_COOKIE_VALUE = hashlib.sha256(f"submux:{ADMIN_TOKEN}".encode()).hexdigest()


def legacy_import() -> None:
    url = os.getenv("UPSTREAM_URL", "").strip()
    if not url:
        return
    with db() as con:
        count = con.execute("SELECT COUNT(*) AS c FROM subscriptions").fetchone()["c"]
        if count:
            return
        name = slugify(os.getenv("INITIAL_SUB_NAME", "default"))
        ts = now_iso()
        con.execute(
            """
            INSERT INTO subscriptions
            (name, url, user_agent, hwid, device_os, ver_os, device_model, app_version, format, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'auto', 1, ?, ?)
            """,
            (
                name,
                url,
                os.getenv("USER_AGENT", "v2raytun/android"),
                os.getenv("HWID", ""),
                os.getenv("DEVICE_OS", "Android"),
                os.getenv("VER_OS", ""),
                os.getenv("DEVICE_MODEL", ""),
                os.getenv("APP_VERSION", ""),
                ts,
                ts,
            ),
        )
        print(f"[SubMux] Imported legacy UPSTREAM_URL as subscription '{name}'", flush=True)


class SubscriptionIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    url: HttpUrl
    user_agent: str = Field(default="v2raytun/android", min_length=1, max_length=512)
    hwid: str = Field(default="", max_length=512)
    device_os: str = Field(default="Android", max_length=128)
    ver_os: str = Field(default="", max_length=128)
    device_model: str = Field(default="", max_length=256)
    app_version: str = Field(default="", max_length=128)
    format: str = "auto"
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = slugify(value)
        if not value:
            raise ValueError("name must contain letters, digits, _ or -")
        return value

    @field_validator("format")
    @classmethod
    def validate_format(cls, value: str) -> str:
        if value not in {"auto", "base64", "raw"}:
            raise ValueError("format must be auto, base64 or raw")
        return value


class TokenIn(BaseModel):
    label: str = Field(default="access", min_length=1, max_length=128)
    scope_all: bool = True
    subscription_ids: list[int] = Field(default_factory=list)


class TokenPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    scope_all: bool | None = None
    subscription_ids: list[int] | None = None


def slugify(value: str) -> str:
    # Keep Unicode letters/digits (including Cyrillic), plus "_" and "-".
    # FastAPI/clients will percent-encode non-ASCII names in URLs as needed.
    return re.sub(r"[^\w-]+", "-", value.strip(), flags=re.UNICODE).strip("-").lower()[:64]


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if "enabled" in d:
        d["enabled"] = bool(d["enabled"])
    if "scope_all" in d:
        d["scope_all"] = bool(d["scope_all"])
    return d


def admin_authenticated(request: Request) -> bool:
    value = request.cookies.get(COOKIE_NAME, "")
    return hmac.compare_digest(value, ADMIN_COOKIE_VALUE)


def cookie_secure_for(request: Request) -> bool:
    if COOKIE_SECURE_MODE in {"1", "true", "yes", "on"}:
        return True
    if COOKIE_SECURE_MODE in {"0", "false", "no", "off"}:
        return False

    # Auto mode: honor the reverse proxy scheme first, otherwise use
    # FastAPI's request scheme. This lets direct HTTP logins work while
    # keeping the cookie Secure when the public endpoint is HTTPS.
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if forwarded_proto:
        return forwarded_proto.split(",", 1)[0].strip().lower() == "https"
    return request.url.scheme.lower() == "https"


def require_admin(request: Request) -> None:
    if not admin_authenticated(request):
        raise HTTPException(status_code=401, detail="Admin authentication required")


def get_subscription_headers(sub: dict[str, Any]) -> dict[str, str]:
    headers = {
        "User-Agent": sub["user_agent"],
        "X-HWID": sub["hwid"],
        "X-Device-OS": sub["device_os"],
        "X-Ver-OS": sub["ver_os"],
        "X-Device-Model": sub["device_model"],
        "X-App-Version": sub["app_version"],
    }
    return {k: v for k, v in headers.items() if v != ""}


def get_token_record(token_value: str) -> dict[str, Any] | None:
    with db() as con:
        row = con.execute("SELECT * FROM tokens WHERE token = ? AND enabled = 1", (token_value,)).fetchone()
        return row_dict(row) if row else None


def extract_token(request: Request, path_token: str | None = None) -> str:
    if path_token:
        return path_token
    query_token = request.query_params.get("token")
    if query_token:
        return query_token
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    raise HTTPException(status_code=401, detail="Missing access token")


def allowed_subscriptions(token: dict[str, Any]) -> list[dict[str, Any]]:
    with db() as con:
        if token["scope_all"]:
            rows = con.execute("SELECT * FROM subscriptions WHERE enabled = 1 ORDER BY id").fetchall()
        else:
            rows = con.execute(
                """
                SELECT s.* FROM subscriptions s
                JOIN token_subscriptions ts ON ts.subscription_id = s.id
                WHERE ts.token_id = ? AND s.enabled = 1
                ORDER BY s.id
                """,
                (token["id"],),
            ).fetchall()
        return [row_dict(r) for r in rows]


async def fetch_one(client: httpx.AsyncClient, sub: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await client.get(sub["url"], headers=get_subscription_headers(sub))
        return {
            "sub": sub,
            "ok": 200 <= response.status_code < 300,
            "status": response.status_code,
            "body": response.content,
            "headers": dict(response.headers),
            "error": None,
        }
    except Exception as exc:
        return {"sub": sub, "ok": False, "status": 502, "body": b"", "headers": {}, "error": str(exc)}


def subscription_config_hash(sub: dict[str, Any]) -> str:
    payload = {
        "url": sub["url"],
        "user_agent": sub["user_agent"],
        "hwid": sub["hwid"],
        "device_os": sub["device_os"],
        "ver_os": sub["ver_os"],
        "device_model": sub["device_model"],
        "app_version": sub["app_version"],
        "format": sub["format"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def bundle_fingerprint(token: dict[str, Any], subs: list[dict[str, Any]]) -> str:
    payload = {
        "token_id": token["id"],
        "scope_all": bool(token["scope_all"]),
        "subscriptions": [
            {
                "id": sub["id"],
                "updated_at": sub["updated_at"],
                "config_hash": subscription_config_hash(sub),
            }
            for sub in subs
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_age(timestamp: int) -> int:
    return max(0, int(time.time()) - int(timestamp))


def get_cached_source(sub: dict[str, Any], allow_stale: bool = False) -> dict[str, Any] | None:
    config_hash = subscription_config_hash(sub)
    with db() as con:
        row = con.execute(
            "SELECT * FROM subscription_cache WHERE subscription_id=? AND config_hash=?",
            (sub["id"], config_hash),
        ).fetchone()
    if not row:
        return None
    age = cache_age(row["fetched_at"])
    if not allow_stale and age >= CACHE_TTL_SECONDS:
        return None
    return {
        "sub": sub,
        "ok": True,
        "status": int(row["status"]),
        "body": bytes(row["body"]),
        "headers": json.loads(row["headers_json"]),
        "error": None,
        "cached": True,
        "stale": age >= CACHE_TTL_SECONDS,
        "cache_age": age,
    }


def store_cached_source(result: dict[str, Any]) -> None:
    if not result.get("ok"):
        return
    sub = result["sub"]
    with db() as con:
        con.execute(
            """
            INSERT INTO subscription_cache
                (subscription_id, config_hash, fetched_at, status, body, headers_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(subscription_id) DO UPDATE SET
                config_hash=excluded.config_hash,
                fetched_at=excluded.fetched_at,
                status=excluded.status,
                body=excluded.body,
                headers_json=excluded.headers_json
            """,
            (
                sub["id"],
                subscription_config_hash(sub),
                int(time.time()),
                int(result["status"]),
                sqlite3.Binary(result["body"]),
                json.dumps(result["headers"], ensure_ascii=False),
            ),
        )


async def get_or_refresh_source(
    client: httpx.AsyncClient,
    sub: dict[str, Any],
    force: bool = False,
) -> dict[str, Any]:
    if not force:
        cached = get_cached_source(sub)
        if cached:
            return cached

    lock = _SOURCE_LOCKS.setdefault(sub["id"], asyncio.Lock())
    async with lock:
        if not force:
            cached = get_cached_source(sub)
            if cached:
                return cached

        previous = get_cached_source(sub, allow_stale=True)
        result = await fetch_one(client, sub)
        if result["ok"]:
            result["cached"] = False
            result["stale"] = False
            result["cache_age"] = 0
            store_cached_source(result)
            return result

        # Keep the last known-good source if refresh fails.
        if previous:
            previous["stale"] = True
            previous["refresh_error"] = result["error"] or f"HTTP {result['status']}"
            return previous
        return result


def get_cached_bundle(
    token: dict[str, Any],
    fingerprint: str,
    allow_stale: bool = False,
) -> dict[str, Any] | None:
    with db() as con:
        row = con.execute(
            "SELECT * FROM bundle_cache WHERE token_id=? AND fingerprint=?",
            (token["id"], fingerprint),
        ).fetchone()
    if not row:
        return None
    age = cache_age(row["generated_at"])
    if not allow_stale and age >= CACHE_TTL_SECONDS:
        return None
    return {
        "status": int(row["status"]),
        "body": bytes(row["body"]),
        "headers": json.loads(row["headers_json"]),
        "generated_at": int(row["generated_at"]),
        "cache_age": age,
        "stale": age >= CACHE_TTL_SECONDS,
    }


def store_cached_bundle(
    token: dict[str, Any],
    fingerprint: str,
    body: bytes,
    headers: dict[str, str],
    status: int = 200,
) -> None:
    with db() as con:
        con.execute(
            """
            INSERT INTO bundle_cache
                (token_id, fingerprint, generated_at, status, body, headers_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(token_id) DO UPDATE SET
                fingerprint=excluded.fingerprint,
                generated_at=excluded.generated_at,
                status=excluded.status,
                body=excluded.body,
                headers_json=excluded.headers_json
            """,
            (
                token["id"],
                fingerprint,
                int(time.time()),
                status,
                sqlite3.Binary(body),
                json.dumps(headers, ensure_ascii=False),
            ),
        )


def cache_headers(headers: dict[str, str], state: str, age: int = 0) -> dict[str, str]:
    out = dict(headers)
    out["X-SubMux-Cache"] = state
    out["X-SubMux-Cache-Age"] = str(max(0, age))
    return out


def force_update_requested(request: Request) -> bool:
    raw = request.query_params.get("forceUpdate", request.query_params.get("forceupdate", ""))
    return raw.strip().lower() in {"1", "true", "yes", "on", "force"}


def response_headers_from_upstream(headers: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    lower = {k.lower(): v for k, v in headers.items()}
    for name in FORWARDED_HEADERS:
        if name.lower() in lower:
            result[name] = lower[name.lower()]
    result["Cache-Control"] = "no-store"
    result["Pragma"] = "no-cache"
    return result


URI_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def looks_like_uri_list(text: str) -> bool:
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    if not lines:
        return False
    uri_lines = [line for line in lines if URI_LINE_RE.match(line)]
    # Subscription URI lists may contain a few comments, but the actual
    # payload should overwhelmingly be proxy URIs.
    return bool(uri_lines) and len(uri_lines) / len(lines) >= 0.8


def maybe_decode_base64(body: bytes, forced: str) -> tuple[str, bool]:
    text = body.decode("utf-8", errors="replace").strip()
    if forced == "raw":
        return text, False

    compact = re.sub(r"\s+", "", text)
    if not compact:
        return "", forced == "base64"

    try:
        padded = compact + "=" * (-len(compact) % 4)
        decoded = base64.b64decode(padded, validate=True).decode("utf-8").strip()
    except Exception:
        if forced == "base64":
            raise ValueError("Upstream payload is not valid base64 UTF-8 text")
        return text, False

    if forced == "base64":
        return decoded, True

    # Auto-detection is intentionally conservative. Do not accidentally
    # reinterpret YAML/JSON/plain text merely because it happens to decode.
    if looks_like_uri_list(decoded):
        return decoded, True

    return text, False

def parse_userinfo(value: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    if not value:
        return out
    for part in value.split(";"):
        if "=" not in part:
            continue
        key, raw = part.strip().split("=", 1)
        try:
            out[key.lower()] = int(raw)
        except ValueError:
            continue
    return out


def merged_userinfo(results: list[dict[str, Any]]) -> str | None:
    infos = [parse_userinfo(r["headers"].get("subscription-userinfo")) for r in results]
    infos = [i for i in infos if i]
    if not infos:
        return None

    parts: list[str] = []

    if any("upload" in i for i in infos):
        parts.append(f"upload={sum(i.get('upload', 0) for i in infos)}")
    if any("download" in i for i in infos):
        parts.append(f"download={sum(i.get('download', 0) for i in infos)}")

    # Missing "total" must not be treated as "unlimited" (total=0).
    if all("total" in i for i in infos):
        totals = [i["total"] for i in infos]
        total = 0 if any(v == 0 for v in totals) else sum(totals)
        parts.append(f"total={total}")

    expires = [i["expire"] for i in infos if i.get("expire", 0) > 0]
    if expires:
        parts.append(f"expire={min(expires)}")
    elif all("expire" in i for i in infos):
        parts.append("expire=0")

    return "; ".join(parts) if parts else None

def proxy_identity_key(uri: str) -> str:
    """Semantic identity for URI-style proxy entries.

    Ignore userinfo credentials and the display fragment, while preserving
    the actual destination and transport/security parameters. The first
    matching entry in bundle order wins.
    """
    try:
        parsed = urlsplit(uri)
        if not parsed.scheme or parsed.hostname is None:
            return uri

        try:
            port = parsed.port
        except ValueError:
            return uri

        query = tuple(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        return repr((
            parsed.scheme.lower(),
            parsed.hostname.lower().rstrip("."),
            port,
            parsed.path or "",
            query,
        ))
    except Exception:
        return uri


def merge_payloads(results: list[dict[str, Any]]) -> tuple[bytes, dict[str, str]]:
    items: list[str] = []
    seen: set[str] = set()
    all_base64 = True

    for result in results:
        content, was_base64 = maybe_decode_base64(result["body"], result["sub"]["format"])
        all_base64 = all_base64 and was_base64

        # SubMux can safely merge URI-list subscriptions. Arbitrary YAML/JSON
        # configs need format-specific parsers; concatenating/deduplicating
        # their lines would silently corrupt them.
        if content and not looks_like_uri_list(content):
            raise ValueError(
                f"{result['sub']['name']}: aggregate mode supports URI-list "
                "subscriptions (raw or base64); use /sub/<name> for pass-through configs"
            )

        for line in content.replace("\r\n", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            key = proxy_identity_key(line)
            if key in seen:
                continue
            seen.add(key)
            items.append(line)

    merged_text = "\n".join(items)
    if merged_text:
        merged_text += "\n"

    body = merged_text.encode("utf-8")
    if all_base64 and results:
        body = base64.b64encode(body)

    title = base64.b64encode(APP_NAME.encode()).decode()
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Disposition": "attachment; filename=submux.txt",
        "Profile-Title": f"base64:{title}",
        "Profile-Update-Interval": "3",
        "X-SubMux-Sources": ",".join(r["sub"]["name"] for r in results),
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }
    userinfo = merged_userinfo(results)
    if userinfo:
        headers["Subscription-Userinfo"] = userinfo
    return body, headers

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
legacy_import()


def is_admin_listener_path(path: str) -> bool:
    return (
        path in {"/", "/admin", "/login", "/logout", "/healthz", "/favicon.ico"}
        or path.startswith("/api/")
        or path.startswith("/static/")
    )


def is_public_listener_path(path: str) -> bool:
    if path == "/healthz":
        return True
    if path == "/subs":
        return True
    if path.startswith("/sub/") and len(path.split("/", 2)) == 3:
        return True

    parts = [part for part in path.split("/") if part]
    if not parts:
        return False

    # Public token routes:
    # /TOKEN/
    # /TOKEN/subs
    # /TOKEN/sub/NAME
    if len(parts) == 1:
        return parts[0] not in {
            "admin", "login", "logout", "api", "static",
            "favicon.ico", "openapi.json", "docs", "redoc", "healthz",
        }
    if len(parts) == 2 and parts[1] == "subs":
        return True
    if len(parts) == 3 and parts[1] == "sub" and parts[2]:
        return True
    return False


@app.middleware("http")
async def listener_isolation(request: Request, call_next):
    server = request.scope.get("server")
    local_port = int(server[1]) if server else ADMIN_PORT
    path = request.url.path

    if local_port == PUBLIC_PORT:
        if not is_public_listener_path(path):
            return Response("Not Found", status_code=404, media_type="text/plain")
    elif local_port == ADMIN_PORT:
        if not is_admin_listener_path(path):
            return Response("Not Found", status_code=404, media_type="text/plain")
    else:
        return Response("Not Found", status_code=404, media_type="text/plain")

    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": APP_NAME}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return RedirectResponse("/static/favicon.svg", status_code=307)


@app.get("/")
def root(request: Request):
    return RedirectResponse("/admin" if admin_authenticated(request) else "/login", status_code=302)


@app.get("/login")
def login_page(request: Request):
    if admin_authenticated(request):
        return RedirectResponse("/admin", status_code=302)
    return FileResponse(BASE_DIR / "templates" / "login.html")


@app.post("/login")
def login(request: Request, admin_token: str = Form(...)):
    if not hmac.compare_digest(admin_token, ADMIN_TOKEN):
        return RedirectResponse("/login?error=1", status_code=303)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        ADMIN_COOKIE_VALUE,
        httponly=True,
        secure=cookie_secure_for(request),
        samesite="lax",
        path="/",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@app.post("/logout")
def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/", secure=cookie_secure_for(request), samesite="lax")
    return response


@app.get("/admin")
def admin_page(request: Request):
    if not admin_authenticated(request):
        return RedirectResponse("/login", status_code=302)
    return FileResponse(BASE_DIR / "templates" / "index.html")


@app.get("/api/config", dependencies=[Depends(require_admin)])
def api_config(request: Request):
    if PUBLIC_BASE_URL:
        public_base = PUBLIC_BASE_URL
    else:
        host = request.url.hostname or "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        public_base = f"{request.url.scheme}://{host}:{PUBLIC_PORT}"

    return {
        "admin_port": ADMIN_PORT,
        "public_port": PUBLIC_PORT,
        "public_base_url": public_base,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
    }


@app.get("/api/subscriptions", dependencies=[Depends(require_admin)])
def api_subscriptions():
    with db() as con:
        rows = con.execute("SELECT * FROM subscriptions ORDER BY id").fetchall()
        return [row_dict(r) for r in rows]


@app.post("/api/subscriptions", dependencies=[Depends(require_admin)])
def api_add_subscription(item: SubscriptionIn):
    ts = now_iso()
    try:
        with db() as con:
            cur = con.execute(
                """
                INSERT INTO subscriptions
                (name, url, user_agent, hwid, device_os, ver_os, device_model, app_version, format, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.name,
                    str(item.url),
                    item.user_agent,
                    item.hwid,
                    item.device_os,
                    item.ver_os,
                    item.device_model,
                    item.app_version,
                    item.format,
                    int(item.enabled),
                    ts,
                    ts,
                ),
            )
            row = con.execute("SELECT * FROM subscriptions WHERE id = ?", (cur.lastrowid,)).fetchone()
            return row_dict(row)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Subscription name already exists")


@app.put("/api/subscriptions/{sub_id}", dependencies=[Depends(require_admin)])
def api_update_subscription(sub_id: int, item: SubscriptionIn):
    ts = now_iso()
    try:
        with db() as con:
            cur = con.execute(
                """
                UPDATE subscriptions SET
                name=?, url=?, user_agent=?, hwid=?, device_os=?, ver_os=?, device_model=?, app_version=?, format=?, enabled=?, updated_at=?
                WHERE id=?
                """,
                (
                    item.name,
                    str(item.url),
                    item.user_agent,
                    item.hwid,
                    item.device_os,
                    item.ver_os,
                    item.device_model,
                    item.app_version,
                    item.format,
                    int(item.enabled),
                    ts,
                    sub_id,
                ),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Subscription not found")
            row = con.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
            return row_dict(row)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Subscription name already exists")


@app.delete("/api/subscriptions/{sub_id}", dependencies=[Depends(require_admin)])
def api_delete_subscription(sub_id: int):
    with db() as con:
        cur = con.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Subscription not found")
    return {"ok": True}


@app.get("/api/subscriptions/{sub_id}/raw", dependencies=[Depends(require_admin)])
async def api_raw_subscription(sub_id: int):
    """Return the upstream payload byte-for-byte, forced to text/plain for inspection."""
    with db() as con:
        row = con.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")

    sub = row_dict(row)
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        result = await fetch_one(client, sub)

    if result["error"]:
        return Response(
            content=f"Upstream connection error: {result['error']}",
            status_code=502,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            },
        )

    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-SubMux-Source": sub["name"],
        "X-SubMux-Upstream-Status": str(result["status"]),
    }
    upstream_type = result["headers"].get("content-type")
    if upstream_type:
        headers["X-SubMux-Upstream-Content-Type"] = upstream_type

    return Response(content=result["body"], status_code=result["status"], headers=headers)


@app.post("/api/subscriptions/{sub_id}/test", dependencies=[Depends(require_admin)])
async def api_test_subscription(sub_id: int):
    with db() as con:
        row = con.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")
    sub = row_dict(row)
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        result = await fetch_one(client, sub)
    return {
        "ok": result["ok"],
        "status": result["status"],
        "bytes": len(result["body"]),
        "content_type": result["headers"].get("content-type"),
        "profile_title": result["headers"].get("profile-title"),
        "error": result["error"],
    }


@app.get("/api/tokens", dependencies=[Depends(require_admin)])
def api_tokens():
    with db() as con:
        tokens = [row_dict(r) for r in con.execute("SELECT * FROM tokens ORDER BY id DESC").fetchall()]
        for token in tokens:
            rows = con.execute(
                """
                SELECT s.id, s.name FROM subscriptions s
                JOIN token_subscriptions ts ON ts.subscription_id=s.id
                WHERE ts.token_id=? ORDER BY s.id
                """,
                (token["id"],),
            ).fetchall()
            token["subscriptions"] = [dict(r) for r in rows]
        return tokens


@app.post("/api/tokens", dependencies=[Depends(require_admin)])
def api_add_token(item: TokenIn):
    token_value = secrets.token_urlsafe(32)
    ts = now_iso()
    with db() as con:
        cur = con.execute(
            "INSERT INTO tokens (label, token, scope_all, enabled, created_at) VALUES (?, ?, ?, 1, ?)",
            (item.label.strip(), token_value, int(item.scope_all), ts),
        )
        token_id = int(cur.lastrowid)
        if not item.scope_all:
            if not item.subscription_ids:
                raise HTTPException(status_code=400, detail="Select at least one subscription")
            valid_ids = {r["id"] for r in con.execute("SELECT id FROM subscriptions").fetchall()}
            if not set(item.subscription_ids).issubset(valid_ids):
                raise HTTPException(status_code=400, detail="Unknown subscription id")
            con.executemany(
                "INSERT INTO token_subscriptions (token_id, subscription_id) VALUES (?, ?)",
                [(token_id, sid) for sid in sorted(set(item.subscription_ids))],
            )
    return {"id": token_id, "label": item.label.strip(), "token": token_value, "scope_all": item.scope_all, "enabled": True}


@app.patch("/api/tokens/{token_id}", dependencies=[Depends(require_admin)])
def api_patch_token(token_id: int, item: TokenPatch):
    with db() as con:
        row = con.execute("SELECT * FROM tokens WHERE id = ?", (token_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Token not found")
        current = row_dict(row)
        label = item.label.strip() if item.label is not None else current["label"]
        enabled = item.enabled if item.enabled is not None else current["enabled"]
        scope_all = item.scope_all if item.scope_all is not None else current["scope_all"]
        con.execute(
            "UPDATE tokens SET label=?, enabled=?, scope_all=? WHERE id=?",
            (label, int(enabled), int(scope_all), token_id),
        )
        if item.subscription_ids is not None or (item.scope_all is not None and item.scope_all):
            con.execute("DELETE FROM token_subscriptions WHERE token_id=?", (token_id,))
        if not scope_all and item.subscription_ids is not None:
            valid_ids = {r["id"] for r in con.execute("SELECT id FROM subscriptions").fetchall()}
            if not set(item.subscription_ids).issubset(valid_ids):
                raise HTTPException(status_code=400, detail="Unknown subscription id")
            con.executemany(
                "INSERT INTO token_subscriptions (token_id, subscription_id) VALUES (?, ?)",
                [(token_id, sid) for sid in sorted(set(item.subscription_ids))],
            )
    return {"ok": True}


@app.delete("/api/tokens/{token_id}", dependencies=[Depends(require_admin)])
def api_delete_token(token_id: int):
    with db() as con:
        cur = con.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Token not found")
    return {"ok": True}


async def build_public_payload(
    token: dict[str, Any],
    subs: list[dict[str, Any]],
    force_sources: bool = False,
) -> tuple[int, bytes, dict[str, str]]:
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        results = await asyncio.gather(
            *(get_or_refresh_source(client, sub, force=force_sources) for sub in subs)
        )

    stale = [r for r in results if r.get("stale")]
    if len(results) == 1:
        result = results[0]
        headers = response_headers_from_upstream(result["headers"])
        if result.get("stale"):
            headers["X-SubMux-Stale"] = result["sub"]["name"]
        if result["error"]:
            body = f"Upstream connection error: {result['error']}".encode()
            return 502, body, {"Content-Type": "text/plain; charset=utf-8"}
        return int(result["status"]), result["body"], headers

    good = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    if not good:
        detail = "; ".join(f"{r['sub']['name']}: {r['error'] or r['status']}" for r in failed)
        return 502, f"All upstream subscriptions failed: {detail}".encode(), {
            "Content-Type": "text/plain; charset=utf-8"
        }

    try:
        body, headers = merge_payloads(good)
    except ValueError as exc:
        return 502, f"Subscription merge error: {exc}".encode(), {
            "Content-Type": "text/plain; charset=utf-8"
        }

    if failed:
        headers["X-SubMux-Failed"] = ",".join(r["sub"]["name"] for r in failed)
    if stale:
        headers["X-SubMux-Stale"] = ",".join(r["sub"]["name"] for r in stale)
    return 200, body, headers


async def refresh_bundle_cache(
    token: dict[str, Any],
    subs: list[dict[str, Any]],
    force_sources: bool = False,
) -> tuple[int, bytes, dict[str, str], str, int]:
    fingerprint = bundle_fingerprint(token, subs)
    lock = _BUNDLE_LOCKS.setdefault(token["id"], asyncio.Lock())

    async with lock:
        # Another request may have refreshed the bundle while we waited.
        if not force_sources:
            cached = get_cached_bundle(token, fingerprint)
            if cached:
                return (
                    cached["status"],
                    cached["body"],
                    cached["headers"],
                    "HIT",
                    cached["cache_age"],
                )

        stale_bundle = get_cached_bundle(token, fingerprint, allow_stale=True)
        status, body, headers = await build_public_payload(
            token, subs, force_sources=force_sources
        )

        if 200 <= status < 300:
            store_cached_bundle(token, fingerprint, body, headers, status)
            return status, body, headers, "REFRESH", 0

        # Upstreams are temporarily unavailable: keep serving the last
        # known-good bundle for the same current configuration.
        if stale_bundle:
            headers = dict(stale_bundle["headers"])
            headers["X-SubMux-Stale-Bundle"] = "1"
            return (
                stale_bundle["status"],
                stale_bundle["body"],
                headers,
                "STALE",
                stale_bundle["cache_age"],
            )

        return status, body, headers, "ERROR", 0


async def refresh_all_bundle_caches() -> None:
    with db() as con:
        rows = con.execute("SELECT * FROM tokens WHERE enabled=1 ORDER BY id").fetchall()
        tokens_to_refresh = [row_dict(row) for row in rows]

    for token in tokens_to_refresh:
        subs = allowed_subscriptions(token)
        if not subs:
            continue
        try:
            fingerprint = bundle_fingerprint(token, subs)
            # Do not move the bundle timestamp forward unless it is actually
            # due for refresh. The worker polls frequently so the effective
            # refresh period stays close to CACHE_TTL_SECONDS.
            if get_cached_bundle(token, fingerprint):
                continue

            lock = _BUNDLE_LOCKS.setdefault(token["id"], asyncio.Lock())
            async with lock:
                if get_cached_bundle(token, fingerprint):
                    continue
                # Fresh source-cache entries may be reused across overlapping
                # bundles; stale ones are fetched from upstream.
                status, body, headers = await build_public_payload(
                    token, subs, force_sources=False
                )
                if 200 <= status < 300:
                    store_cached_bundle(token, fingerprint, body, headers, status)
        except Exception as exc:
            print(
                f"[SubMux] background cache refresh failed for token {token['id']}: {exc}",
                flush=True,
            )


async def cache_refresh_loop() -> None:
    poll_seconds = min(60, CACHE_TTL_SECONDS)
    while True:
        await asyncio.sleep(poll_seconds)
        await refresh_all_bundle_caches()


@app.on_event("startup")
async def start_cache_refresh_task():
    global _CACHE_REFRESH_TASK
    if _CACHE_REFRESH_TASK is None or _CACHE_REFRESH_TASK.done():
        _CACHE_REFRESH_TASK = asyncio.create_task(cache_refresh_loop())


@app.on_event("shutdown")
async def stop_cache_refresh_task():
    global _CACHE_REFRESH_TASK
    if _CACHE_REFRESH_TASK is not None:
        _CACHE_REFRESH_TASK.cancel()
        try:
            await _CACHE_REFRESH_TASK
        except asyncio.CancelledError:
            pass
        _CACHE_REFRESH_TASK = None


async def serve_public(request: Request, name: str | None = None, path_token: str | None = None) -> Response:
    token_value = extract_token(request, path_token)
    token = get_token_record(token_value)
    if not token:
        raise HTTPException(status_code=403, detail="Invalid or disabled access token")

    subs = allowed_subscriptions(token)
    if name is not None:
        subs = [sub for sub in subs if sub["name"] == name]
        if not subs:
            raise HTTPException(status_code=404, detail="Subscription not found or not allowed")
    if not subs:
        raise HTTPException(status_code=403, detail="Token has no accessible subscriptions")

    force = force_update_requested(request)

    # Named pass-through endpoints use the persistent per-source cache too.
    if name is not None:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            before = None if force else get_cached_source(subs[0])
            result = await get_or_refresh_source(client, subs[0], force=force)

        if result["error"]:
            return Response(
                content=f"Upstream connection error: {result['error']}",
                status_code=502,
                media_type="text/plain",
            )

        headers = response_headers_from_upstream(result["headers"])
        state = "REFRESH" if force or before is None else "HIT"
        age = int(result.get("cache_age", 0))
        if result.get("stale"):
            state = "STALE"
            headers["X-SubMux-Stale"] = result["sub"]["name"]
        return Response(
            content=result["body"],
            status_code=result["status"],
            headers=cache_headers(headers, state, age),
        )

    fingerprint = bundle_fingerprint(token, subs)

    if not force:
        cached = get_cached_bundle(token, fingerprint)
        if cached:
            return Response(
                content=cached["body"],
                status_code=cached["status"],
                headers=cache_headers(
                    cached["headers"], "HIT", cached["cache_age"]
                ),
            )

    status, body, headers, state, age = await refresh_bundle_cache(
        token, subs, force_sources=force
    )
    return Response(
        content=body,
        status_code=status,
        headers=cache_headers(headers, state, age),
    )


@app.get("/subs")
async def public_all(request: Request):
    return await serve_public(request)


@app.get("/sub/{name}")
async def public_one(name: str, request: Request):
    return await serve_public(request, name=slugify(name))


@app.get("/{path_token}/subs")
async def public_all_path(path_token: str, request: Request):
    return await serve_public(request, path_token=path_token)


@app.get("/{path_token}")
@app.get("/{path_token}/")
async def public_all_short_path(path_token: str, request: Request):
    """Short token URL: /<TOKEN>/ returns the aggregate subscription."""
    return await serve_public(request, path_token=path_token)


@app.get("/{path_token}/sub/{name}")
async def public_one_path(path_token: str, name: str, request: Request):
    return await serve_public(request, name=slugify(name), path_token=path_token)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return Response(str(exc.detail), status_code=exc.status_code, media_type="text/plain")


def bind_listener(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET6 if ":" in "0.0.0.0" else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.listen(2048)
    sock.set_inheritable(True)
    return sock


if __name__ == "__main__":
    if ADMIN_PORT == PUBLIC_PORT:
        raise RuntimeError("ADMIN_PORT and PUBLIC_PORT must be different")

    sockets = [bind_listener(ADMIN_PORT), bind_listener(PUBLIC_PORT)]
    print(
        f"[SubMux] admin listener: 0.0.0.0:{ADMIN_PORT}; "
        f"public listener: 0.0.0.0:{PUBLIC_PORT}",
        flush=True,
    )
    config = uvicorn.Config(app, proxy_headers=True, log_level="info")
    uvicorn.Server(config).run(sockets=sockets)
