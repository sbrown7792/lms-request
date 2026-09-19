"""A stand-in LMS, complete enough to run LMS Request with nothing else.

Speaks the JSON-RPC subset lmsrequest/lms.py and tidal.py actually call, and
serves generated cover art from /imageproxy/..., so the artwork proxy is
exercised for real. Everything in it is invented -- no real artists, no real
TIDAL account, and no risk of starting music in someone's house.

This is how docs/screenshots/ are made, and it is a quick way to click around
the UI without a music server. Needs Pillow for the cover art.

    python3 scripts/demo_lms.py &

    LMSREQUEST_LMS_PORT=9999 LMSREQUEST_LMS_HOST=127.0.0.1 \
    LMSREQUEST_DATA_DIR=/tmp/demo LMSREQUEST_HOST_TOKEN=demo \
    LMSREQUEST_PUBLIC_URL=https://requests.example.com ./run.sh start

Then open /host, pick a player, pick a playlist and press Load. /state is a
test hook, not part of LMS: GET /state?cur=3&mode=play&elapsed=84 moves the
demo player along so a screenshot can show a track partway through.
"""
import io, json, random, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from PIL import Image, ImageDraw

PORT = 9999

PLAYERS = [
    ("Living Room", "aa:bb:cc:00:00:01", "squeezelite"),
    ("Kitchen",     "aa:bb:cc:00:00:02", "squeezelite"),
    ("Patio",       "aa:bb:cc:00:00:03", "squeezelite"),
    ("_Whole House","aa:bb:cc:00:00:09", "group"),
]

PLAYLISTS = [
    "Friday Night Floorfillers", "Back Deck BBQ", "Kitchen Disco",
    "Slow Burn", "Nineties Throwbacks", "Late Night Wind-Down",
    "Sunday Morning Coffee", "Long Drive Home",
]

TRACKS = [
    ("Midnight on Marlow Street", "Velvet Antenna",       "Neon Hour"),
    ("Cassette Summer",           "Sunset Commute",       "Cassette Summer"),
    ("Hold the Line Open",        "The Glasshouse Effect","Frequencies"),
    ("Gold Rush Hour",            "Marla Quinn",          "Gold Rush Hour"),
    ("Paper Aeroplanes",          "Kite String Theory",   "Altitude"),
    ("Slow Motion Riot",          "The Loud Quiet",       "Slow Motion Riot"),
    ("Static in the Kitchen",     "Ivory Static",         "Household Names"),
    ("Long Way Round",            "Pale Harbour",         "Tidewater"),
    ("Ceiling Fan Season",        "Orchard Street",       "Ceiling Fan Season"),
    ("Blue Hour Radio",           "Bellwether",           "Blue Hour Radio"),
    ("Runaway Tram",              "Dust & Diamonds",      "City Limits"),
    ("All the Lights Downtown",   "The Hollow Coast",     "After Hours"),
    ("Harbour Lights",            "Velvet Antenna",       "Neon Hour"),
    ("Second Wind",               "Marla Quinn",          "Gold Rush Hour"),
    ("Telegraph Hill",            "Pale Harbour",         "Tidewater"),
    ("Everything Louder",         "The Loud Quiet",       "Slow Motion Riot"),
]

BY_URL = {f"tidal://{100 + i}.flc": t for i, t in enumerate(TRACKS)}
URLS = list(BY_URL)

# Two hues per cover, picked once so a track keeps its artwork.
PALETTE = [(14, 165, 180), (236, 72, 100), (250, 176, 60), (129, 90, 230),
           (34, 180, 120), (240, 110, 60), (70, 130, 240), (220, 70, 170)]


def cover(seed: int, size: int = 600) -> bytes:
    rnd = random.Random(seed)
    top = PALETTE[seed % len(PALETTE)]
    bottom = PALETTE[(seed * 5 + 3) % len(PALETTE)]
    img = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(img)
    for y in range(size):                      # vertical gradient
        f = y / size
        draw.line([(0, y), (size, y)],
                  fill=tuple(int(top[c] + (bottom[c] - top[c]) * f) for c in range(3)))
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for _ in range(3):                         # a few translucent shapes
        r = rnd.randint(size // 6, size // 2)
        x, y = rnd.randint(0, size), rnd.randint(0, size)
        od.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, rnd.randint(14, 40)))
    od.rectangle([0, size * 0.72, size, size], fill=(0, 0, 0, 48))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


class State:
    def __init__(self):
        self.queue: list[str] = []
        self.cur = 0
        self.mode = "stop"
        self.elapsed = 0.0


S = State()


def art_ref(url: str) -> str:
    return f"/imageproxy/{url.split('//')[1].split('.')[0]}/cover.jpg"


