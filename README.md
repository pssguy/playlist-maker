# playlist-maker

Artist name or URL in, Spotify playlist out.

## Setup (once)
1. `pip install -r requirements.txt`
2. Spotify developer dashboard: create an app, tick **Web API**, add redirect URI
   `http://127.0.0.1:8888/callback` exactly.
3. Copy `.env.example` to `.env`, paste Client ID and Secret.

## Use
    python playlist_maker.py "Buffalo Tom" --dry-run     # check the list, no Spotify
    python playlist_maker.py "Buffalo Tom" -n 20
    python playlist_maker.py --url <page>
    python playlist_maker.py --batch queue.txt

First real run opens a browser to log in; the login is cached in `.spotify_token.json`
and lasts about six months. Results and misses go to `output/` (one JSON per artist,
plus `log.csv`). Re-running a batch skips playlists that already exist.

## Without the terminal
Make a desktop shortcut (right-click desktop, New > Shortcut) with this location,
adjusting the path to where you put the folder:

    pythonw "C:\dev\playlist-maker\app.py"

Double-click it and a page opens in your browser at http://127.0.0.1:8765: enter an
artist or URL, remove/reorder/add songs, then Create playlist. Use Quit on the page
to stop it. Problems are written to `app.log`.

If the shortcut does nothing, use the full path to `pythonw.exe` (run `where python`
in a terminal to find the folder).
