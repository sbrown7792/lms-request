"""Walk the LMS TIDAL plugin's menu tree over JSON-RPC.

The plugin (michaelherger/lms-plugin-tidal) is a Slim::Plugin::OPMLBased with
tag 'tidal', so its whole browse tree is reachable as `tidal items` with an
item_id naming a node. Searching TIDAL this way means we reuse the server's own
TIDAL login -- no second set of credentials -- and anything we find is
guaranteed playable on this server.

Two shapes come back depending on mode:

  plain          -> loop_loop, items have {id, name, type, image, url}
  menu:tidal     -> item_loop, items have {text: "Title\\nArtist", icon,
                    presetParams: {favorites_url: "tidal://<id>.flc"}}

We use plain mode for browsing (playlists, playlist tracks) and menu mode for
search results, because only menu mode carries the artist name.

Never construct a tidal:// URL. The extension tracks the server's quality
preference -- it is '.flc' on this server, not '.flac' -- so always use the URL
the server hands back.
"""

from __future__ import annotations

import asyncio
import logging

from .lms import LMS

log = logging.getLogger("lmsrequest.tidal")

PAGE = 200


class TidalUnavailable(RuntimeError):
    """The TIDAL plugin is missing, disabled, or not logged in."""


class Tidal:
    def __init__(self, lms: LMS) -> None:
        self.lms = lms
        # Top-level node ids, resolved by name on first use. They are stable
        # small integers on a given server, but the order shifts between plugin
        # versions, so we look them up rather than hardcoding 3 and 7.
        self._nodes: dict[str, str] | None = None

    async def _top_level(self, player: str) -> dict[str, str]:
        if self._nodes is not None:
            return self._nodes
        rows = await self.lms.query_rows(player, ["tidal", "items", 0, 50])
        if not rows:
            raise TidalUnavailable(
                "the TIDAL plugin returned no menu -- check it is enabled and "
                "logged in on the LMS server"
            )
        nodes: dict[str, str] = {}
        for row in rows:
            if row.get("name"):
                nodes[row["name"]] = str(row["id"])
            if row.get("type") == "search":
                nodes["__search__"] = str(row["id"])
        self._nodes = nodes
        log.info("TIDAL menu nodes: %s", nodes)
        return nodes

    async def _node(self, player: str, *names: str) -> str:
        nodes = await self._top_level(player)
        for name in names:
            if name in nodes:
                return nodes[name]
        raise TidalUnavailable(
            f"no {names[0]!r} node in the TIDAL menu (found: {sorted(nodes)})"
        )

    async def _page_all(self, player: str, item_id: str, *extra: str) -> list[dict]:
        """Read every row of a node, following the count field."""
        out: list[dict] = []
        offset = 0
        while True:
            result = await self.lms.query(
                player, ["tidal", "items", offset, PAGE, f"item_id:{item_id}", *extra]
            )
            rows = self.lms.rows(result)
            out.extend(rows)
            total = int(result.get("count") or len(out))
            offset += len(rows)
            if not rows or offset >= total:
                return out

    # -- playlists ---------------------------------------------------------

    async def saved_playlists(self, player: str) -> list[dict]:
        """The host's saved TIDAL playlists, for the default-playlist picker."""
        node = await self._node(player, "Playlists")
        rows = await self._page_all(player, node)
        return [
            {
                "node_id": str(r["id"]),
                "name": r.get("name") or "(untitled)",
                "art": self.lms.image_url(r.get("image")),
            }
            for r in rows
            if r.get("id") is not None
        ]

    async def resolve_playlist(self, player: str, name: str) -> str | None:
        """Find a playlist's node id by name.

        Node ids are positional -- '3.2' means whatever is third in the list
        today -- so we store the name and re-resolve it every time we load.
        """
        for pl in await self.saved_playlists(player):
            if pl["name"] == name:
                return pl["node_id"]
        return None

    async def playlist_tracks(self, player: str, node_id: str) -> list[dict]:
        """Every track of a playlist node, with playable URLs."""
        rows = await self._page_all(player, node_id, "want_url:1")
        return [
            {
                "url": r["url"],
                "title": r.get("name") or "",
                "artist": "",
                "art": self.lms.image_url(r.get("image")),
                "source": "tidal",
            }
            for r in rows
            if r.get("url")
        ]

    # -- search ------------------------------------------------------------

    async def search_tracks(self, player: str, query: str, limit: int = 30) -> list[dict]:
        """Search TIDAL for tracks. Two calls: categories, then the Songs row.

        We read the Songs row's own id out of the first response rather than
        building '7_<escaped query>.4' ourselves -- LMS escapes the query into
        that id in its own way, and matching by name survives menu reordering.
        """
        search_node = await self._node(player, "__search__", "Search")
        cats = await self.lms.query_rows(
            player,
            ["tidal", "items", 0, 50, f"item_id:{search_node}", f"search:{query}"],
        )
        if not cats:
            return []

        songs = next((c for c in cats if c.get("name") == "Songs"), None)
        if songs is None:
            log.warning("no Songs category for %r; categories=%s",
                        query, [c.get("name") for c in cats])
            return []

        result = await self.lms.query(
            player,
            [
                "tidal", "items", 0, limit,
                f"item_id:{songs['id']}",
                f"search:{query}",
                "menu:tidal",
            ],
        )
        return [t for t in (self._menu_track(r) for r in self.lms.rows(result)) if t]

    def _menu_track(self, row: dict) -> dict | None:
        """Turn one menu-mode row into a track, or None if it isn't one."""
        url = (row.get("presetParams") or {}).get("favorites_url")
        if not url or not url.startswith(("tidal://", "wimp://")):
            return None
        lines = (row.get("text") or "").split("\n")
        return {
            "url": url,
            "title": lines[0].strip() if lines else "",
            "artist": lines[1].strip() if len(lines) > 1 else "",
            "art": self.lms.image_url(row.get("icon")),
            "source": "tidal",
        }


async def search_everywhere(
    tidal: Tidal, lms: LMS, player: str, query: str, limit: int = 30
) -> list[dict]:
    """TIDAL and the local library at once, TIDAL first, duplicates dropped."""
    tidal_hits, local_hits = await asyncio.gather(
        tidal.search_tracks(player, query, limit),
        lms.search_local(query, 20),
        return_exceptions=True,
    )
    results: list[dict] = []
    seen: set[str] = set()
    for group in (tidal_hits, local_hits):
        if isinstance(group, BaseException):
            log.warning("search leg failed: %s", group)
            continue
        for track in group:
            key = (track["title"] + "|" + track["artist"]).lower()
            if key in seen:
                continue
            seen.add(key)
            results.append(track)
    return results[:limit]
