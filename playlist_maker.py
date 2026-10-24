#!/usr/bin/env python3
"""
playlist_maker.py: artist name or URL in, Spotify playlist out.

  python playlist_maker.py "Buffalo Tom"                  # Deezer top 20
  python playlist_maker.py "Buffalo Tom" -n 30
  python playlist_maker.py --url https://www.musicianwages.com/most-popular-buffalo-tom-songs/
  python playlist_maker.py --batch queue.txt              # many at once
  python playlist_maker.py "Buffalo Tom" --dry-run        # track list only, no Spotify

Spotify calls need the app owner's Premium to be active. --dry-run does not.
"""
import argparse
import base64
import csv
import json
import os
import re
import secrets
import sys
import time
import unicodedata
import webbrowser
from datetime import datetime
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

TOKEN_FILE = HERE / ".spotify_token.json"
OUT_DIR = HERE / "output"
SPOTIFY_API = "https://api.spotify.com/v1"
SPOTIFY_ACCOUNTS = "https://accounts.spotify.com"
SCOPES = "playlist-modify-public playlist-modify-private playlist-read-private"
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
BRAND = os.getenv("BRAND", "Chai Publications")
LLM_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")

# Words that mark an alternative version. Penalised unless the requested title has them too.
VERSION_WORDS = ["live", "demo", "remix", "acoustic", "instrumental", "karaoke",
                 "edit", "version", "session", "sessions", "rehearsal", "commentary",
                 "outtake", "alternate", "take", "mono", "unplugged"]


# ---------------------------------------------------------------- text helpers

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def base_title(t: str) -> str:
    """'Taillights Fade - 2011 Remaster' -> 'Taillights Fade'; '(Live)' etc. removed."""
    t = re.split(r"\s+[-\u2013\u2014]\s+", t)[0]
    stripped = re.sub(r"[\(\[].*?[\)\]]", "", t).strip()
    return stripped or t.strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(s)).strip("-")[:60]


# ---------------------------------------------------------------- sources

def deezer_top_tracks(artist: str, n: int):
    r = requests.get("https://api.deezer.com/search/artist", params={"q": artist}, timeout=20)
    r.raise_for_status()
    cands = r.json().get("data", [])
    if not cands:
        raise RuntimeError(f"Deezer found no artist matching '{artist}'")
    exact = [a for a in cands if norm(a["name"]) == norm(artist)]
    pick = max(exact or cands[:3], key=lambda a: a.get("nb_fan", 0))
    if not exact:
        print(f"  note: no exact Deezer match, using '{pick['name']}'")

    r = requests.get(f"https://api.deezer.com/artist/{pick['id']}/top",
                     params={"limit": min(n * 2, 100)}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"Deezer error: {body['error']}")

    titles, seen = [], set()
    for t in body.get("data", []):
        clean = base_title(t["title"])
        if norm(clean) in seen:
            continue
        seen.add(norm(clean))
        titles.append(clean)
        if len(titles) >= n:
            break
    return pick["name"], titles, "Deezer listening data"


def url_tracks(url: str, n, artist_hint=None):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env (needed to read URLs)")

    html = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (playlist-maker)"}).text
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript", "svg"]):
        tag.decompose()
    text = re.sub(r"\n\s*\n+", "\n", soup.get_text("\n"))[:60000]

    hint = f"The artist is probably {artist_hint}. " if artist_hint else ""
    prompt = (
        "Below is the text of a web page that lists songs by one music artist. "
        f"{hint}Return ONLY a JSON object, no other text: "
        '{"artist": "<artist name>", "tracks": ["<song title>", ...]}. '
        "List the song titles in the page's ranking, with #1 / most popular first "
        "(if the page counts down from 10 to 1, reverse it). Song titles only: no album "
        "names, numbers or commentary. Exclude songs mentioned only in passing that are "
        "not part of the list itself.\n\n--- PAGE TEXT ---\n" + text
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": LLM_MODEL, "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]},
        timeout=120,
    )
    r.raise_for_status()
    out = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
    data = json.loads(re.sub(r"```(?:json)?", "", out).strip())
    tracks = [base_title(t) for t in data["tracks"] if t.strip()]
    if n:
        tracks = tracks[:n]
    return data["artist"], tracks, urlparse(url).netloc.replace("www.", "")


# ---------------------------------------------------------------- Spotify auth

class _Callback(BaseHTTPRequestHandler):
    code = state = error = None

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        if "code" in q or "error" in q:
            _Callback.code = q.get("code", [None])[0]
            _Callback.state = q.get("state", [None])[0]
            _Callback.error = q.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<p>Done. You can close this tab.</p>")

    def log_message(self, *args):
        pass


