"""LMS Request web app: guest request page, host console, TV dashboard."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import socket
import time
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

import segno
from fastapi import (
    Body,
    Cookie,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .lms import LMS, LMSError, PlayerNotAllowed
from .party import Party, RequestRefused
from .ratelimit import RateLimiter
from .store import Store
from .tidal import Tidal, TidalUnavailable, search_everywhere

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"

# Where lmsrequest.db lives. In a container this is a mounted volume; the database
# holds the cookie-signing secret and the ban list, so losing it logs the host
# out and forgets who was banned.
DATA_DIR = Path(os.environ.get("LMSREQUEST_DATA_DIR") or ROOT)

CONFIG_PATH = Path(os.environ.get("LMSREQUEST_CONFIG") or (ROOT / "config.toml"))


def build_version() -> dict:
    """What is running, for /healthz and the startup log.

    In an image these come from build args baked in as environment variables.
    From a source checkout the VERSION file is the fallback, so a locally run
    copy still reports something meaningful.
    """
    version = os.environ.get("LMSREQUEST_VERSION")
    if not version:
        version_file = ROOT / "VERSION"
        version = version_file.read_text().strip() if version_file.exists() else "dev"
    return {
        "version": version,
        "commit": os.environ.get("LMSREQUEST_COMMIT", "unknown"),
        "built": os.environ.get("LMSREQUEST_BUILT", "unknown"),
    }


BUILD = build_version()

# Every setting has a default, so LMS Request runs with no config file at all --
# which is how the container image is meant to be used.
DEFAULTS: dict[str, dict] = {
    "lms": {"host": "lms", "port": 9000},
    "server": {
        "bind": "0.0.0.0",
        "port": 8080,
        "public_url": "",
        "trust_forwarded_for": False,
    },
    "host": {"token": ""},
    "party": {"search_limit": 30, "up_next": 5},
    "limits": {
        "add_tokens": 5,
        "add_refill_seconds": 180,
        "max_pending_per_guest": 3,
    },
    # Empty by default: the guard is a development safety net, not a shipped
    # restriction. An image that shipped a player allowlist would refuse to
    # play anything.
    "dev": {"allowed_players": []},
}


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _as_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


# (section, key) -> (environment variable, parser)
ENV_OVERRIDES: dict[tuple[str, str], tuple[str, object]] = {
    ("lms", "host"): ("LMSREQUEST_LMS_HOST", str),
    ("lms", "port"): ("LMSREQUEST_LMS_PORT", int),
    ("server", "bind"): ("LMSREQUEST_BIND", str),
    ("server", "port"): ("LMSREQUEST_PORT", int),
    ("server", "public_url"): ("LMSREQUEST_PUBLIC_URL", str),
    ("server", "trust_forwarded_for"): ("LMSREQUEST_TRUST_FORWARDED_FOR", _as_bool),
    ("host", "token"): ("LMSREQUEST_HOST_TOKEN", str),
    ("party", "search_limit"): ("LMSREQUEST_SEARCH_LIMIT", int),
    ("party", "up_next"): ("LMSREQUEST_UP_NEXT", int),
    ("limits", "add_tokens"): ("LMSREQUEST_ADD_TOKENS", int),
    ("limits", "add_refill_seconds"): ("LMSREQUEST_ADD_REFILL_SECONDS", int),
    ("limits", "max_pending_per_guest"): ("LMSREQUEST_MAX_PENDING", int),
    ("dev", "allowed_players"): ("LMSREQUEST_ALLOWED_PLAYERS", _as_list),
}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s"
)
log = logging.getLogger("lmsrequest")

GUEST_COOKIE = "lmsrequest_guest"
DEFAULT_TOKEN = "change-me-before-the-party"

# Failed host logins per address: enough to stop a script guessing the password
# over the internet, loose enough to survive a few fat-fingered attempts.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60.0
_login_failures: dict[str, list[float]] = {}


def login_locked_out(ip: str) -> float:
    """Seconds still to wait, or 0.0 if this address may try again."""
    now = time.monotonic()
    recent = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_LOCKOUT_SECONDS]
    _login_failures[ip] = recent
    if len(recent) < LOGIN_MAX_FAILURES:
        return 0.0
    return LOGIN_LOCKOUT_SECONDS - (now - recent[0])


def note_login_failure(ip: str) -> None:
    _login_failures.setdefault(ip, []).append(time.monotonic())


def load_config() -> dict:
    """Defaults, then config.toml if present, then environment variables."""
    config = {section: dict(values) for section, values in DEFAULTS.items()}

    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as fh:
            for section, values in tomllib.load(fh).items():
                config.setdefault(section, {}).update(values)
    else:
        log.info("no config file at %s -- defaults and environment only", CONFIG_PATH)

    for (section, key), (variable, parse) in ENV_OVERRIDES.items():
        raw = os.environ.get(variable)
        if raw is None:
            continue
        try:
            config.setdefault(section, {})[key] = parse(raw)
        except ValueError:
            log.warning("ignoring %s=%r: not a valid value", variable, raw)

    return config


def lan_ip() -> str:
    """Best guess at the address guests should point their phones at."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))  # no packets sent; just picks a route
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class App:
    """Wiring, kept off module scope so tests can build one per case."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.lms = LMS(
            host=config["lms"]["host"],
            port=config["lms"]["port"],
            allowed_players=config.get("dev", {}).get("allowed_players", []),
        )
        self.tidal = Tidal(self.lms)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.store = Store(DATA_DIR / "lmsrequest.db")
        limits = config.get("limits", {})
        self.limiter = RateLimiter(
            self.store,
            capacity=limits.get("add_tokens", 5),
            refill_seconds=limits.get("add_refill_seconds", 180),
        )
        self.party = Party(
            self.lms,
            self.tidal,
            self.store,
            self.limiter,
            max_pending_per_guest=limits.get("max_pending_per_guest", 3),
        )
        # With no password configured, mint one and keep it in the database so
        # it survives restarts. Logged at startup, which is how the host finds
        # it -- the alternative is an unreachable console.
        self.token_was_generated = False
        if not self.config["host"]["token"]:
            stored = self.store.get("host_token")
            if not stored:
                stored = secrets.token_urlsafe(9)
                self.store.set("host_token", stored)
            self.config["host"]["token"] = stored
            self.token_was_generated = True

        # Cookie-signing secret, generated once and kept in the db.
        secret = self.store.get("cookie_secret")
        if not secret:
            secret = secrets.token_hex(32)
            self.store.set("cookie_secret", secret)
        self.secret = secret.encode()

    # -- guest identity ----------------------------------------------------

    def sign(self, value: str) -> str:
        mac = hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()[:16]
        return f"{value}.{mac}"

    def verify(self, cookie: str | None) -> str | None:
        if not cookie or "." not in cookie:
            return None
        value, _, mac = cookie.rpartition(".")
        expected = hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()[:16]
        return value if hmac.compare_digest(mac, expected) else None

    def guest_id(self, response: Response, cookie: str | None) -> str:
        existing = self.verify(cookie)
        if existing:
            return existing
        new = secrets.token_urlsafe(9)
        response.set_cookie(
            GUEST_COOKIE,
            self.sign(new),
            max_age=60 * 60 * 12,
            httponly=True,
            samesite="lax",
        )
        return new

    def client_ip(self, request: Request) -> str:
        """The address to log and to ban.

        Behind a reverse proxy the socket address is the proxy's, identical for
        every guest -- so read X-Forwarded-For when configured to.

        Take the *rightmost* entry, not the leftmost. A proxy that appends
        produces "whatever-the-client-claimed, the-address-it-saw", so the
        leftmost value is forgeable: a guest could dodge a ban or pin their
        requests on someone else's address. The rightmost entry is the one the
        trusted proxy observed. (This assumes a single proxy hop, which is the
        normal home setup.)
        """
        if self.config["server"].get("trust_forwarded_for"):
            forwarded = request.headers.get("x-forwarded-for", "")
            hops = [h.strip() for h in forwarded.split(",") if h.strip()]
            if hops:
                return hops[-1]
        return request.client.host if request.client else "unknown"

    def join_url(self, request: Request | None = None) -> str:
        """The address guests should open: what the QR encodes, no trailing slash.

        Derived from the request the browser actually made, so LMS Request works at
        whatever hostname, port or sub-path it is deployed behind with no
        configuration. Loading /tv from https://example.com/party/tv yields
        https://example.com/party.

        Read from the request rather than reported by the page's JavaScript: a
        /qr.svg that encoded a client-supplied string would be an open QR
        generator for arbitrary URLs on your own domain. The Host header is
        already the browser's view of where it is.

        `server.public_url` overrides it, for when the screen showing the QR
        reaches LMS Request by a different route than guests' phones do.
        """
        configured = self.config["server"].get("public_url")
        if configured:
            return configured.rstrip("/")
        if request is None:
            return f"http://{lan_ip()}:{self.config['server']['port']}"

        trusted = bool(self.config["server"].get("trust_forwarded_for"))
        scheme = request.url.scheme
        host = request.headers.get("host", "")
        if trusted:
            # uvicorn runs with --no-proxy-headers, so these are ours to apply.
            scheme = (request.headers.get("x-forwarded-proto") or scheme).split(",")[0].strip()
            forwarded_host = request.headers.get("x-forwarded-host", "")
            if forwarded_host:
                host = forwarded_host.split(",")[0].strip()
        if not host:
            return f"http://{lan_ip()}:{self.config['server']['port']}"

        # root_path is set when mounted under a sub-path by a proxy.
        root = (request.scope.get("root_path") or "").rstrip("/")
        return f"{scheme}://{host}{root}"


config = load_config()
state = App(config)


@asynccontextmanager
async def lifespan(_: FastAPI):
    guard = state.lms.allowed_players
    log.info(
        "LMS Request %s (%s, built %s) up. LMS %s, guests at %s",
        BUILD["version"], BUILD["commit"], BUILD["built"],
        state.lms.base, state.join_url(),
    )
    if state.token_was_generated:
        log.warning(
            "host console password: %s   (set LMSREQUEST_HOST_TOKEN or host.token "
            "in config.toml to choose your own)",
            state.config["host"]["token"],
        )
    elif state.config["host"]["token"] == DEFAULT_TOKEN:
        log.warning(
            "host.token is still the placeholder -- change it before exposing "
            "LMS Request beyond your own network"
        )
    if guard:
        log.warning(
            "dev guard active: only %s may receive commands", ", ".join(sorted(guard))
        )
    yield
    await state.lms.close()
    state.store.close()


# No docs or schema endpoints: this gets pointed at the open internet for an
# evening, and none of them are useful to a guest.
app = FastAPI(
    title="LMS Request",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.exception_handler(RequestRefused)
async def refused_handler(_, exc: RequestRefused):
    return JSONResponse({"error": exc.message, "code": exc.code}, status_code=409)


@app.exception_handler(PlayerNotAllowed)
async def guard_handler(_, exc: PlayerNotAllowed):
    return JSONResponse({"error": str(exc), "code": "player_blocked"}, status_code=403)


@app.exception_handler(TidalUnavailable)
async def tidal_handler(_, exc: TidalUnavailable):
    return JSONResponse({"error": str(exc), "code": "tidal_down"}, status_code=502)


@app.exception_handler(LMSError)
async def lms_handler(_, exc: LMSError):
    return JSONResponse({"error": str(exc), "code": "lms_error"}, status_code=502)


HOST_COOKIE = "lmsrequest_host"


def host_proof() -> str:
    """Cookie payload proving the token was entered.

    Derived from the token, so changing it in config.toml invalidates any
    cookie already handed out instead of leaving old sessions authenticated.
    """
    token = state.config["host"]["token"]
    digest = hashlib.sha256(token.encode()).hexdigest()[:16]
    return f"host:{digest}"


def token_ok(candidate: str) -> bool:
    expected = state.config["host"]["token"]
    return bool(expected) and hmac.compare_digest(candidate, expected)


def require_host(request: Request) -> bool:
    """Gate for everything under /api/host.

    Three ways in, in order: the cookie set by the login prompt, an
    an X-Host-Token header (for scripts), or ?t= (older bookmarks and curl).
    """
    cookie = request.cookies.get(HOST_COOKIE)
    if cookie and state.verify(cookie) == host_proof():
        return True
    header = request.headers.get("x-host-token", "")
    if header and token_ok(header):
        return True
    if token_ok(request.query_params.get("t", "")):
        return True
    raise HTTPException(status_code=403, detail="host password required")


# -- pages -----------------------------------------------------------------


@app.get("/")
async def guest_page():
    return FileResponse(STATIC / "guest.html")


@app.get("/host")
async def host_page():
    return FileResponse(STATIC / "host.html")


@app.post("/api/host/login")
async def api_host_login(
    request: Request, response: Response, body: dict = Body(...)
):
    ip = state.client_ip(request)
    wait = login_locked_out(ip)
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail=f"too many attempts — wait {int(wait) + 1}s",
        )
    if not token_ok(body.get("token") or ""):
        note_login_failure(ip)
        log.warning("failed host login from %s", ip)
        raise HTTPException(status_code=403, detail="wrong password")
    _login_failures.pop(ip, None)
    response.set_cookie(
        HOST_COOKIE,
        state.sign(host_proof()),
        max_age=60 * 60 * 24,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


@app.post("/api/host/logout")
async def api_host_logout(response: Response):
    response.delete_cookie(HOST_COOKIE)
    return {"ok": True}


@app.get("/tv")
async def tv_page():
    return FileResponse(STATIC / "tv.html")


@app.get("/qr.svg")
async def qr_code(request: Request):
    # save(kind="svg") emits a standalone document with the xmlns declaration.
    # svg_inline() omits it, which is invalid inside an <img> -- the browser
    # just shows the alt text.
    buf = io.BytesIO()
    # Dark-on-white with the default 4-module quiet zone. The previous light
    # modules on a transparent background produced an inverted code, which many
    # phone cameras scan slowly or not at all.
    segno.make(state.join_url(request), error="m").save(
        buf, kind="svg", scale=10, border=4, dark="#0b0b0f", light="#ffffff"
    )
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.get("/healthz")
async def healthz():
    """Liveness and build identity. Deliberately does not touch LMS."""
    return {"ok": True, **BUILD}


@app.get("/favicon.ico")
async def favicon():
    """Browsers ask for this at the root regardless of the <link> tags."""
    return FileResponse(STATIC / "icons" / "favicon.ico")


@app.get("/manifest.webmanifest")
async def manifest(request: Request):
    """Built per-request so start_url follows wherever LMS Request is deployed."""
    base = state.join_url(request)
    return Response(
        json.dumps(
            {
                "name": "LMS Request",
                "short_name": "LMS Request",
                "description": "Request a song",
                "start_url": f"{base}/",
                "scope": f"{base}/",
                "display": "standalone",
                "background_color": "#0b0b0f",
                "theme_color": "#0b0b0f",
                "icons": [
                    {
                        "src": "/static/icons/icon-192.png",
                        "sizes": "192x192",
                        "type": "image/png",
                    },
                    {
                        "src": "/static/icons/icon-512.png",
                        "sizes": "512x512",
                        "type": "image/png",
                        "purpose": "any maskable",
                    },
                ],
            }
        ),
        media_type="application/manifest+json",
    )


# -- guest API -------------------------------------------------------------


@app.get("/api/status")
async def api_status():
    """Public status. The host's own endpoint is /api/host/queue."""
    snap = await state.party.snapshot(up_next=state.config["party"].get("up_next", 5))
    # Guests are shown the party name instead, so don't hand out the player
    # name or id on an endpoint anyone on the network can read.
    snap.pop("player_name", None)
    snap.pop("player", None)
    return snap


