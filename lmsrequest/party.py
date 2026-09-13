"""The party queue engine.

One player has one queue. The curated playlist fills it; guest requests are
injected just after the current track, in request order, so a request is heard
within a song or two instead of in three hours' time.

Injection is append-then-move:

    playlist add <url>              -> lands at the end, index tracks-1
    playlist move <last> <target>   -> target = cur_index + 1 + pending

`pending` is counted from the server's own queue on every request rather than
tracked locally. That way the placement stays right even if the host skips
tracks or drags things around in Material Skin while the party is running.

Shuffle stays off. LMS shuffle keeps its own internal running order, which
makes `playlist move` indices mean something other than what you see. We
shuffle the track list in Python at load time instead.
"""

from __future__ import annotations

import asyncio
import logging
import random

from .lms import LMS, LMSError
from .ratelimit import RateLimiter
from .store import Store
from .tidal import Tidal

log = logging.getLogger("lmsrequest.party")

REPEAT_ALL = 2
SHUFFLE_OFF = 0

# How far past the current track a queue entry may sit and still be treated as
# a guest request. Requests are always injected directly after the current
# track, so the block is short -- the per-guest cap keeps it to a handful.
#
# The bound matters because requests are matched by URL, and a curated playlist
# will happily contain a song someone also asked for. Without it, a copy 800
# tracks away counts as "a request already queued" and drags the insertion
# point to the end of the night.
REQUEST_WINDOW = 40


