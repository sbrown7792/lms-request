#!/usr/bin/env python3
"""Verify LMS Request's assumptions about the LMS JSON-RPC and TIDAL menu tree.

Stdlib only, so this runs before the venv exists. Read-only: every call here is
a query, nothing touches playback. Keep this around -- when the TIDAL plugin
updates, this is what tells you which assumption broke.

    python3 scripts/probe.py [--player <mac>] [--query "daft punk"]
"""

import argparse
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path(os.environ.get("LMSREQUEST_CONFIG") or
              (Path(__file__).resolve().parent.parent / "config.toml"))


def lms_target() -> tuple[str, int]:
    """Same precedence as the app: config file if present, then environment."""
    host, port = "lms", 9000
    if CONFIG.exists():
        with CONFIG.open("rb") as fh:
            lms = tomllib.load(fh).get("lms", {})
        host = lms.get("host", host)
        port = lms.get("port", port)
    host = os.environ.get("LMSREQUEST_LMS_HOST", host)
    port = int(os.environ.get("LMSREQUEST_LMS_PORT", port))
    return host, port

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"


class Probe:
    def __init__(self, host: str, port: int) -> None:
        self.url = f"http://{host}:{port}/jsonrpc.js"
        self.calls = 0

    def request(self, player: str, cmd: list) -> dict:
        payload = {"id": 1, "method": "slim.request", "params": [player, cmd]}
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.calls += 1
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp).get("result") or {}

    def rows(self, player: str, cmd: list) -> list[dict]:
        """Pull the result list out, whatever LMS decided to call it.

        Every query names its list differently -- players_loop, loop_loop,
        item_loop, titles_loop -- so go by the suffix rather than guessing.
        """
        result = self.request(player, cmd)
        for key, value in result.items():
            if key.endswith("_loop") and isinstance(value, list):
                return value
        return []


def fail(msg: str) -> None:
    print(f"{BAD} {msg}")
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--player",
        default=os.environ.get("LMSREQUEST_PROBE_PLAYER", ""),
        help="player id to browse TIDAL as; defaults to the first connected one",
    )
    ap.add_argument("--query", default="daft punk")
    args = ap.parse_args()

    host, port = lms_target()
    probe = Probe(host, port)
    print(f"LMS: {probe.url}\n")

    # 1. Reachability and the player list.
    try:
        players = probe.rows("", ["players", 0, 50])
    except (urllib.error.URLError, TimeoutError) as exc:
        fail(f"cannot reach {probe.url}: {exc}")

    # Only queries are issued below -- nothing here starts or changes playback.
    # The player id is needed because the TIDAL plugin resolves the account per
    # player.
    chosen = args.player
    if not chosen:
        chosen = next(
            (p["playerid"] for p in players if p.get("connected")),
            players[0]["playerid"] if players else "",
        )

    print(f"{OK} {len(players)} players")
    target = None
    for p in players:
        mark = ""
        if p["playerid"] == chosen:
            target = p
            mark = "  <-- using"
        conn = "connected" if p.get("connected") else "OFFLINE"
        print(f"    {p['name']:<18} {p['playerid']}  {p['model']:<11} {conn}{mark}")
    if target is None:
        fail(f"player {chosen} not found on this server")
    if not target.get("connected"):
        print(f"{BAD} that player is offline -- TIDAL browsing may fail")
    args.player = chosen
    print()

    # 2. The TIDAL top-level menu. Ids here should be plain integers.
    top = probe.rows(args.player, ["tidal", "items", 0, 50])
    if not top:
        fail("TIDAL plugin returned no menu -- is it enabled and logged in?")
    names = {row.get("name"): row.get("id") for row in top}
    print(f"{OK} TIDAL menu: " + "  ".join(f"{i}={n}" for n, i in names.items()))

    playlists_id = names.get("Playlists")
    search_id = next(
        (r["id"] for r in top if (r.get("type") == "search" or r.get("name") == "Search")),
        None,
    )
    if playlists_id is None:
        fail("no 'Playlists' node in the TIDAL menu")
    if search_id is None:
        fail("no search node in the TIDAL menu")
    print(f"    playlists node = {playlists_id!r}   search node = {search_id!r}\n")

    # 3. Saved TIDAL playlists -- the default-playlist picker's source.
    saved = probe.rows(args.player, ["tidal", "items", 0, 200, f"item_id:{playlists_id}"])
    print(f"{OK} {len(saved)} saved TIDAL playlists")
    for row in saved[:8]:
        print(f"    {row.get('id'):<8} {row.get('name')}")
    if len(saved) > 8:
        print(f"    ... and {len(saved) - 8} more")
    if not saved:
        fail("no saved playlists -- the picker would be empty")
    print()

    # 4. Tracks of the first playlist, with playable URLs.
    first = saved[0]
    tracks = probe.rows(
        args.player,
        ["tidal", "items", 0, 200, f"item_id:{first['id']}", "want_url:1"],
    )
    with_urls = [t for t in tracks if t.get("url")]
    print(f"{OK} '{first['name']}': {len(tracks)} tracks, {len(with_urls)} with URLs")
    for t in with_urls[:3]:
        print(f"    {t['url']:<26} {t.get('name')}")
    if not with_urls:
        fail("playlist tracks came back without URLs -- want_url:1 not honoured?")
    suffixes = {t["url"].rsplit(".", 1)[-1] for t in with_urls}
    print(f"    URL format suffix(es): {', '.join(sorted(suffixes))}\n")

    # 5. Search, step one: the category rows.
    cats = probe.rows(
        args.player,
        ["tidal", "items", 0, 50, f"item_id:{search_id}", f"search:{args.query}"],
    )
    if not cats:
        fail(f"search for {args.query!r} returned no categories")
    print(f"{OK} search categories: " + ", ".join(str(c.get("name")) for c in cats))
    songs = next((c for c in cats if c.get("name") == "Songs"), None)
    if songs is None:
        fail("no 'Songs' category -- match-by-name assumption is broken")
    print(f"    songs node = {songs['id']!r}\n")

    # 6. Search, step two: tracks in menu mode, which carries the artist.
    result = probe.request(
        args.player,
        [
            "tidal", "items", 0, 10,
            f"item_id:{songs['id']}",
            f"search:{args.query}",
            "menu:tidal",
        ],
    )
    items = result.get("item_loop") or []
    if not items:
        fail("menu-mode search returned no tracks")
    print(f"{OK} {len(items)} track results for {args.query!r}")
    resolved = 0
    for item in items[:5]:
        text = (item.get("text") or "").split("\n")
        title = text[0] if text else "?"
        artist = text[1] if len(text) > 1 else ""
        url = (item.get("presetParams") or {}).get("favorites_url")
        if url:
            resolved += 1
        print(f"    {str(url):<26} {title} — {artist}")
    if not resolved:
        fail("no presetParams.favorites_url on results -- cannot build playable URLs")
    print()

    # 7. Queue state fields the FIFO injection depends on.
    status = probe.request(args.player, ["status", 0, 3, "tags:u"])
    needed = ["playlist_cur_index", "playlist_tracks", "playlist_timestamp"]
    missing = [f for f in needed if f not in status]
    if missing:
        print(f"{BAD} status missing {missing} (normal if the queue is empty)")
    else:
        print(
            f"{OK} status: index={status['playlist_cur_index']} "
            f"of {status['playlist_tracks']} tracks"
        )
    print(f"\n{OK} all assumptions hold ({probe.calls} calls)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
