# Contributing

Small project, no ceremony: open a PR. What follows is the context that isn't
obvious from the code — the LMS mechanics this rests on, and the traps that cost
real debugging time. Read the relevant section before changing that area and
you'll skip the same afternoon.

For building and running locally, see [BUILDING.md](BUILDING.md).

## Before you open a PR

```bash
.venv/bin/python -m unittest discover -s tests -v   # no network needed
python3 scripts/check_static.py                     # front-end consistency
python3 scripts/probe.py                            # needs a live LMS
```

CI runs the first two on every pull request, plus three assertions about config
loading: that defaults work with no config file, that the committed
`config.toml` leaves the player guard off, and that the guard can still be set
from the environment. It can't run `probe.py` — that needs a real server with a
real TIDAL login — so run it yourself if you touch `lms.py` or `tidal.py`.

`check_static.py` catches the two front-end failures that are otherwise silent:
JS reaching for an element id the markup doesn't define, and a page fetching an
API path the server doesn't serve. Either one just makes a view stop updating,
with nothing in the log.

`probe.py` is the regression harness for every assumption below. When a TIDAL
plugin update breaks something, it tells you which one. Keep it stdlib-only so
it runs before the venv exists and inside the container.

## How it works

Three mechanisms, all over `POST http://<lms>:9000/jsonrpc.js`:

**Searching TIDAL.** The plugin is a `Slim::Plugin::OPMLBased` with tag `tidal`,
so its menu tree is walkable as `tidal items`. Searching takes two calls — get
the category rows for the query, then drill into the `Songs` row with
`menu:tidal`, which returns `text: "Title\nArtist"` and a playable URL in
`presetParams.favorites_url`.

**Injecting a request.** Append, then reposition:

```
playlist add <url>                    # lands at the end
playlist move <last> <target>         # target = last pending request + 1
```

The target is anchored on the *index of the last still-pending request ahead of
the current track*, not on a count. That matters: if you promote a curated track
into the middle of the request block from Material Skin, a count-based target
lands inside the block and jumps a new request ahead of earlier ones. `pending`
itself is read back from LMS on every request, so nothing desyncs when the queue
is reordered or skipped by hand.

**Shuffle stays off.** LMS shuffle keeps its own internal running order, which
makes `playlist move` indices mean something other than what you see. The track
list is shuffled in Python at load time instead.

**Requests are matched by URL, within a window.** There's no way to tag a queue
entry as "a request", so they're recognised by URL — and a long curated playlist
will happily contain a song a guest also asked for. Only entries within
`REQUEST_WINDOW` (40) of the current track count, because requests are always
injected right behind it. Without that bound, a curated copy 800 tracks away
counts as a queued request and pushes new ones to the end of the night.

**`playlist move` reads its destination after the removal.** LMS's `moveSong`
splices the item out and then re-inserts, so moving a track *down* the queue
lands on `target`, but moving one *up* lands on `target - 1`. Promoting a track
already in the queue has to account for that; inserting a fresh one doesn't.

**Artwork is proxied.** LMS hands back `http://<lms>:9000/imageproxy/…` URLs,
which guests can't reach and which would be blocked as mixed content under TLS.
`LMS.image_url()` rewrites them to `/art?p=…` on this origin and the server
fetches them. `/art` only fetches from the configured LMS host or `*.tidal.com`
— an image proxy that fetched whatever it was handed would be an SSRF hole.

## Gotchas worth knowing

- TIDAL track URLs carry an abbreviated extension — `.flc`, not `.flac` — and it
  follows the plugin's quality setting, so it differs between installs. Never
  build a URL; use the one the server returns.
- LMS names its result list differently per query (`players_loop`, `loop_loop`,
  `item_loop`, `titles_loop`), so `LMS.rows()` matches on the `_loop` suffix
  rather than naming keys.
- Playlist node ids like `3.2` are positional — `3.2` is a given playlist only
  until you reorder your TIDAL playlists. The playlist *name* is stored and the
  id re-resolved at load time.
- LMS `/imageproxy` answers `301`, so the artwork fetch needs
  `follow_redirects=True`. Without it every image 404s.
- Request state is three-way, not two: `vetoed` blocks re-requesting (a veto is
  meant to stick) while `retired` — set when the playlist is reloaded — does
  not.

## Front-end gotchas found the hard way

The three pages are vanilla JS with no build step. Keep it that way; a build
step for three files that poll one endpoint is not a trade worth making.

- `segno`'s `svg_inline()` omits the `xmlns`, so it renders as a broken image
  inside `<img src>`. `/qr.svg` uses `save(kind="svg")` for a standalone doc.
- The QR payload is derived server-side from the `Host` header, deliberately. A
  `/qr.svg` that encoded a string supplied by the page would be an open QR
  generator for arbitrary URLs on your own domain.
- A block's own background paints *above* its negative-z-index descendants, so
  `body.tv` must be transparent or the blurred backdrop is invisible.
- CSS grid items default to `min-width: auto`; one long playlist name overflowed
  the host page until `.grid2 > * { min-width: 0 }`.
- A `display: block` rule beats the `hidden` attribute's UA `display: none`.
- `autocomplete="off"` does not stop password managers. With no `<form>` on the
  page their scope is the whole document, so they pair the host password field
  with the first text input they find — which is why the login sheet has a form
  of its own and the party name carries `data-1p-ignore` and friends.
- The Wake Lock API needs a secure context, so `/tv` on a plain-`http` LAN
  address doesn't get it. That's why there's a muted-video fallback, and why the
  lock is re-acquired on `visibilitychange` — the browser drops it every time
  the page is hidden.

## Icons

`lmsrequest/static/icons/favicon.svg` is the source; the PNGs, `favicon.ico` and
the `/manifest.webmanifest` icon set derive from it. The PNGs are committed so a
deployment needs neither Chrome nor Pillow. After editing the SVG:

```bash
python3 scripts/make_icons.py    # needs Chrome/Chromium and Pillow
```

`start_url` in the manifest is built per-request, so "add to home screen" works
at whatever address guests used.

## Things to keep true

- **Guests can't control playback.** The public API exposes requesting and
  reading; everything else lives behind `/api/host/*`.
- **Guests never need a route to LMS.** Anything client-visible that points at
  the LMS host is a bug.
- **The player name isn't public.** Guests see the party name; which speaker the
  sound comes out of isn't in the public response.
- **The committed `config.toml` ships with the player guard off,** or everyone
  who clones this wonders why nothing plays. CI asserts it.
- **Queue arithmetic stays testable against a fake server.** That's what keeps
  the placement logic verifiable without a live LMS and a real party.