def _token_request(data):
    cid, secret = os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"]
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = requests.post(f"{SPOTIFY_ACCOUNTS}/api/token", data=data,
                      headers={"Authorization": f"Basic {basic}"}, timeout=20)
    r.raise_for_status()
    tok = r.json()
    tok["expires_at"] = time.time() + tok["expires_in"] - 60
    return tok


def get_token() -> str:
    if TOKEN_FILE.exists():
        tok = json.loads(TOKEN_FILE.read_text())
        if time.time() < tok["expires_at"]:
            return tok["access_token"]
        try:
            new = _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
            new.setdefault("refresh_token", tok["refresh_token"])
            TOKEN_FILE.write_text(json.dumps(new))
            return new["access_token"]
        except requests.HTTPError:
            print("Stored login has expired (they last ~6 months). Logging in again.")

    state = secrets.token_urlsafe(16)
    auth_url = f"{SPOTIFY_ACCOUNTS}/authorize?" + urlencode({
        "client_id": os.environ["SPOTIFY_CLIENT_ID"], "response_type": "code",
        "redirect_uri": REDIRECT_URI, "scope": SCOPES, "state": state,
    })
    port = urlparse(REDIRECT_URI).port or 8888
    print("Opening browser for Spotify login. Log in as the account that should own the playlists.")
    print(f"If nothing opens, paste this into a browser:\n{auth_url}")
    webbrowser.open(auth_url)
    server = HTTPServer(("127.0.0.1", port), _Callback)
    while _Callback.code is None and _Callback.error is None:
        server.handle_request()
    server.server_close()
    if _Callback.error:
        raise SystemExit(f"Spotify login refused: {_Callback.error}")
    if _Callback.state != state:
        raise SystemExit("Login state mismatch; try again.")
    tok = _token_request({"grant_type": "authorization_code", "code": _Callback.code,
                          "redirect_uri": REDIRECT_URI})
    TOKEN_FILE.write_text(json.dumps(tok))
    return tok["access_token"]


def sp(method, path, token, **kw):
    for attempt in range(6):
        r = requests.request(method, SPOTIFY_API + path, timeout=30,
                             headers={"Authorization": f"Bearer {token}"}, **kw)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "5")) + 1
            if wait > 900:
                raise SystemExit(f"Spotify quota exhausted; it asks for a {wait // 60}-minute wait. Run again later.")
            print(f"  rate-limited, waiting {wait}s")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 403:
            raise SystemExit(
                f"403 from Spotify on {path}: {r.text[:300]}\n"
                "Check: app owner's Premium is active; the logged-in account is on the app's user list.")
        r.raise_for_status()
        return r.json() if r.content else {}
    raise SystemExit(f"Gave up on {path} after repeated failures")


# ---------------------------------------------------------------- matching

def score(cand, title, artist) -> float:
    if norm(artist) not in [norm(a["name"]) for a in cand.get("artists", [])]:
        return -1
    cand_name = norm(cand["name"])
    wanted = norm(title)
    s = SequenceMatcher(None, norm(base_title(cand["name"])), norm(base_title(title))).ratio()
    if s < 0.8:
        return -1
    for w in VERSION_WORDS:
        pat = rf"\b{w}\b"
        if re.search(pat, cand_name) and not re.search(pat, wanted):
            s -= 0.3
    if "remaster" in cand_name:
        s -= 0.05
    album = cand.get("album", {})
    if album.get("album_type") == "compilation":
        s -= 0.15
    if re.search(r"\b(live|greatest hits|best of|anthology|essential)\b", norm(album.get("name", ""))):
        s -= 0.1
    s += (cand.get("popularity") or 0) / 2000          # tie-breaker only
    return s


def find_track(title, artist, token):
    queries = [f'track:"{title}" artist:"{artist}"', f"{title} {artist}"]
    best, best_s = None, -1
    for q in queries:
        items = sp("GET", "/search", token, params={"q": q, "type": "track", "limit": 10})
        for cand in items.get("tracks", {}).get("items", []):
            s = score(cand, title, artist)
            if s > best_s:
                best, best_s = cand, s
        if best is not None:
            break
        time.sleep(0.3)
    return best


def existing_playlist(name, token):
    offset = 0
    while True:
        page = sp("GET", "/me/playlists", token, params={"limit": 50, "offset": offset})
        for p in page.get("items", []):
            if p and p.get("name") == name:
                return p
        if not page.get("next"):
            return None
        offset += 50