@app.get("/api/join")
async def api_join(request: Request):
    return {"url": state.join_url(request)}


@app.get("/api/search")
async def api_search(request: Request, q: str = Query(min_length=2, max_length=120)):
    # Banned guests are stopped here too, so a ban also spares the TIDAL API.
    state.party.ensure_not_banned(state.client_ip(request))
    player = state.party.require_player()
    limit = state.config["party"].get("search_limit", 30)
    results = await search_everywhere(state.tidal, state.lms, player, q.strip(), limit)
    pending = state.store.pending_urls()
    for track in results:
        # Grey these out in the UI rather than letting the tap fail.
        track["queued"] = state.store.requested_ever(track["url"]) or track["url"] in pending
    return {"query": q, "results": results}


@app.post("/api/request")
async def api_request(
    request: Request,
    response: Response,
    track: dict = Body(...),
    lmsrequest_guest: str | None = Cookie(default=None),
):
    guest = state.guest_id(response, lmsrequest_guest)
    return await state.party.request(track, guest, ip=state.client_ip(request))


# -- host API --------------------------------------------------------------


@app.get("/api/host/players")
async def api_players(_: bool = Depends(require_host)):
    players = await state.lms.players()
    return {"players": players, "selected": state.party.player}


@app.post("/api/host/player")
async def api_set_player(body: dict = Body(...), _: bool = Depends(require_host)):
    players = {p["id"]: p for p in await state.lms.players()}
    chosen = players.get(body.get("id", ""))
    if chosen is None:
        raise HTTPException(404, "no such player")
    state.party.set_player(chosen["id"], chosen["name"])
    return {"selected": chosen}