class RequestRefused(Exception):
    """A guest request we are declining, with a message fit for the guest."""

    def __init__(self, message: str, code: str = "refused") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class Party:
    def __init__(
        self,
        lms: LMS,
        tidal: Tidal,
        store: Store,
        limiter: RateLimiter,
        max_pending_per_guest: int = 3,
    ) -> None:
        self.lms = lms
        self.tidal = tidal
        self.store = store
        self.limiter = limiter
        self.max_pending_per_guest = max_pending_per_guest
        # Serialise queue mutation. Two guests tapping at once would otherwise
        # both read the same playlist_tracks and move the wrong index.
        self._lock = asyncio.Lock()

    # -- host settings -----------------------------------------------------

    @property
    def player(self) -> str | None:
        return self.store.get("player_id")

    @property
    def player_name(self) -> str:
        return self.store.get("player_name") or "no player selected"

    @property
    def playlist_name(self) -> str | None:
        return self.store.get("playlist_name")

    @property
    def party_name(self) -> str | None:
        """What guests are told they're listening to.

        Deliberately separate from the player name -- guests don't care that
        the music is coming out of 'Satellite_Right'.
        """
        return self.store.get("party_name") or None

    @property
    def requests_open(self) -> bool:
        return bool(self.store.get("requests_open", True))

    @property
    def allow_repeats(self) -> bool:
        """May a guest ask for a song that has already played tonight?

        Off by default: one play each keeps a party moving and stops three
        people queueing the same song. On, the host no longer has to clear the
        whole request history just to let one song come round again.
        """
        return bool(self.store.get("allow_repeats", False))

    def set_player(self, player_id: str, name: str) -> None:
        self.store.set("player_id", player_id)
        self.store.set("player_name", name)

    def set_playlist(self, name: str) -> None:
        self.store.set("playlist_name", name)

    def set_party_name(self, name: str) -> None:
        self.store.set("party_name", name.strip())

    def set_requests_open(self, open_: bool) -> None:
        self.store.set("requests_open", bool(open_))

    def set_allow_repeats(self, allow: bool) -> None:
        self.store.set("allow_repeats", bool(allow))

    def blocked(self, url: str, title: str = "") -> tuple[str, str] | None:
        """Why this track can't be requested right now, as (code, message).

        Lives here rather than in request() because the guest UI greys out
        tracks using the same verdict -- a refusal a guest can see coming beats
        one that arrives after the tap.
        """
        name = title or "That song"
        state = self.store.request_state(url)
        if state == "vetoed":
            # A veto sticks whatever the repeat setting says: it was a
            # deliberate "not tonight", not an accident of the one-play rule.
            return "vetoed", f"The host has taken {name} off the list."
        if state == "pending":
            return "duplicate", f"{name} is already on the list."
        if state == "played" and not self.allow_repeats:
            return "played", f"{name} has already played tonight."
        return None

    def ensure_not_banned(self, ip: str) -> None:
        if self.store.is_banned(ip):
            raise RequestRefused(
                "Requests from this device have been switched off by the host.",
                "banned",
            )

    def require_player(self) -> str:
        player = self.player
        if not player:
            raise RequestRefused(
                "No player selected yet — the host needs to pick one.", "no_player"
            )
        return player

    # -- loading the curated playlist --------------------------------------

    async def load_playlist(self, name: str, start_playing: bool = True) -> dict:
        """Replace the queue with a shuffled copy of a saved TIDAL playlist."""
        player = self.require_player()
        node_id = await self.tidal.resolve_playlist(player, name)
        if node_id is None:
            raise RequestRefused(f"Playlist {name!r} is no longer in your TIDAL account.")

        tracks = await self.tidal.playlist_tracks(player, node_id)
        if not tracks:
            raise RequestRefused(f"Playlist {name!r} came back empty.")

        random.shuffle(tracks)

        await self.lms.set_shuffle(player, SHUFFLE_OFF)
        await self.lms.playlist_clear(player)
        for track in tracks:
            await self.lms.playlist_add(player, track["url"], track["title"] or None)
        await self.lms.set_repeat(player, REPEAT_ALL)

        # Requests from a previous session point at queue positions that no
        # longer exist. Retire them -- guests may ask again on the new queue.
        for row in self.store.pending():
            self.store.mark(row["url"], "retired")

        self.store.set("playlist_name", name)
        if start_playing:
            await self.lms.play(player)

        log.info("loaded %d tracks from %r onto %s", len(tracks), name, player)
        return {"playlist": name, "tracks": len(tracks)}

    # -- requests ----------------------------------------------------------

    async def _insertion_point(self, player: str) -> tuple[int, list[dict], int]:
        """(current index, queue entries, index a new request should occupy).

        The target is one past the *last* still-pending request ahead of the
        current track, so requests stay in the order they were asked for.

        Anchoring on that index rather than counting requests matters: if the
        host promotes a curated track into the middle of the request block from
        Material Skin, `cur + 1 + count` would land inside the block and jump
        the new request ahead of ones asked for earlier.
        """
        cur, entries = await self.lms.queue(player)
        ours = self.store.pending_urls()
        ahead = [
            e["index"] for e in entries
            if cur < e["index"] <= cur + REQUEST_WINDOW and e["url"] in ours
        ]
        target = max(ahead) + 1 if ahead else cur + 1
        return cur, entries, target

    async def _promote(self, player: str, src: int, cur: int, target: int) -> int:
        """Move a track already in the queue up to the request position.

        `target` is where a *new* track would be inserted. Moving an existing
        one doesn't grow the queue, and LMS's moveSong splices the item out
        before inserting, so the destination is read against the shortened
        list: moving downward lands on `target`, moving upward lands one short.
        """
        dest = target - 1 if src < target else target
        if src != dest:
            await self.lms.playlist_move(player, src, dest)
        return dest - cur

    async def _drop_other_copies(self, player: str, url: str, keep: int) -> int:
        """Remove any further copies of a track still queued ahead.

        Long curated playlists do contain the same song twice. Promoting one
        copy to the request slot would otherwise still leave the guest hearing
        it again later, which is the repeat we were asked to prevent.

        Deletes from the highest index down so the lower ones stay valid.
        """
        cur, entries = await self.lms.queue(player)
        dupes = sorted(
            (e["index"] for e in entries
             if e["url"] == url and e["index"] > cur and e["index"] != keep),
            reverse=True,
        )
        for index in dupes:
            await self.lms.playlist_delete_index(player, index)
        if dupes:
            log.info("dropped %d further copies of %s at %s", len(dupes), url, dupes)
        return len(dupes)

    async def request(self, track: dict, guest_id: str, ip: str = "") -> dict:
        """Inject a guest request. Raises RequestRefused with a guest message."""
        self.ensure_not_banned(ip)

        if not self.requests_open:
            raise RequestRefused("Requests are paused right now.", "paused")

        player = self.require_player()
        url = track.get("url") or ""
        if not url:
            raise RequestRefused("That track has no playable URL.", "bad_track")

        block = self.blocked(url, track.get("title") or "")
        if block:
            raise RequestRefused(block[1], block[0])

        mine = self.store.pending_count_for(guest_id)
        if mine >= self.max_pending_per_guest:
            raise RequestRefused(
                f"You already have {mine} songs waiting — let those play first.",
                "too_many_pending",
            )

        verdict = self.limiter.take(guest_id)
        if not verdict.allowed:
            raise RequestRefused(verdict.message, "rate_limited")

        async with self._lock:
            try:
                cur, entries, target = await self._insertion_point(player)

                if entries and cur < len(entries) and entries[cur]["url"] == url:
                    raise RequestRefused(
                        f"{track.get('title') or 'That song'} is playing right now!",
                        "playing_now",
                    )

                # Is it already sitting further down the party playlist? Then
                # promote that copy instead of adding a second one -- otherwise
                # the song plays twice and both copies look like requests.
                existing = next(
                    (e for e in entries if e["url"] == url and e["index"] > cur), None
                )

                if existing is not None:
                    position = await self._promote(player, existing["index"], cur, target)
                    await self._drop_other_copies(player, url, cur + position)
                elif not entries:
                    # Nothing loaded, so it becomes the only track.
                    await self.lms.playlist_add(player, url, track.get("title"))
                    await self.lms.play(player)
                    position = 1
                else:
                    await self.lms.playlist_add(player, url, track.get("title"))
                    landed = len(entries)  # it appended: the new last index
                    target = min(target, landed)
                    if target != landed:
                        await self.lms.playlist_move(player, landed, target)
                    position = target - cur
            except (LMSError, RequestRefused):
                # Nothing was queued, so the guest keeps their token.
                self.limiter.refund(guest_id)
                raise

        self.store.add_request(
            url=url,
            title=track.get("title") or "",
            artist=track.get("artist") or "",
            art=track.get("art"),
            guest_id=guest_id,
            ip=ip,
        )
        log.info(
            "request %r by %s (%s) -> position %d",
            track.get("title"), guest_id, ip or "unknown", position,
        )
        return {
            "position": position,
            "tokens_left": int(verdict.tokens_left),
            "title": track.get("title"),
            "artist": track.get("artist"),
        }

    # -- reading the queue back --------------------------------------------

    async def snapshot(self, up_next: int = 5) -> dict:
        """Now playing plus what's coming, annotated with who asked for what."""
        player = self.player
        if not player:
            return {
                "player": None,
                "player_name": self.player_name,
                "party_name": self.party_name,
                "playlist": self.playlist_name,
                "requests_open": self.requests_open,
                "allow_repeats": self.allow_repeats,
                "now_playing": None,
                "up_next": [],
            }

        result = await self.lms.status(player, 0, 9999)
        cur = int(result.get("playlist_cur_index") or 0)
        entries = self.lms.rows(result)
        self._reconcile(cur, entries)

        def describe(entry: dict, index: int) -> dict:
            """LMS's metadata wins; the request row only adds attribution."""
            url = entry.get("url", "")
            row = self.store.requester_of(url)
            requested = (
                row is not None
                and row["state"] not in ("retired", "vetoed")
                and index <= cur + REQUEST_WINDOW
            )
            return {
                "index": index,
                "url": url,
                "title": entry.get("title") or (row["title"] if row else "") or "",
                "artist": entry.get("artist") or (row["artist"] if row else "") or "",
                "art": self.lms.image_url(entry.get("artwork_url"))
                or (row["art"] if row else None),
                "requested": requested,
            }

        now = describe(entries[cur], cur) if cur < len(entries) else None
        coming = [
            describe(e, cur + 1 + i)
            for i, e in enumerate(entries[cur + 1 : cur + 1 + up_next])
        ]
        return {
            "player": player,
            "player_name": self.player_name,
            "party_name": self.party_name,
            "playlist": self.playlist_name,
            "requests_open": self.requests_open,
            "allow_repeats": self.allow_repeats,
            "mode": result.get("mode"),
            "total": int(result.get("playlist_tracks") or len(entries)),
            # Seconds elapsed and track length, for the TV progress bar. The
            # client interpolates between polls off its own clock.
            "elapsed": float(result.get("time") or 0.0),
            "duration": float(result.get("duration") or 0.0),
            "now_playing": now,
            "up_next": coming,
        }

    def _reconcile(self, cur: int, entries: list[dict]) -> None:
        """Mark requests as played once they fall behind the current index."""
        ahead = {e.get("url") for e in entries[cur:]}
        for row in self.store.pending():
            if row["url"] not in ahead:
                self.store.mark(row["url"], "played")

    # -- activity feed -----------------------------------------------------

    def activity(self, limit: int = 80) -> dict:
        """Recent requests with the address that made them, plus the bans."""
        banned = {b["ip"] for b in self.store.bans()}
        return {
            "requests": [
                {
                    "title": r["title"],
                    "artist": r["artist"],
                    "guest": r["guest_id"],
                    "ip": r["ip"] or "",
                    "at": r["created"],
                    "state": r["state"],
                    "banned": bool(r["ip"]) and r["ip"] in banned,
                }
                for r in self.store.recent_requests(limit)
            ],
            "bans": [
                {"ip": b["ip"], "reason": b["reason"], "at": b["created"]}
                for b in self.store.bans()
            ],
        }

    def ban(self, ip: str, reason: str = "") -> dict:
        ip = (ip or "").strip()
        if not ip:
            raise RequestRefused("No address to ban.", "bad_ip")
        self.store.add_ban(ip, reason)
        log.warning("banned %s (%s)", ip, reason or "no reason given")
        return {"banned": ip}

    def unban(self, ip: str) -> dict:
        self.store.remove_ban((ip or "").strip())
        log.info("unbanned %s", ip)
        return {"unbanned": ip}

    # -- host controls -----------------------------------------------------

    async def skip(self) -> None:
        await self.lms.playlist_jump(self.require_player(), "+1")

    async def play_pause(self) -> dict:
        """Flip playback and report the mode it ended up in.

        Which way to flip is read back from LMS rather than taken from the
        caller, so the button still does the right thing when someone has
        paused from Material Skin or the player's own remote.

        Resuming uses `play` rather than `pause 0` because the player may be
        stopped rather than paused -- at the end of a queue, say -- and `pause`
        does nothing from a stop.
        """
        player = self.require_player()
        result = await self.lms.status(player)   # summary fields, no queue
        playing = (result.get("mode") or "") == "play"
        if playing:
            await self.lms.pause(player, 1)
        else:
            await self.lms.play(player)
        return {"mode": "pause" if playing else "play"}

    async def remove(self, index: int) -> dict:
        """Drop a queued track. Index-based so duplicates can't confuse it."""
        player = self.require_player()
        cur, entries = await self.lms.queue(player)
        match = next((e for e in entries if e["index"] == index), None)
        if match is None:
            raise RequestRefused(f"Nothing at queue position {index}.", "not_found")
        await self.lms.playlist_delete_index(player, index)
        # 'vetoed', not 'retired': a song the host pulled stays pulled.
        self.store.mark(match["url"], "vetoed")
        return {"removed": match["title"] or match["url"]}