# ---------------------------------------------------------------- main job

def run_one(target, n, args, token=None):
    is_url = target.startswith("http")
    if is_url:
        artist, titles, source = url_tracks(target, n, args.artist)
    else:
        artist, titles, source = deezer_top_tracks(target, n or 20)
    if not titles:
        print(f"No tracks found for {target}")
        return None

    print(f"\n{artist}: {len(titles)} tracks from {source}")
    name = args.name.format(artist=artist, count=len(titles))
    record = {"target": target, "artist": artist, "source": source, "playlist_name": name,
              "requested": titles, "matched": [], "unmatched": [], "playlist_url": None,
              "run_at": datetime.now().isoformat(timespec="seconds")}

    if args.dry_run:
        for i, t in enumerate(titles, 1):
            print(f"  {i:>2}. {t}")
        save(record)
        return record

    token = token or get_token()
    if not args.force:
        ex = existing_playlist(name, token)
        if ex:
            print(f"  already exists, skipping (use --force to make another): {ex['external_urls']['spotify']}")
            return None

    uris, seen = [], set()
    for t in titles:
        hit = find_track(t, artist, token)
        if hit and hit["uri"] not in seen:
            seen.add(hit["uri"])
            uris.append(hit["uri"])
            record["matched"].append({"requested": t, "spotify": hit["name"],
                                      "album": hit.get("album", {}).get("name"), "uri": hit["uri"]})
            print(f"  ok   {t}  ->  {hit['name']}  [{hit.get('album', {}).get('name')}]")
        elif not hit:
            record["unmatched"].append(t)
            print(f"  MISS {t}")
        time.sleep(0.3)

    if not uris:
        print("  nothing matched; no playlist created")
        save(record)
        return record

    desc = f"Compiled by {BRAND}. Source: {source}."
    pl = sp("POST", "/me/playlists", token, json={"name": name, "description": desc, "public": not args.private})
    for i in range(0, len(uris), 100):
        sp("POST", f"/playlists/{pl['id']}/items", token, json={"uris": uris[i:i + 100]})
    record["playlist_url"] = pl["external_urls"]["spotify"]
    print(f"  created: {record['playlist_url']}  ({len(uris)}/{len(titles)} matched)")
    save(record)
    return record


def save(record):
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"{slug(record['artist'])}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False))
    log = OUT_DIR / "log.csv"
    new = not log.exists()
    with log.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["run_at", "artist", "source", "requested", "matched", "unmatched", "playlist_url"])
        w.writerow([record["run_at"], record["artist"], record["source"], len(record["requested"]),
                    len(record["matched"]), "; ".join(record["unmatched"]), record["playlist_url"] or ""])


def read_batch(path):
    jobs = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        target, _, num = line.partition("|")
        jobs.append((target.strip(), int(num) if num.strip().isdigit() else None))
    return jobs


def main():
    ap = argparse.ArgumentParser(description="Artist or URL in, Spotify playlist out.")
    ap.add_argument("target", nargs="?", help="artist name")
    ap.add_argument("--url", help="page listing an artist's songs")
    ap.add_argument("--artist", help="artist name hint when using --url")
    ap.add_argument("--batch", help="text file: one artist or URL per line, optional '| N'")
    ap.add_argument("-n", type=int, help="number of tracks (default 20 for names, all for URLs)")
    ap.add_argument("--name", default=os.getenv("NAME_TEMPLATE", "{artist}: Top {count}"),
                    help="playlist name template; {artist} and {count} available")
    ap.add_argument("--private", action="store_true", help="make playlists private")
    ap.add_argument("--dry-run", action="store_true", help="fetch track lists only; no Spotify")
    ap.add_argument("--force", action="store_true", help="create even if a same-named playlist exists")
    ap.add_argument("--pause", type=float, default=10, help="seconds between batch items")
    args = ap.parse_args()

    if args.batch:
        jobs = read_batch(args.batch)
    elif args.url:
        jobs = [(args.url, args.n)]
    elif args.target:
        jobs = [(args.target, args.n)]
    else:
        ap.print_help()
        sys.exit(1)

    token = None if args.dry_run else get_token()
    for i, (target, n) in enumerate(jobs):
        try:
            run_one(target, n or args.n, args, token)
        except (RuntimeError, requests.HTTPError, json.JSONDecodeError, KeyError) as e:
            print(f"\nFAILED {target}: {e}")
        if i < len(jobs) - 1:
            time.sleep(args.pause)
            if not args.dry_run:
                token = get_token()   # refreshes if the hour-long token has lapsed


if __name__ == "__main__":
    main()