@app.get("/api/host/playlists")
async def api_playlists(_: bool = Depends(require_host)):
    player = state.party.require_player()
    return {
        "playlists": await state.tidal.saved_playlists(player),
        "selected": state.party.playlist_name,
    }


@app.post("/api/host/playlist")
async def api_set_playlist(body: dict = Body(...), _: bool = Depends(require_host)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    state.party.set_playlist(name)
    return {"selected": name}


@app.post("/api/host/party-name")
async def api_set_party_name(body: dict = Body(...), _: bool = Depends(require_host)):
    state.party.set_party_name(body.get("name") or "")
    return {"party_name": state.party.party_name}


@app.post("/api/host/load")
async def api_load(body: dict = Body(default={}), _: bool = Depends(require_host)):
    name = (body.get("name") or state.party.playlist_name or "").strip()
    if not name:
        raise HTTPException(400, "no playlist selected")
    return await state.party.load_playlist(name, start_playing=body.get("play", True))


@app.get("/api/host/queue")
async def api_queue(_: bool = Depends(require_host)):
    return await state.party.snapshot(up_next=100)


@app.post("/api/host/skip")
async def api_skip(_: bool = Depends(require_host)):
    await state.party.skip()
    return {"ok": True}


@app.delete("/api/host/queue/{index}")
async def api_remove(index: int, _: bool = Depends(require_host)):
    return await state.party.remove(index)


@app.post("/api/host/requests")
async def api_toggle_requests(body: dict = Body(...), _: bool = Depends(require_host)):
    state.party.set_requests_open(bool(body.get("open", True)))
    return {"requests_open": state.party.requests_open}


@app.get("/api/host/activity")
async def api_activity(_: bool = Depends(require_host)):
    return state.party.activity()


@app.post("/api/host/ban")
async def api_ban(body: dict = Body(...), _: bool = Depends(require_host)):
    return state.party.ban(body.get("ip", ""), body.get("reason", ""))


@app.post("/api/host/unban")
async def api_unban(body: dict = Body(...), _: bool = Depends(require_host)):
    return state.party.unban(body.get("ip", ""))


@app.post("/api/host/reset")
async def api_reset(_: bool = Depends(require_host)):
    state.store.reset_party()
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
