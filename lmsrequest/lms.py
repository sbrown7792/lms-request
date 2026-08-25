"""Async client for the Lyrion Media Server JSON-RPC interface.

Everything goes through POST http://<host>:9000/jsonrpc.js with a body of
{"id":1,"method":"slim.request","params":[<playerid>,[<command>, ...]]}.
The command vocabulary is the LMS CLI, documented at lyrion.org/reference/cli/.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

log = logging.getLogger("lmsrequest.lms")

# Where proxied artwork is served from. Clients only ever see this prefix.
ART_PATH = "/art?p="

# Album art is a few hundred KB; anything far larger is not artwork.
MAX_ART_BYTES = 8 * 1024 * 1024


def resolve_art_ref(ref: str, lms_base: str, lms_host: str) -> str:
    """Turn an /art reference back into a URL the server may fetch.

    Deliberately narrow. An image proxy that fetched whatever it was handed is
    an SSRF hole: a guest could aim it at a router admin page or a cloud
    metadata endpoint and read the response through it. Relative references
    resolve against LMS; absolute ones must be on a host we expect artwork from.

    Raises ValueError for anything else.
    """
    ref = (ref or "").strip()
    if not ref or ".." in ref or any(c in ref for c in "\n\r\\"):
        raise ValueError("bad artwork reference")

    if ref.startswith(("http://", "https://")):
        host = (urlsplit(ref).hostname or "").lower()
        if host != lms_host.lower() and not host.endswith(".tidal.com"):
            raise ValueError(f"artwork host not allowed: {host or '?'}")
        return ref

    # Protocol-relative ("//host/x") and other schemes are not artwork paths.
    if ref.startswith("//") or ":" in ref.split("/")[0]:
        raise ValueError("bad artwork reference")
    return f"{lms_base.rstrip('/')}/{ref.lstrip('/')}"


class LMSError(RuntimeError):
    """The server was reachable but the command did not do what we asked."""


class PlayerNotAllowed(LMSError):
    """Blocked by the dev safety guard before it reached the server."""


class LMS:
    def __init__(
        self,
        host: str,
        port: int = 9000,
        allowed_players: list[str] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base = f"http://{host}:{port}"
        self.url = f"{self.base}/jsonrpc.js"
        # Non-empty means: refuse state-changing commands to anything else.
        self.allowed_players = {p.lower() for p in (allowed_players or [])}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    # -- transport ---------------------------------------------------------

    async def query(self, player: str, cmd: list[Any]) -> dict:
        """Run a read-only query. Never gated by the safety guard."""
        payload = {"id": 1, "method": "slim.request", "params": [player, cmd]}
        try:
            resp = await self._client.post(self.url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LMSError(f"{self.url} unreachable: {exc}") from exc
        return resp.json().get("result") or {}

    async def command(self, player: str, cmd: list[Any]) -> dict:
        """Run a state-changing command, subject to the safety guard."""
        if self.allowed_players and player.lower() not in self.allowed_players:
            raise PlayerNotAllowed(
                f"player {player} is not in dev.allowed_players; "
                f"clear that list in config.toml to unlock all players"
            )
        log.info("command %s %s", player, cmd)
        return await self.query(player, cmd)

    @staticmethod
    def rows(result: dict) -> list[dict]:
        """Pull the result list out of a response.

        Each query names its list differently -- players_loop, loop_loop,
        item_loop, titles_loop, playlist_loop -- so match on the suffix rather
        than special-casing every command.
        """
        for key, value in result.items():
            if key.endswith("_loop") and isinstance(value, list):
                return value
        return []

    async def query_rows(self, player: str, cmd: list[Any]) -> list[dict]:
        return self.rows(await self.query(player, cmd))

    # -- players -----------------------------------------------------------

    async def players(self) -> list[dict]:
        """All players, including group players, newest state first-hand."""
        rows = await self.query_rows("", ["players", 0, 100])
        return [
            {
                "id": r["playerid"],
                "name": r.get("name") or r["playerid"],
                "model": r.get("model", ""),
                "is_group": r.get("model") == "group",
                "connected": bool(r.get("connected")),
                "power": bool(r.get("power")),
                "allowed": (
                    not self.allowed_players
                    or r["playerid"].lower() in self.allowed_players
                ),
            }
            for r in rows
        ]

    # -- queue state -------------------------------------------------------

    # url, artist, album, artwork. LMS fills these in for remote TIDAL tracks
    # too, so the whole queue gets real metadata and not just the requests.
    STATUS_TAGS = "tags:uaKl"

    async def status(self, player: str, start: int = 0, count: int = 0) -> dict:
        """Player status. count=0 gets the summary fields without the queue."""
        return await self.query(player, ["status", start, count, self.STATUS_TAGS])

    async def queue(self, player: str, limit: int = 9999) -> tuple[int, list[dict]]:
        """(current index, queue entries). Entries carry index, url, title."""
        result = await self.status(player, 0, limit)
        cur = int(result.get("playlist_cur_index") or 0)
        entries = [
            {
                "index": int(r.get("playlist index", i)),
                "url": r.get("url", ""),
                "title": r.get("title") or "",
                "artist": r.get("artist") or "",
                "art": self.image_url(r.get("artwork_url")),
            }
            for i, r in enumerate(self.rows(result))
        ]
        return cur, entries

    async def track_count(self, player: str) -> int:
        result = await self.status(player, 0, 0)
        return int(result.get("playlist_tracks") or 0)

    # -- queue mutation ----------------------------------------------------

    async def playlist_clear(self, player: str) -> None:
        await self.command(player, ["playlist", "clear"])

    async def playlist_add(self, player: str, url: str, title: str | None = None) -> None:
        cmd = ["playlist", "add", url]
        if title:
            cmd.append(title)
        await self.command(player, cmd)

    async def playlist_move(self, player: str, frm: int, to: int) -> None:
        await self.command(player, ["playlist", "move", frm, to])

    async def playlist_delete_index(self, player: str, index: int) -> None:
        await self.command(player, ["playlist", "delete", index])

    async def playlist_jump(self, player: str, index: int | str) -> None:
        """Jump to an absolute index, or a relative one like '+1'."""
        await self.command(player, ["playlist", "index", index])

    async def set_shuffle(self, player: str, mode: int) -> None:
        await self.command(player, ["playlist", "shuffle", mode])

    async def set_repeat(self, player: str, mode: int) -> None:
        await self.command(player, ["playlist", "repeat", mode])

    async def play(self, player: str) -> None:
        await self.command(player, ["play"])

    async def pause(self, player: str, on: int = 1) -> None:
        await self.command(player, ["pause", on])

    # -- local library -----------------------------------------------------

    async def search_local(self, query: str, limit: int = 20) -> list[dict]:
        """Search the local library. Free bonus alongside the TIDAL search."""
        rows = await self.query_rows(
            "", ["titles", 0, limit, f"search:{query}", "tags:ula"]
        )
        return [
            {
                "url": r["url"],
                "title": r.get("title") or "",
                "artist": r.get("artist") or "",
                "art": None,
                "source": "local",
            }
            for r in rows
            if r.get("url")
        ]

    def image_url(self, path: str | None) -> str | None:
        """Turn an LMS artwork reference into a URL on *this* server.

        Never hand a client an LMS URL. Guests are typically on a network that
        cannot reach LMS at all -- only this process can -- and behind TLS an
        http:// LMS URL would be blocked as mixed content even if it could.

        So the reference is passed to /art, which fetches it server-side. Two
        shapes arrive from LMS: paths relative to the server ("/imageproxy/...",
        "/music/<id>/cover.jpg") and occasionally absolute URLs on TIDAL's CDN.
        Both are kept verbatim here and resolved when /art is called.
        """
        if not path:
            return None
        if path.startswith(ART_PATH):
            return path  # already proxied; keeps this idempotent
        return ART_PATH + quote(path, safe="")

    async def fetch_image(self, url: str) -> tuple[bytes, str]:
        """Fetch one artwork image. Raises LMSError if it isn't usable."""
        try:
            # LMS's /imageproxy answers 301 and points at the real CDN URL, so
            # redirects have to be followed. Safe here: the first hop is already
            # restricted to LMS or TIDAL by the caller, and a server that could
            # redirect us somewhere hostile is one we already trust for
            # everything else.
            resp = await self._client.get(url, timeout=15.0, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LMSError(f"artwork fetch failed: {exc}") from exc

        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            raise LMSError(f"artwork was {content_type or 'untyped'}, not an image")
        if len(resp.content) > MAX_ART_BYTES:
            raise LMSError(f"artwork is {len(resp.content)} bytes, over the cap")
        return resp.content, content_type
