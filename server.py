"""Cadáver Exquisito: servidor del juego.

Sin dependencias para jugar en local (guarda las partidas en data.json).
Si existe la variable DATABASE_URL, guarda en PostgreSQL (necesita psycopg).

    python server.py            -> http://localhost:8000
"""

import base64
import json
import os
import queue
import re
import secrets
import threading
import time
import unicodedata
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
PORT = int(os.environ.get("PORT", "8000"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATA_FILE = Path(os.environ.get("DATA_FILE", ROOT / "data.json"))

TURN_SECONDS = 4 * 60
MAX_GAMES = 300
MAX_LINE = 160
MAX_TITLE = 80
MAX_NAME = 40
ALLOWED_TURNS = {4, 6, 8, 10, 12, 16, 20, 25, 30, 35, 40}
MAX_PLACE = 60
MAX_TURNS = 60
ID_RE = re.compile(r"^[a-z0-9]{6,32}$")
PLAYER_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_WINDOWS = 120
MAX_IMAGE = 700_000  # bytes, after the browser shrinks the photo


# ---------------------------------------------------------------- storage

class FileStore:
    def __init__(self, path):
        self.path = path

    def load(self):
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, game):
        self._dump()

    def delete(self, gid):
        self._dump()

    def _dump(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(GAMES, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)


class PgStore:
    def __init__(self, url):
        import psycopg  # instalado en Render desde requirements.txt

        self.psycopg = psycopg
        self.url = url
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS games (id TEXT PRIMARY KEY, data JSONB NOT NULL)")

    def _conn(self):
        return self.psycopg.connect(self.url, autocommit=True)

    def load(self):
        with self._conn() as c:
            rows = c.execute("SELECT id, data FROM games").fetchall()
        return {gid: (data if isinstance(data, dict) else json.loads(data)) for gid, data in rows}

    def save(self, game):
        with self._conn() as c:
            c.execute(
                "INSERT INTO games (id, data) VALUES (%s, %s) "
                "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
                (game["id"], json.dumps(game, ensure_ascii=False)),
            )

    def delete(self, gid):
        with self._conn() as c:
            c.execute("DELETE FROM games WHERE id = %s", (gid,))


class FileWinStore:
    """Windows (photos) live apart from the games, so the poem is never touched."""

    def __init__(self, path):
        self.path = path

    def load(self):
        if not self.path.exists():
            return {}, {}
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}, {}
        metas, images = {}, {}
        for wid, w in raw.items():
            images[wid] = base64.b64decode(w.pop("img"))
            metas[wid] = w
        return metas, images

    def save(self, meta, image):
        self._dump()

    def delete(self, wid):
        self._dump()

    def _dump(self):
        out = {wid: {**m, "img": base64.b64encode(IMAGES[wid]).decode()} for wid, m in WINDOWS.items()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)


class PgWinStore:
    def __init__(self, url):
        import psycopg

        self.psycopg = psycopg
        self.url = url
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS windows (id TEXT PRIMARY KEY, data JSONB NOT NULL, image BYTEA NOT NULL)")

    def _conn(self):
        return self.psycopg.connect(self.url, autocommit=True)

    def load(self):
        with self._conn() as c:
            rows = c.execute("SELECT id, data, image FROM windows").fetchall()
        metas = {wid: (d if isinstance(d, dict) else json.loads(d)) for wid, d, _ in rows}
        images = {wid: bytes(img) for wid, _, img in rows}
        return metas, images

    def save(self, meta, image):
        with self._conn() as c:
            c.execute(
                "INSERT INTO windows (id, data, image) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, image = EXCLUDED.image",
                (meta["id"], json.dumps(meta, ensure_ascii=False), image),
            )

    def delete(self, wid):
        with self._conn() as c:
            c.execute("DELETE FROM windows WHERE id = %s", (wid,))


LOCK = threading.RLock()
STORE = PgStore(DATABASE_URL) if DATABASE_URL else FileStore(DATA_FILE)
GAMES = STORE.load()
try:
    WIN_STORE = PgWinStore(DATABASE_URL) if DATABASE_URL else FileWinStore(DATA_FILE.with_name(DATA_FILE.stem + "-ventanas.json"))
    WINDOWS, IMAGES = WIN_STORE.load()
except Exception as e:  # noqa: BLE001  never let the photos take the poem down
    print(f"AVISO: no se pudieron cargar las ventanas ({e!r}). El poema sigue funcionando.", flush=True)
    WIN_STORE = FileWinStore(DATA_FILE.with_name(DATA_FILE.stem + "-ventanas.json"))
    WINDOWS, IMAGES = {}, {}

# ---------------------------------------------------------------- live updates

LISTENERS = set()
LISTENERS_LOCK = threading.Lock()


def broadcast():
    payload = json.dumps(public_list(), ensure_ascii=False)
    with LISTENERS_LOCK:
        for q in list(LISTENERS):
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


# ---------------------------------------------------------------- game logic

def now():
    return time.time()


def clean(text, limit):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def writing(game):
    return bool(game.get("writingBy")) and game.get("writingUntil", 0) > now()


def public_game(g):
    """What every visitor may see. Hidden verses never leave the server."""
    lines = g.get("lines", [])
    base = {
        "id": g["id"],
        "title": g["title"],
        "turns": g["turns"],
        "peek": g["peek"],
        "createdAt": g["createdAt"],
        "createdByName": g.get("createdByName") or "",
        "status": g["status"],
        "count": len(lines),
        "hands": len({l["by"] for l in lines}),
    }
    if g["status"] == "done":
        base["lines"] = [{"text": l["text"], "name": l.get("name") or "", "place": l.get("place") or "",
                          "window": line_window(g, i)} for i, l in enumerate(lines)]
    else:
        last = lines[-1]["text"] if lines else ""
        base["peekWords"] = " ".join(last.split()[-g["peek"]:]) if last else ""
        base["writing"] = writing(g)
        base["writingName"] = g.get("writingName") or "" if writing(g) else ""
        base["writingUntil"] = g.get("writingUntil", 0) if writing(g) else 0
    return base


def public_list():
    games = sorted(GAMES.values(), key=lambda g: g["createdAt"], reverse=True)
    return {"games": [public_game(g) for g in games], "windows": public_windows(), "serverTime": now()}


# ---------------------------------------------------------------- windows

def norm_name(name):
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


def window_for(pid, name):
    """The window of whoever wrote a verse: same browser first, then same name."""
    for w in WINDOWS.values():
        if pid and w.get("playerId") == pid:
            return w["id"]
    key = norm_name(name)
    if key:
        for w in sorted(WINDOWS.values(), key=lambda w: w["at"]):
            if norm_name(w.get("name")) == key:
                return w["id"]
    return None


def line_window(g, i):
    """The window shown with verse i+1: a link the coordinator set by hand wins, otherwise the automatic match."""
    links = g.get("links") or {}
    key = str(i + 1)
    if key in links:
        wid = links[key]
        return wid if wid in WINDOWS else None
    l = g["lines"][i]
    taken = set(links.values())
    for w in WINDOWS.values():
        if l.get("by") and w.get("playerId") == l.get("by"):
            return None if w["id"] in taken else w["id"]
    name = norm_name(l.get("name"))
    if name:
        for w in sorted(WINDOWS.values(), key=lambda w: w["at"]):
            if w["id"] not in taken and norm_name(w.get("name")) == name:
                return w["id"]
    return None


def public_window(w):
    return {"id": w["id"], "name": w.get("name") or "", "place": w.get("place") or "", "color": w["color"],
            "w": w.get("w", 0), "h": w.get("h", 0), "v": int(w.get("updated", w["at"]) * 1000)}


def public_windows():
    return [public_window(w) for w in sorted(WINDOWS.values(), key=lambda w: w["at"])]


def own_window(pid):
    return next((w for w in WINDOWS.values() if w.get("playerId") == pid), None)


def save_window(body):
    pid = need_player(body)
    color = str(body.get("color") or "")
    if not COLOR_RE.match(color):
        raise Fail(400, "Elegí un color para tu ventana.")
    mine = own_window(pid)
    raw = str(body.get("image") or "")
    if not raw and mine:
        image = IMAGES[mine["id"]]  # only the color, name or place changed
    else:
        if "," in raw[:40]:
            raw = raw.split(",", 1)[1]
        try:
            image = base64.b64decode(raw, validate=True)
        except ValueError:
            raise Fail(400, "Elegí una foto para tu ventana.")
        if not image.startswith(b"\xff\xd8"):
            raise Fail(400, "No pude leer la foto. Probá con una JPG o PNG.")
        if len(image) > MAX_IMAGE:
            raise Fail(413, "La foto es demasiado pesada. Probá con otra.")
    if not mine and len(WINDOWS) >= MAX_WINDOWS:
        raise Fail(507, "Ya se llegó al máximo de ventanas.")
    wid = mine["id"] if mine else secrets.token_hex(5)
    meta = {
        "id": wid, "playerId": pid,
        "name": clean(body.get("name"), MAX_NAME),
        "place": clean(body.get("place"), MAX_PLACE),
        "color": color.lower(),
        "w": int(body.get("w") or (mine or {}).get("w", 0)), "h": int(body.get("h") or (mine or {}).get("h", 0)),
        "at": now() if not mine else mine["at"],
        "updated": now(),
    }
    WINDOWS[wid] = meta
    IMAGES[wid] = image
    WIN_STORE.save(meta, image)
    return {"window": public_window(meta)}


def delete_window(body):
    pid = need_player(body)
    mine = own_window(pid)
    if mine:
        del WINDOWS[mine["id"]]
        IMAGES.pop(mine["id"], None)
        WIN_STORE.delete(mine["id"])
    return {}


def my_window(body):
    pid = need_player(body)
    mine = own_window(pid)
    return {"window": public_window(mine) if mine else None}


class Fail(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def need_game(gid):
    g = GAMES.get(gid)
    if not g:
        raise Fail(404, "Esta partida no existe o fue borrada.")
    return g


def need_player(body):
    pid = str(body.get("playerId") or "")
    if not PLAYER_RE.match(pid):
        raise Fail(400, "Falta el identificador del jugador. Recargá la página.")
    return pid


def create_game(body):
    pid = need_player(body)
    turns = int(body.get("turns") or 8)
    peek = int(body.get("peek") or 3)
    if turns not in ALLOWED_TURNS or not 1 <= peek <= 5:
        raise Fail(400, "Ajustes de partida no válidos.")
    if len(GAMES) >= MAX_GAMES:
        raise Fail(507, "Hay demasiadas partidas guardadas. Borrá alguna vieja e intentalo otra vez.")
    gid = secrets.token_hex(5)
    g = {
        "id": gid,
        "title": clean(body.get("title"), MAX_TITLE) or "Sin título",
        "turns": turns,
        "peek": peek,
        "createdBy": pid,
        "createdByName": clean(body.get("name"), MAX_NAME),
        "createdAt": now(),
        "status": "open",
        "lines": [],
        "writingBy": None,
        "writingName": "",
        "writingUntil": 0,
    }
    GAMES[gid] = g
    STORE.save(g)
    return {"id": gid}


def take_turn(g, body):
    pid = need_player(body)
    if g["status"] == "done":
        raise Fail(409, "La partida ya se reveló.")
    if writing(g) and g["writingBy"] != pid:
        raise Fail(409, "Otra persona acaba de tomar el turno. Esperá a que termine.")
    g["writingBy"] = pid
    g["writingName"] = clean(body.get("name"), MAX_NAME)
    g["writingUntil"] = now() + TURN_SECONDS
    STORE.save(g)
    return {"until": g["writingUntil"], "peekWords": public_game(g)["peekWords"]}


def add_line(g, body):
    pid = need_player(body)
    text = clean(body.get("text"), MAX_LINE)
    if not text:
        raise Fail(400, "Escribí algo antes de doblar la hoja.")
    if g["status"] == "done":
        raise Fail(409, "La partida ya se reveló. Tu verso no se guardó.")
    # A lapsed turn still counts if nobody else took it meanwhile.
    if writing(g) and g["writingBy"] != pid:
        raise Fail(409, "Se acabó tu tiempo y otra persona tomó el turno. Tu verso no se guardó.")
    g["lines"].append({"text": text, "by": pid, "name": clean(body.get("name"), MAX_NAME),
                       "place": clean(body.get("place"), MAX_PLACE), "at": now()})
    g["writingBy"], g["writingName"], g["writingUntil"] = None, "", 0
    if len(g["lines"]) >= g["turns"]:
        g["status"] = "done"
    STORE.save(g)
    return {"done": g["status"] == "done"}


def release(g, body):
    pid = need_player(body)
    if g.get("writingBy") == pid:
        g["writingBy"], g["writingName"], g["writingUntil"] = None, "", 0
        STORE.save(g)
    return {}


def reveal(g, body):
    pid = need_player(body)
    if g["createdBy"] != pid:
        raise Fail(403, "Solo quien abrió la partida puede revelarla antes de tiempo.")
    g["status"] = "done"
    g["writingBy"], g["writingName"], g["writingUntil"] = None, "", 0
    STORE.save(g)
    return {}


def delete_game(g, body):
    pid = need_player(body)
    if g["createdBy"] != pid:
        raise Fail(403, "Solo quien abrió la partida puede borrarla.")
    del GAMES[g["id"]]
    STORE.delete(g["id"])
    return {}


def set_turns(g, body):
    pid = need_player(body)
    if g["createdBy"] != pid:
        raise Fail(403, "Solo quien abrió la partida puede cambiar la cantidad de versos.")
    if g["status"] == "done":
        raise Fail(409, "La partida ya se reveló.")
    try:
        turns = int(body.get("turns"))
    except (TypeError, ValueError):
        raise Fail(400, "Escribí un número de versos.")
    count = len(g.get("lines", []))
    if turns <= count:
        raise Fail(400, f"Ya hay {count} versos escritos. Elegí un número mayor, o usá «Revelar antes de tiempo» para cerrar el poema.")
    if turns > MAX_TURNS:
        raise Fail(400, f"El máximo es {MAX_TURNS} versos.")
    g["turns"] = turns
    STORE.save(g)
    return {"turns": turns}


def my_lines(g, body):
    """Only the asking player's own verses: lets each person check their verse was saved."""
    pid = need_player(body)
    lines = g.get("lines", [])
    return {"total": g["turns"], "lines": [{"n": i + 1, "text": l["text"]} for i, l in enumerate(lines) if l.get("by") == pid]}


def need_creator(g, body):
    if g["createdBy"] != need_player(body):
        raise Fail(403, "Solo quien abrió la partida puede unir fotos y versos.")


def link_info(g, body):
    """For the coordinator only: who signed each verse (names, never the text) and which photo goes with it."""
    need_creator(g, body)
    links = g.get("links") or {}
    return {"links": [{"n": i + 1, "name": l.get("name") or "", "place": l.get("place") or "",
                       "window": line_window(g, i), "manual": str(i + 1) in links}
                      for i, l in enumerate(g.get("lines", []))],
            "windows": public_windows()}


def set_link(g, body):
    need_creator(g, body)
    try:
        n = int(body.get("n"))
    except (TypeError, ValueError):
        raise Fail(400, "Falta el número de verso.")
    if not 1 <= n <= len(g.get("lines", [])):
        raise Fail(400, "Ese verso no existe.")
    wid = str(body.get("window") or "")
    links = dict(g.get("links") or {})
    if wid == "auto":
        links.pop(str(n), None)
    elif wid == "" or wid in WINDOWS:
        if wid:  # a photo goes with one verse only
            links = {k: v for k, v in links.items() if v != wid}
        links[str(n)] = wid
    else:
        raise Fail(400, "Esa foto ya no existe.")
    g["links"] = links
    STORE.save(g)
    return link_info(g, body)


ACTIONS = {"take": take_turn, "lines": add_line, "release": release, "reveal": reveal, "delete": delete_game,
           "turns": set_turns, "mine": my_lines,
           "links": link_info, "link": set_link}

# ---------------------------------------------------------------- rate limit

HITS = {}


def too_many(ip):
    t = now()
    recent = [x for x in HITS.get(ip, []) if t - x < 60]
    recent.append(t)
    HITS[ip] = recent[-200:]
    return len(recent) > 300


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "Cadaver/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("QUIET") or "/healthz" in (self.path or ""):
            return
        super().log_message(fmt, *args)

    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() or self.client_address[0]

    def send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/games":
            with LOCK:
                return self.send_json(200, public_list())
        if path == "/api/events":
            return self.events()
        if path == "/healthz":
            return self.send_json(200, {"ok": True})
        if path == "/api/export.txt":
            return self.export()
        m = re.match(r"^/api/windows/([a-z0-9]{6,32})\.jpg$", path)
        if m:
            return self.window_image(m.group(1))
        return self.static(path)

    def window_image(self, wid):
        with LOCK:
            data = IMAGES.get(wid)
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        self.send_response(200 if self.path.split("?", 1)[0] in ("/", "/healthz") else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def export(self):
        from urllib.parse import parse_qs, urlparse
        only = parse_qs(urlparse(self.path).query).get("id", [""])[0]
        with LOCK:
            done = [g for g in GAMES.values() if g["status"] == "done" and (not only or g["id"] == only)]
            done.sort(key=lambda g: g["createdAt"])
            parts = []
            for g in done:
                when = time.strftime("%d/%m/%Y", time.localtime(g["createdAt"]))
                lines = g.get("lines", [])
                block = [g["title"].upper(), f"{when} · {len(lines)} versos", ""]
                for l in lines:
                    sign = ", ".join(x for x in (l.get("name"), l.get("place")) if x)
                    block.append(l["text"] + (f"    — {sign}" if sign else ""))
                parts.append("\n".join(block))
        header = ("CARTOGRAFÍA SENSIBLE · Taller de Integración Final\n"
                  "Ciclo de Complementación Curricular en Gestión Cultural\n"
                  "Escuela de Artes, Medios e Innovación · UNLaM\n"
                  "Cadáver exquisito escrito entre toda la comisión\n\n\n")
        text = header + ("\n\n\n".join(parts) if parts else "Todavía no hay trayectorias reveladas.") + "\n"
        data = text.encode("utf-8")
        name = f"cadaver-exquisito-{only}.txt" if only else "cadaver-exquisito-poemas.txt"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (PUBLIC / name).resolve()
        if PUBLIC not in target.parents or not target.is_file():
            self.send_error(404)
            return
        types = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", types.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def events(self):
        q = queue.Queue(maxsize=20)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        with LOCK:
            first = json.dumps(public_list(), ensure_ascii=False)
        with LISTENERS_LOCK:
            LISTENERS.add(q)
        try:
            self.wfile.write(f"data: {first}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=20)
                    self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with LISTENERS_LOCK:
                LISTENERS.discard(q)
            self.close_connection = True

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if too_many(self.client_ip()):
            return self.send_json(429, {"error": "Demasiadas acciones seguidas. Esperá un minuto."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            limit = 1_200_000 if path == "/api/windows" else 10_000
            if length > limit:
                raise Fail(413, "Mensaje demasiado largo.")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise Fail(400, "Petición no válida.")
            with LOCK:
                if path == "/api/games":
                    result = create_game(body)
                elif path == "/api/windows":
                    result = save_window(body)
                elif path == "/api/windows/delete":
                    result = delete_window(body)
                elif path == "/api/windows/me":
                    result = my_window(body)
                else:
                    m = re.match(r"^/api/games/([a-z0-9]+)/([a-z]+)$", path)
                    if not m or m.group(2) not in ACTIONS or not ID_RE.match(m.group(1)):
                        raise Fail(404, "Ruta desconocida.")
                    result = ACTIONS[m.group(2)](need_game(m.group(1)), body)
            if path != "/api/windows/me" and not path.endswith(("/mine", "/links")):
                broadcast()
            self.send_json(200, {"ok": True, **result})
        except Fail as f:
            self.send_json(f.status, {"error": f.message})
        except (ValueError, TypeError):
            self.send_json(400, {"error": "Petición no válida."})
        except Exception as e:  # noqa: BLE001
            self.log_error("error: %r", e)
            self.send_json(500, {"error": "Algo falló en el servidor. Intentalo de nuevo."})


def expire_loop():
    """Tell open pages when a forgotten turn frees up."""
    while True:
        time.sleep(15)
        changed = False
        with LOCK:
            for g in GAMES.values():
                if g.get("writingBy") and g.get("writingUntil", 0) <= now() and not g.get("_expiredSent"):
                    g["_expiredSent"] = True
                    changed = True
                elif writing(g):
                    g.pop("_expiredSent", None)
        if changed:
            broadcast()


if __name__ == "__main__":
    threading.Thread(target=expire_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    mode = "PostgreSQL" if DATABASE_URL else f"archivo {DATA_FILE.name}"
    print(f"Cadáver Exquisito en http://localhost:{PORT}  (guardando en {mode})", flush=True)
    srv.serve_forever()
