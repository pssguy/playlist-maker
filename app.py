#!/usr/bin/env python3
"""
Playlist Maker UI. Double-click "Playlist Maker.bat" (or run: python app.py).
Opens http://127.0.0.1:8765 in your browser. Use the Quit button there to stop it.
"""
import json
import os
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)

# pythonw has no console: send output to a log file instead.
if sys.stdout is None or sys.stderr is None:
    log = open(HERE / "app.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = log

import playlist_maker as pm  # noqa: E402

PORT = 8765
UI_FILE = HERE / "ui.html"
NAME_TEMPLATE = os.getenv("NAME_TEMPLATE", "{artist}: Top {count}")
SERVER = None


def to_row(requested, hit):
    if not hit:
        return {"requested": requested, "uri": None}
    album = hit.get("album", {}) or {}
    images = album.get("images") or []
    return {
        "requested": requested,
        "uri": hit["uri"],
        "name": hit["name"],
        "album": album.get("name", ""),
        "year": (album.get("release_date") or "")[:4],
        "image": images[-1]["url"] if images else None,
    }


def api_lookup(d):
    target = (d.get("target") or "").strip()
    if not target:
        raise RuntimeError("Enter an artist name or a URL.")
    n = int(d["n"]) if str(d.get("n") or "").isdigit() else None
    if target.startswith("http"):
        artist, titles, source = pm.url_tracks(target, n)
    else:
        artist, titles, source = pm.deezer_top_tracks(target, n or 20)
    if not titles:
        raise RuntimeError(f"No songs found for {target}.")

    token = pm.get_token()
    rows, seen = [], set()
    for t in titles:
        hit = pm.find_track(t, artist, token)
        if hit and hit["uri"] in seen:
            continue
        if hit:
            seen.add(hit["uri"])
        rows.append(to_row(t, hit))
        time.sleep(0.2)
    return {"artist": artist, "source": source, "target": target,
            "template": NAME_TEMPLATE, "rows": rows}


def api_add(d):
    title, artist = (d.get("title") or "").strip(), (d.get("artist") or "").strip()
    if not title or not artist:
        raise RuntimeError("Need a song title.")
    hit = pm.find_track(pm.base_title(title), artist, pm.get_token())
    return to_row(title, hit)


def api_create(d):
    name = (d.get("name") or "").strip()
    tracks = [t for t in d.get("tracks", []) if t.get("uri")]
    if not name:
        raise RuntimeError("The playlist needs a name.")
    if not tracks:
        raise RuntimeError("No tracks left to add.")

    token = pm.get_token()
    if not d.get("force"):
        existing = pm.existing_playlist(name, token)
        if existing:
            return {"exists": existing["external_urls"]["spotify"]}

    desc = f"Compiled by {pm.BRAND}. Source: {d.get('source', 'various')}."
    pl = pm.sp("POST", "/me/playlists", token,
               json={"name": name, "description": desc, "public": bool(d.get("public", True))})
    uris = [t["uri"] for t in tracks]
    for i in range(0, len(uris), 100):
        pm.sp("POST", f"/playlists/{pl['id']}/items", token, json={"uris": uris[i:i + 100]})
    url = pl["external_urls"]["spotify"]

    pm.save({
        "target": d.get("target", ""), "artist": d.get("artist", name), "source": d.get("source", ""),
        "playlist_name": name, "requested": d.get("requested", []),
        "matched": [{"requested": t.get("requested"), "spotify": t.get("name"),
                     "album": t.get("album"), "uri": t["uri"]} for t in tracks],
        "unmatched": d.get("unmatched", []), "playlist_url": url,
        "run_at": datetime.now().isoformat(timespec="seconds"),
    })
    return {"url": url, "count": len(uris)}


def api_quit(_):
    threading.Timer(0.3, SERVER.shutdown).start()
    return {"ok": True}


ROUTES = {"/api/lookup": api_lookup, "/api/add": api_add,
          "/api/create": api_create, "/api/quit": api_quit}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        fn = ROUTES.get(self.path)
        if not fn:
            return self._send(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self._send(200, fn(payload))
        except RuntimeError as e:        # expected problems: show the message as-is
            self._send(400, {"error": str(e)})
        except SystemExit as e:          # playlist_maker uses SystemExit for Spotify errors
            print(f"[{datetime.now():%H:%M:%S}] {e}")
            self._send(500, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    global SERVER
    url = f"http://127.0.0.1:{PORT}/"
    try:
        SERVER = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        webbrowser.open(url)            # already running: just show it
        return
    print(f"[{datetime.now():%H:%M:%S}] Playlist Maker running at {url}")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    SERVER.serve_forever()
    print(f"[{datetime.now():%H:%M:%S}] stopped")


if __name__ == "__main__":
    main()
