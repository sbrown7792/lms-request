# LMS Request

Guest song requests for [Lyrion Media Server](https://lyrion.org) with TIDAL.

A curated TIDAL playlist plays by default. Guests scan a QR code, search, and tap
a song — it goes into the queue right after the current track, in the order it
was asked for, ahead of the rest of the playlist. No accounts, no app install,
and guests don't need TIDAL themselves.

Search runs through **your LMS server's own TIDAL plugin**, so there's no second
set of TIDAL credentials to manage, and anything a guest can find is guaranteed
playable on your server.

## Three views

| Page | Who it's for |
|---|---|
| `/` | **Guests.** Search, tap, confirm. Shows what's playing and a few tracks ahead. The only page a guest needs. |
| `/host` | **You.** Password-protected. Pick the player and the playlist, name the party, veto requests, see who's asking for what. |
| `/tv` | **A spare screen.** Now playing with album art and a progress bar, the join QR code, and a short upcoming list. Keeps itself awake. |

## What it does

- **Requests play next, in order.** A request lands immediately after the
  current track and behind any earlier request — first asked, first played.
- **The party playlist never runs out.** It's loaded shuffled and set to repeat,
  so requests are the interruption and the playlist is the floor.
- **No double plays.** Request a song that's already further down the playlist
  and that copy moves up, rather than a second one being added. Extra copies
  still ahead are dropped; copies already behind the current track are left
  alone, since they've played. Asking for whatever is playing right now is
  refused and costs nothing.
- **Nothing to install, for anyone.** Guests get a web page in their phone's
  own browser. They don't need TIDAL or an account.
- **Guests can't touch the music.** No skip, no pause, no player switching, no
  volume. Requesting is the only verb they have.
- **Host veto that sticks.** Remove a request and it can't be re-requested.
- **Party etiquette, enforced gently.** Each guest gets a few requests that
  refill over time, a cap on how many can be queued at once, and one play per
  song — flip **Allow re-requests** on when you'd rather let a favourite come
  round again.
- **A live activity feed** with the address behind each request, and a one-click
  ban if someone is being a nuisance.
- **Guests never need a route to LMS.** Album art is fetched by the server and
  re-served from this app, so LMS can stay entirely on your private network.
- **It survives you fiddling.** Skip tracks or reorder the queue by hand from
  Material Skin or iPeng and request placement still works — the queue is read
  back from LMS on every request rather than tracked locally.

## Requirements

- LMS with the [TIDAL plugin](https://github.com/michaelherger/lms-plugin-tidal)
  installed and logged in
- Docker, or Python 3.11+ to run it directly
- Guests able to reach LMS Request. They do *not* need any route to LMS itself.

## Quick start

Prebuilt `linux/amd64` images are published to `ghcr.io/sbrown7792/lms-request`.

```bash
docker run -d --name lms-request -p 8080:8080 -v lms-request-data:/data \
  -e LMSREQUEST_LMS_HOST=lms -e LMSREQUEST_HOST_TOKEN=pick-something \
  ghcr.io/sbrown7792/lms-request:latest
```

Or with compose, which keeps the settings in a file you can edit later:

```bash
git clone https://github.com/sbrown7792/lms-request.git
cd lms-request
cp .env.example .env
$EDITOR .env        # LMS_HOST at minimum
docker compose -f docker-compose.deploy.yml up -d
docker compose -f docker-compose.deploy.yml logs -f   # host password printed here
```

That pulls the image rather than building it, so `docker-compose.deploy.yml` and
`.env` are the only two files you actually need — copy those instead of cloning
if you prefer.

**Mount `/data`.** It holds `lmsrequest.db`: the cookie-signing secret, the ban
list, your player and playlist choices, and the request log. Without a volume,
every restart logs you out and forgets the bans.

While the package is private, authenticate first with a token carrying
`read:packages`:

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u <your-github-user> --password-stdin
```

Then confirm it can really reach LMS and TIDAL. This is the one check CI can't
do for you, because it needs a live server:

```bash
docker compose -f docker-compose.deploy.yml exec lms-request python scripts/probe.py
```

It lists your players and your saved TIDAL playlists, and resolves a real search
to a playable URL. If that passes, open `http://<that-host>:8080/host`.

To build the image yourself or run without Docker, see [BUILDING.md](BUILDING.md).

## Running the party

1. **Open the host console** at `http://<this-machine>:8080/host` and enter the
   password. It's held in an `HttpOnly` cookie for 24 hours, so it never sits in
   a URL or in browser history. Changing the password invalidates cookies
   already issued, and five wrong guesses from one address locks it out for a
   minute.
2. **Pick a player.** Every LMS player is listed, group players included — a
   whole-house group counts as one target.
3. **Pick a default playlist** from your saved TIDAL playlists. There's a filter
   box, which earns its keep past the first screenful.
4. **Name the party.** Guests see "Playing at *your name*". They're never shown
   the player name — which speaker the sound comes out of means nothing to them,
   and it isn't in the public API response either. Leave it blank to show
   nothing.
5. **Load playlist & play.** The queue is filled with a shuffled copy and set to
   repeat, so it loops all night.
6. **Put `/tv` on a spare screen.** The upcoming list is deliberately short
   (`LMSREQUEST_UP_NEXT`, default 5) — showing the next ten kills the suspense.
   The page keeps the screen awake by itself, so there's no display timeout to
   disable first.

Guests land on `/`.

### While it's running

The host console's playback row plays, pauses and skips, and its **Queue** card
removes individual tracks. The **Activity** card is a
live feed of every request with the time, the client address, a short guest id,
and its state; each row has a **Ban IP** button, and banned addresses appear as
pills you can un-ban.

**Allow re-requests** decides whether a song that has already played can be
asked for again. It's off by default — one play each keeps things moving — and
the switch is there so you don't have to wipe the whole request history just to
let one song round again. Songs still waiting in the queue can't be requested
twice either way, and a song you removed stays removed: a veto is a deliberate
"not tonight", not the one-play rule.

A ban blocks searching as well as requesting, and survives *Reset request
history* — a ban is a deliberate act and shouldn't quietly lapse. It does not
pull that person's already-queued songs; use the Queue card for those. Rate
limits are per browser cookie and bans are per address, so clearing cookies buys
a fresh allowance but not a fresh address.

## Settings

**No config file needed.** Every setting has an environment variable:

| Variable | Default | Notes |
|---|---|---|
| `LMSREQUEST_LMS_HOST` / `LMSREQUEST_LMS_PORT` | `lms` / `9000` | Where LMS is; the port is its web port |
| `LMSREQUEST_HOST_TOKEN` | generated and logged at startup | Host console password |
| `LMSREQUEST_BIND` / `LMSREQUEST_PORT` | `0.0.0.0` / `8080` | |
| `LMSREQUEST_TRUST_FORWARDED_FOR` | `false` | Turn on behind a reverse proxy — see below |
| `LMSREQUEST_PUBLIC_URL` | empty | Overrides the join URL; normally unnecessary |
| `LMSREQUEST_UP_NEXT` / `LMSREQUEST_SEARCH_LIMIT` | `5` / `30` | Upcoming tracks shown; search results shown |
| `LMSREQUEST_ADD_TOKENS` / `LMSREQUEST_ADD_REFILL_SECONDS` | `5` / `180` | Requests per guest, and how fast one comes back |
| `LMSREQUEST_MAX_PENDING` | `3` | Requests one guest may have queued at once |
| `LMSREQUEST_ALLOWED_PLAYERS` | empty (all allowed) | Safety guard — see below |
| `LMSREQUEST_CONFIG` / `LMSREQUEST_DATA_DIR` | `./config.toml` / repo root | |

Precedence is defaults, then `config.toml` if it exists, then the environment.
An unparseable value is logged and ignored rather than fatal. Running without
Docker, editing [`config.toml`](config.toml) is usually easier — it documents
every setting in place.

`config.toml` is deliberately kept out of the image, so a container is
configured only by its environment and can never ship someone else's local
settings.

**The join URL needs no configuration.** The QR code, the link on the TV and
host pages, and the web app manifest are all derived from the address the
browser used to load the page. Open `/tv` at `http://192.168.1.9:8080/tv` and
the QR points at `http://192.168.1.9:8080`; open it at
`https://party.example.com/tv` and it points there instead. Behind a proxy
serving the app from a sub-path, that's included too.

### The player safety guard

Off by default. Give it a list of player ids and LMS Request refuses to send any
state-changing command to a player not on it:

```bash
LMSREQUEST_ALLOWED_PLAYERS=00:04:20:12:34:56,00:04:20:ab:cd:ef
```

Every player still appears in the picker; only commands are gated, and the host
console shows a banner saying so. Worth setting for a dry run, so a test can't
start music in the wrong room at the wrong hour. Player ids are MAC addresses;
the host console's player list shows them, and so does `scripts/probe.py`.

## Behind a reverse proxy

Exposing this on a public hostname means putting a proxy in front of it. Set
`LMSREQUEST_TRUST_FORWARDED_FOR=true` **and** have the proxy send the headers —
both halves are needed:

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
which then gets blocked as mixed content. Without trusting the headers, every
guest arrives as the proxy's address — the activity feed shows one address for
everyone and a ban locks out the whole party.

Leave `trust_forwarded_for` **off** when the app is reachable directly on the
LAN. The socket address is then used and no request header can influence it,
which is what you want when the header is forgeable.

## If something looks wrong

- **The QR points at an internal address.** The host console's **Deployment**
  card shows the address it derived, the client address it saw, whether
  forwarded headers are trusted, and which ones actually arrived. Nearly always
  a missing `proxy_set_header` from the block above. `LMSREQUEST_PUBLIC_URL`
  overrides the lot if a proxy can't be persuaded.
- **No playlists, or search finds nothing.** Run `scripts/probe.py` — it names
  the assumption that broke, and a TIDAL plugin update is the usual reason.
- **Nothing plays and the console shows a banner.** The player safety guard is
  set; clear `LMSREQUEST_ALLOWED_PLAYERS`.
- **You're logged out after every restart.** `/data` isn't on a volume.
- **Which build is this?** `curl -s http://<host>:8080/healthz` reports the
  version, commit and build time. It deliberately doesn't touch LMS, so the
  container won't restart-loop just because the music server went away.

## More

- [BUILDING.md](BUILDING.md) — build the image, run from source, cut a release
- [CONTRIBUTING.md](CONTRIBUTING.md) — how it works inside, and the traps found
  the hard way