def tidal_items(cmd):
    args = {a.split(":", 1)[0]: a.split(":", 1)[1]
            for a in cmd[4:] if isinstance(a, str) and ":" in a}
    node = args.get("item_id")
    query = args.get("search", "")
    menu = args.get("menu")

    if node is None:                                   # top level
        rows = [{"id": str(i), "name": n} for i, n in enumerate(
            ["Home", "Featured", "My Mix", "Playlists", "Albums", "Songs",
             "Artists", "Search", "Genres", "Moods"])]
        rows[7]["type"] = "search"
        return {"count": len(rows), "loop_loop": rows}

    if node == "3":                                    # saved playlists
        rows = [{"id": f"3.{i}", "name": n, "type": "playlist",
                 "image": art_ref(URLS[i % len(URLS)])}
                for i, n in enumerate(PLAYLISTS)]
        return {"count": len(rows), "loop_loop": rows}

    if node.startswith("3."):                          # tracks of a playlist
        rows = [{"id": f"{node}.{i}", "name": t[0], "url": u,
                 "image": art_ref(u)}
                for i, (u, t) in enumerate(BY_URL.items())]
        return {"count": len(rows), "loop_loop": rows}

    if node == "7":                                    # search categories
        rows = [{"id": f"7_{query}.{i}", "name": n} for i, n in enumerate(
            ["Everything", "Playlists", "Artists", "Albums", "Songs"])]
        return {"count": len(rows), "loop_loop": rows}

    if node.startswith("7_") and menu:                 # search results
        q = query.lower()
        # Matches first, then the rest as padding: TIDAL returns a long list
        # for any query, and a two-row result would misrepresent the page.
        hit = [(u, t) for u, t in BY_URL.items()
               if q in t[0].lower() or q in t[1].lower()]
        rest = [(u, t) for u, t in BY_URL.items() if (u, t) not in hit]
        hits = hit + rest
        rows = [{"text": f"{t[0]}\n{t[1]}", "icon": art_ref(u),
                 "presetParams": {"favorites_url": u}} for u, t in hits]
        return {"count": len(rows), "item_loop": rows}

    return {"count": 0, "loop_loop": []}


def status(cmd):
    start, count = int(cmd[1]), int(cmd[2])
    window = S.queue[start:start + count] if count else []
    loop = []
    for offset, url in enumerate(window):
        title, artist, album = BY_URL[url]
        loop.append({"playlist index": start + offset, "url": url, "title": title,
                     "artist": artist, "album": album, "artwork_url": art_ref(url)})
    out = {"playlist_cur_index": S.cur, "playlist_tracks": len(S.queue),
           "playlist_timestamp": 1, "mode": S.mode, "time": S.elapsed,
           "duration": 214.0}
    if loop:
        out["playlist_loop"] = loop
    return out


def playlist(cmd, _player):
    verb = cmd[1]
    if verb == "clear":
        S.queue.clear(); S.cur = 0
    elif verb == "add":
        S.queue.append(cmd[2])
    elif verb == "move":
        S.queue.insert(int(cmd[3]), S.queue.pop(int(cmd[2])))
    elif verb == "deleteitem":
        S.queue.pop(int(cmd[2]))
    elif verb == "index":
        S.cur = S.cur + 1 if cmd[2] == "+1" else int(cmd[2])
    return {}


def dispatch(player, cmd):
    head = cmd[0]
    if head == "players":
        return {"count": len(PLAYERS), "players_loop": [
            {"playerid": pid, "name": n, "model": m, "connected": 1, "power": 1}
            for n, pid, m in PLAYERS]}
    if head == "status":
        return status(cmd)
    if head == "tidal":
        return tidal_items(cmd)
    if head == "titles":
        return {"count": 0, "titles_loop": []}
    if head == "playlist":
        return playlist(cmd, player)
    if head == "play":
        S.mode = "play"
    elif head == "pause":
        S.mode = "pause" if (len(cmd) < 2 or cmd[1]) else "play"
    return {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/imageproxy/"):
            seed = int(re.search(r"/imageproxy/(\d+)", self.path).group(1))
            body = cover(seed)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/state"):             # test hook, not LMS
            args = dict(p.split("=") for p in self.path.split("?")[1].split("&"))
            if "cur" in args: S.cur = int(args["cur"])
            if "mode" in args: S.mode = args["mode"]
            if "elapsed" in args: S.elapsed = float(args["elapsed"])
            body = json.dumps({"cur": S.cur, "mode": S.mode,
                               "tracks": len(S.queue)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        player, cmd = payload["params"]
        body = json.dumps({"id": 1, "result": dispatch(player, cmd)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"fake LMS on {PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
