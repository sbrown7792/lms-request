# LMS Request

Guest song requests for [Lyrion Media Server](https://lyrion.org) with TIDAL.

A curated TIDAL playlist plays by default. Guests scan a QR code, search, and tap
a song — it goes into the queue right after the current track, in the order it
was asked for, ahead of the rest of the playlist. No accounts, no app install,
and guests don't need TIDAL themselves.

Search runs through **your LMS server's own TIDAL plugin**, so there's no second
set of TIDAL credentials to manage and anything a guest can find is guaranteed
playable on your server.

## Requirements

- LMS with the [TIDAL plugin](https://github.com/michaelherger/lms-plugin-tidal)
  installed and logged in
- Python 3.11+ (needs `tomllib`)
- Guests able to reach LMS Request. They do *not* need any route to LMS itself —
  artwork and search both go through this app.

## Quick start

Prebuilt images are published to `ghcr.io/sbrown7792/lms-request`. On the host
that will run it:

```bash
git clone https://github.com/sbrown7792/lms-request.git
cd lms-request
cp .env.example .env
$EDITOR .env        # set LMS_HOST at minimum
docker compose -f docker-compose.deploy.yml up -d
docker compose -f docker-compose.deploy.yml logs -f   # host password printed here
```

That pulls the image rather than building it — only `docker-compose.deploy.yml`
and `.env` are actually needed, so you can copy those two files instead of
cloning.

Or a single command, no files at all:

```bash
docker run -d --name lms-request -p 8080:8080 -v lms-request-data:/data \
  -e LMSREQUEST_LMS_HOST=lms -e LMSREQUEST_HOST_TOKEN=pick-something \
  ghcr.io/sbrown7792/lms-request:latest
```

While the package is private, authenticate first with a token that has
`read:packages`:

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u <your-github-user> --password-stdin
```

Then open `http://<that-host>:8080/host`, and see [Running the
party](#running-the-party).

Confirm it can actually reach LMS and TIDAL — the one check CI can't do, because
it needs a real server:

```bash
docker compose -f docker-compose.deploy.yml exec lms-request python scripts/probe.py
```

## Building the image yourself

```bash
git clone https://github.com/sbrown7792/lms-request.git
cd lms-request
docker compose up -d --build
docker compose logs -f lms-request        # the host password is printed here
```

**Mount `/data`.** It holds `lmsrequest.db`: the cookie-signing secret, the ban
list, the host's player and playlist choices, and the request log. Without a
volume, every restart logs the host out and forgets the bans.

**No config file needed.** Every setting has an environment variable:

| Variable | Default |
|---|---|
| `LMSREQUEST_LMS_HOST` / `LMSREQUEST_LMS_PORT` | `lms` / `9000` |
| `LMSREQUEST_BIND` / `LMSREQUEST_PORT` | `0.0.0.0` / `8080` |
| `LMSREQUEST_HOST_TOKEN` | generated and logged at startup |
| `LMSREQUEST_PUBLIC_URL` | empty (derived from the request) |
| `LMSREQUEST_TRUST_FORWARDED_FOR` | `false` |
| `LMSREQUEST_UP_NEXT` / `LMSREQUEST_SEARCH_LIMIT` | `5` / `30` |
| `LMSREQUEST_ADD_TOKENS` / `LMSREQUEST_ADD_REFILL_SECONDS` | `5` / `180` |
| `LMSREQUEST_MAX_PENDING` | `3` |
| `LMSREQUEST_ALLOWED_PLAYERS` | empty (all players allowed) — comma-separated ids |
| `LMSREQUEST_CONFIG` / `LMSREQUEST_DATA_DIR` | `./config.toml` / repo root |

Precedence is defaults, then `config.toml` if it exists, then the environment. An
unparseable value is logged and ignored rather than fatal.

`config.toml` is in `.dockerignore` on purpose, so the image is configured only
by its environment and can never ship someone's local settings — a stale
`allowed_players` baked into an image would leave it refusing to play at all.

**Networking.** LMS Request only speaks HTTP to LMS on port 9000, so ordinary bridge
networking is enough — no host networking, multicast or slimproto, unlike LMS
itself. If `lms` doesn't resolve inside the container, uncomment `extra_hosts` in
`docker-compose.yml`.

**Healthcheck** hits `/healthz`, which deliberately does not touch LMS: the
container shouldn't restart-loop because the music server went away.

## Setup (without Docker)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
$EDITOR config.toml          # set lms.host and a real host.token
python3 scripts/probe.py     # confirm LMS and TIDAL respond as expected
./run.sh start
```

`probe.py` is stdlib-only and reads the same environment variables, so it also
runs inside the container:

```bash
docker compose exec lms-request python scripts/probe.py
```

`run.sh` takes `start`, `stop`, `restart`, or `log`.

## Running the party

1. Open the host console at `http://<this-machine>:8080/host` and enter the
   password from `host.token`. It's kept in an `HttpOnly` cookie for 24 hours,
   so it never sits in a URL or in browser history. Changing `host.token`
   invalidates any cookie already issued. Five wrong passwords from one address
   locks that address out for a minute, so it can't be guessed at speed.
2. **Pick a player.** Every LMS player is listed, including group players
   (a group counts as one target, so a whole-house group works).
3. **Pick a default playlist** from your saved TIDAL playlists. There's a
   filter box, which earns its keep once you have more than a screenful.
4. **Name the party.** Guests see "Playing at *your name*". They're never
   shown the player name — which speaker the sound comes out of means nothing
   to them, and it isn't in the public API response either. Leave it blank to
   show nothing.
5. **Load playlist & play.** The queue is filled with a shuffled copy and set to
   repeat, so it loops all night.
6. Put `/tv` on a spare screen. Now playing fills the top of the screen — album
   art, title, progress bar, and a blurred zoom of the artwork as the backdrop —
   with the QR code and a short upcoming list below. That list is deliberately
   short (`party.up_next`, default 5): showing the next ten kills the suspense.

Guests land on `/`. That's the only page they need.

## Configuration

Everything lives in `config.toml`.

| Setting | Meaning |
|---|---|
| `lms.host` / `lms.port` | Where LMS is. Port is the web port, normally 9000. |
| `server.public_url` | Override the join URL. Normally leave empty — see below. |
| `host.token` | The host console password, guarding every `/api/host/*` route. **Change it.** |
| `limits.add_tokens` | Requests a guest may make before waiting. |
| `limits.add_refill_seconds` | How fast one request token comes back. |
| `limits.max_pending_per_guest` | Requests one guest may have queued at once. |
| `server.trust_forwarded_for` | Read the client address from `X-Forwarded-For`. See below. |
| `dev.allowed_players` | Safety guard — see below. |

### Deploying it anywhere

The join URL — the QR code, the link on the TV and host pages, and the web app
manifest — is derived from the address the browser used. Open `/tv` at
`http://192.168.1.9:8080/tv` and the QR points at `http://192.168.1.9:8080`;
open it at `https://party.example.com/tv` and it points there instead. Behind a
proxy that mounts LMS Request under a sub-path, `root_path` is included too. No
configuration.

That derivation is server-side, from the `Host` header, deliberately: a
`/qr.svg` that encoded a string supplied by the page would be an open QR
generator for arbitrary URLs on your own domain.

### Behind a reverse proxy

Set `LMSREQUEST_TRUST_FORWARDED_FOR=true` and have the proxy send the headers.
The complete nginx location block:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;

    # nginx defaults Host to the *upstream* address. Left alone, the QR code
    # and join link advertise an internal host no guest phone can reach.
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-Host  $host;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Replace rather than append, so a client cannot inject a value: this is
    # the address the ban list works on.
    proxy_set_header X-Forwarded-For   $remote_addr;
}
```

Without `X-Forwarded-Proto` an HTTPS deployment advertises an `http://` link,
which then gets blocked as mixed content.

The host console's **Deployment** card shows the address it derived, the client
address it saw, whether forwarded headers are being trusted, and the headers
that actually arrived — check there first if the QR points at the wrong place.
`LMSREQUEST_PUBLIC_URL` overrides the lot if a proxy can't be persuaded.

**Guests never need to reach LMS.** Artwork is fetched server-side and served
from `/art` on this origin, so only the container needs a route to LMS. That
also keeps everything same-origin under TLS. `/art` will only fetch from the
configured LMS host or `*.tidal.com`; anything else is refused, so it can't be
turned into an open proxy.

Set `server.public_url` only when the screen showing the QR reaches LMS Request by a
different route than guests' phones do, e.g. the TV is on the LAN but guests
come in over a public hostname.

### Icons

`lmsrequest/static/icons/favicon.svg` is the source; the PNGs, `favicon.ico` and the
`/manifest.webmanifest` icon set derive from it. The PNGs are committed so a
deployment needs neither Chrome nor Pillow. After editing the SVG:

```bash
python3 scripts/make_icons.py    # needs Chrome/Chromium and Pillow
```

`start_url` in the manifest is built per-request, so "add to home screen" works
at whatever address guests used.

### Seeing who's requesting, and cutting someone off

The host console's **Activity** card is a live feed: every request with the time,
the client address, a short guest id, and its state. Each row has a **Ban IP**
button; banned addresses appear as pills at the top of the card with an unban
button. A ban blocks both requesting and searching, so it also spares the TIDAL
API. Bans survive *Reset request history* — a ban is a deliberate act and
shouldn't quietly lapse.

Banning does **not** pull that person's already-queued songs. Use the Remove
buttons in the Queue card for those.

Rate limits are per browser cookie, bans are per address — a guest who clears
their cookies gets a fresh allowance but keeps their address.

### Getting the client address right

If LMS Request is reachable directly on the LAN, leave `trust_forwarded_for = false`.
The socket address is used, and no request header can influence it.

Behind a reverse proxy — which is what exposing it on a public hostname means —
every guest arrives with the proxy's address, so the feed shows one address for
everyone and a ban would lock out the whole party. Set
`trust_forwarded_for = true` there.

Two details this gets right, both of which bit during development:

- **The rightmost `X-Forwarded-For` entry is used, not the leftmost.** A proxy
  that appends produces `whatever-the-client-claimed, the-address-it-saw`, so
  the leftmost value is forgeable — a guest could dodge a ban or pin their
  requests on someone else's address.
- **`run.sh` passes `--no-proxy-headers`.** uvicorn enables proxy headers by
  default and trusts `X-Forwarded-For` from `127.0.0.1`, rewriting the client
  address before the app sees it — which silently overrode
  `trust_forwarded_for = false`. Start LMS Request some other way and you lose that
  protection.

This assumes a single proxy hop, which is the normal home setup.

### The player safety guard

Off by default. Give it a list of player ids and LMS Request refuses to send any
state-changing command to a player not on it. Players still all appear in the
picker; only commands are gated, and the host console shows a banner saying so.

Useful for a dry run, so a test can't start music in the wrong room. Set it any
of three ways:

```bash
# config.toml
allowed_players = ["00:04:20:12:34:56"]

# environment, including Docker
LMSREQUEST_ALLOWED_PLAYERS=00:04:20:12:34:56,00:04:20:ab:cd:ef

# .env in the project root — gitignored, picked up by run.sh
echo 'LMSREQUEST_ALLOWED_PLAYERS=00:04:20:12:34:56' > .env
```

Player ids are MAC addresses. The host console's player list shows them, and so
does `scripts/probe.py`.

`docker-compose.deploy.yml` wires it to `ALLOWED_PLAYERS` in `.env`. CI asserts
the committed `config.toml` leaves it off, because a repo that ships with the
guard on has anyone who clones it wondering why nothing plays.

## What guests can and can't do

- Search TIDAL and the local library; tap a track and confirm the request.
- They cannot skip, pause, change player, or see the host console.
- A song already requested tonight shows as `queued` and can't be re-requested.
- A song the host removes stays blocked — a veto is meant to stick.
- Requesting a song that's already further down the party playlist **moves that
  copy up** rather than adding a second one, so it doesn't play twice. If the
  playlist happens to contain the song more than once, the extra copies still
  ahead are dropped too. Copies already behind the current track are left alone
  — they've played.
- Requesting whatever is playing right now is refused, with the token refunded.
- Rate limits are per browser (a signed `HttpOnly` cookie). A guest who clears
  their browser data gets a fresh allowance. That's deliberate: this is party
  etiquette enforcement, not security.

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
would land inside the block and jump a new request ahead of earlier ones.
`pending` itself is read back from LMS on every request, so nothing desyncs if
you reorder or skip things by hand.

**Shuffle stays off.** LMS shuffle keeps its own internal running order, which
makes `playlist move` indices mean something other than what you see. LMS Request
shuffles the track list in Python at load time instead.

**Requests are matched by URL, within a window.** There's no way to tag a queue
entry as "a request", so LMS Request recognises them by URL — and a long curated
playlist will happily contain a song a guest also asked for. Only entries within
`REQUEST_WINDOW` (40) of the current track count, because requests are always
injected right behind it. Without that bound, a curated copy 800 tracks away
counts as a queued request and pushes new ones to the end of the night.

**`playlist move` reads its destination after the removal.** LMS's `moveSong`
splices the item out and then re-inserts, so moving a track *down* the queue
lands on `target`, but moving one *up* lands on `target - 1`. Promoting a track
already in the queue has to account for that; inserting a fresh one doesn't.

### Gotchas worth knowing

- TIDAL track URLs carry an abbreviated extension — `.flc`, not `.flac` — and
  it follows the plugin's quality setting, so it differs between installs.
  Never build a URL; use the one the server returns.
- LMS names its result list differently per query (`players_loop`, `loop_loop`,
  `item_loop`, `titles_loop`), so `LMS.rows()` matches on the `_loop` suffix.
- Playlist node ids like `3.2` are positional. LMS Request stores the playlist *name*
  and re-resolves the id at load time.
- Don't stop the server with `pkill -f uvicorn...` — the pattern matches the
  shell running it too. Use `./run.sh stop`.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v   # 20 tests, no network
python3 scripts/check_static.py                     # front-end consistency
python3 scripts/probe.py                            # live LMS + TIDAL check
```

`probe.py` is the regression harness for the assumptions above. If a TIDAL
plugin update breaks something, it tells you which one.

`check_static.py` catches the two front-end failures that are otherwise silent —
JS reaching for an element id the markup doesn't define, and a page fetching an
API path the server doesn't serve. Either one just makes a view stop updating
with nothing in the log.

### Front-end gotchas found the hard way

- `segno`'s `svg_inline()` omits the `xmlns`, so it renders as a broken image
  inside `<img src>`. `/qr.svg` uses `save(kind="svg")` for a standalone doc.
- A block's own background paints *above* its negative-z-index descendants, so
  `body.tv` must be transparent or the blurred backdrop is invisible.
- CSS grid items default to `min-width: auto`; one long playlist name overflowed
  the page until `.grid2 > * { min-width: 0 }`.
- A `display: block` rule beats the `hidden` attribute's UA `display: none`.

## Releasing (maintainers)

Images are published to GitHub Container Registry by
[`.github/workflows/release.yml`](.github/workflows/release.yml). Tests run
first, so a failing build never publishes.

Pushing changes under `.github/workflows/` needs a token with the `workflow`
scope: `gh auth refresh -h github.com -s workflow`.

**Cut a release.** Bump `VERSION`, commit, then tag:

```bash
echo 0.2.0 > VERSION
git commit -am "Release 0.2.0"
git tag v0.2.0 && git push --tags
```

That publishes `:0.2.0`, `:0.2` and `:latest`. Pushes to `root` (the default branch) publish `:edge`.
The running container reports which build it is:

```bash
curl -s http://target:8080/healthz
# {"ok":true,"version":"0.2.0","commit":"a1b2c3…","built":"…"}
```

If the package is private, consumers need `docker login ghcr.io` on the target
with a token carrying `read:packages`.

**Architecture.** The workflow builds `linux/amd64` only. For a Raspberry Pi,
add `linux/arm64` to `platforms:` in the workflow — it works, but the emulated
`pip install` roughly doubles the job time.

## Layout

```
VERSION                     bumped per release; reported at /healthz
config.toml                 local settings (never baked into the image)
run.sh                      start / stop / restart / log, without Docker
Dockerfile
docker-compose.yml          build from source
docker-compose.deploy.yml   pull the published image
.env.example                deployment settings
.github/workflows/          tests, and publish to GHCR on a tag
lmsrequest/
  lms.py                    JSON-RPC client and command helpers
  tidal.py                  TIDAL menu-tree walker: playlists, tracks, search
  party.py                  queue engine: load, inject, dedupe, veto
  ratelimit.py              per-guest token bucket
  store.py                  sqlite persistence
  app.py                    FastAPI routes, config layering, auth
  static/                   guest.html, host.html, tv.html, icons/
scripts/
  probe.py                  live LMS + TIDAL diagnostic, stdlib only
  check_static.py           front-end consistency
  make_icons.py             rasterise favicon.svg
tests/                      40 unit tests, no network
```
