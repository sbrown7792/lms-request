# Building and running from source

For deploying the published image, see the [README](README.md) — that's the
short path and most people want it. This is for building it yourself, running it
without Docker, or cutting a release.

## Build the image

```bash
git clone https://github.com/sbrown7792/lms-request.git
cd lms-request
docker compose up -d --build
docker compose logs -f lms-request        # the host password is printed here
```

`docker-compose.yml` builds from source; `docker-compose.deploy.yml` pulls the
published image. They're separate files so neither one has a mode you can pick
by accident.

The build stamps `VERSION`, the commit and the build time into OCI labels and
into `LMSREQUEST_*` env, which is what `/healthz` reports back:

```bash
curl -s http://localhost:8080/healthz
# {"ok":true,"version":"0.1.2","commit":"a1b2c3…","built":"…"}
```

Plain `docker build` works too, but pass the args or the image will report
itself as `dev`:

```bash
docker build \
  --build-arg VERSION="$(cat VERSION)" \
  --build-arg COMMIT="$(git rev-parse --short HEAD)" \
  --build-arg BUILT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -t lms-request:local .
```

A few deliberate choices in the `Dockerfile`:

- **Single stage.** There's nothing to compile — a builder stage would add
  moving parts to save nothing.
- **Runs as uid 10001,** not root. `/data` is chowned to it in the image, so a
  named volume inherits the right owner on first use.
- **`config.toml` is in `.dockerignore`.** The image is configured only by its
  environment and can never carry someone's local settings. A stale
  `allowed_players` baked in would leave it refusing to play at all.
- **The healthcheck doesn't touch LMS,** so the container won't restart-loop
  because the music server went away.

**Networking.** LMS Request only speaks HTTP to LMS on port 9000, so ordinary
bridge networking is enough — no host networking, multicast or slimproto, unlike
LMS itself. If `lms` doesn't resolve inside the container, uncomment
`extra_hosts` in `docker-compose.yml`.

## Run without Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
$EDITOR config.toml          # set lms.host and a real host.token
python3 scripts/probe.py     # confirm LMS and TIDAL respond as expected
./run.sh start
```

`run.sh` takes `start`, `stop`, `restart` or `log`, and tracks the pid in
`lmsrequest.pid`. It sources `.env` if one exists, taking the same
`LMSREQUEST_*` variables the container does — handy for keeping a local
`ALLOWED_PLAYERS` out of committed files.

Two things `run.sh` does that matter if you start uvicorn yourself:

- **`--no-proxy-headers`.** uvicorn enables proxy headers by default and trusts
  `X-Forwarded-For` from `127.0.0.1`, rewriting the client address before the
  app sees it — which silently overrides `trust_forwarded_for = false`.
- **`python -m uvicorn`, not `.venv/bin/uvicorn`.** venv console scripts bake in
  an absolute shebang, so they break the moment the checkout is moved.

Don't stop it with `pkill -f uvicorn…` — the pattern matches the shell running
the command too, so it kills your own session. Use `./run.sh stop`.

`probe.py` is stdlib-only and reads the same environment variables, so the same
script runs inside the container:

```bash
docker compose exec lms-request python scripts/probe.py
```

## Cut a release

Images are published to GitHub Container Registry by
[`.github/workflows/release.yml`](.github/workflows/release.yml). Tests run
first, so a failing build never publishes.

```bash
echo 0.2.0 > VERSION
git commit -am "Release 0.2.0"
git tag -a v0.2.0 -m "…" && git push origin root --tags
```

A tag publishes `:0.2.0`, `:0.2` and `:latest`. Pushes to `root` (the default
branch) publish `:edge`.

Pushing anything under `.github/workflows/` needs a token with the `workflow`
scope: `gh auth refresh -h github.com -s workflow`.

**A GHCR package is private until you say otherwise, even when its repository
is public** — the two visibilities are separate, and nothing in the workflow
changes it. Until it is flipped, every `docker pull` fails with `denied` and
consumers need `docker login ghcr.io` with a token carrying `read:packages`.
Publish it at Profile → Packages → the package → Package settings → Change
visibility. There is no API for it.

**Architecture.** The workflow builds `linux/amd64` only. For a Raspberry Pi,
add `linux/arm64` to `platforms:` — it works, but the emulated `pip install`
roughly doubles the job time.

## Layout

```
VERSION                     bumped per release; reported at /healthz
config.toml                 local settings (never baked into the image)
run.sh                      start / stop / restart / log, without Docker
Dockerfile
docker-compose.yml          build from source
docker-compose.deploy.yml   pull the published image
.env.example                deployment settings
.github/workflows/          tests on every PR, publish to GHCR on a tag
lmsrequest/
  lms.py                    JSON-RPC client, command helpers, artwork proxy
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
  demo_lms.py               stand-in LMS: run the UI with no music server
  make_social.py            render the repo's social card from og.html
docs/screenshots/           generated by demo_lms.py, not hand-captured
docs/social/                og.html and the 1280x640 card rendered from it
tests/                      unit tests, no network
```
