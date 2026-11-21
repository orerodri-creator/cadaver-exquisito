"""Cadáver Exquisito: servidor del juego.

Sin dependencias para jugar en local (guarda las partidas en data.json).
Si existe la variable DATABASE_URL, guarda en PostgreSQL (necesita psycopg).

    python server.py            -> http://localhost:8000
"""

import json
import os
import queue
import re
import secrets
import threading
import time
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


LOCK = threading.RLock()
STORE = PgStore(DATABASE_URL) if DATABASE_URL else FileStore(DATA_FILE)
GAMES = STORE.load()

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
        base["lines"] = [{"text": l["text"], "name": l.get("name") or "", "place": l.get("place") or ""} for l in lines]
    else:
        last = lines[-1]["text"] if lines else ""
        base["peekWords"] = " ".join(last.split()[-g["peek"]:]) if last else ""
        base["writing"] = writing(g)
        base["writingName"] = g.get("writingName") or "" if writing(g) else ""
        base["writingUntil"] = g.get("writingUntil", 0) if writing(g) else 0
    return base


def public_list():
    games = sorted(GAMES.values(), key=lambda g: g["createdAt"], reverse=True)
    return {"games": [public_game(g) for g in games], "serverTime": now()}


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


ACTIONS = {"take": take_turn, "lines": add_line, "release": release, "reveal": reveal, "delete": delete_game,
           "turns": set_turns}

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
        return self.static(path)

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
            if length > 10_000:
                raise Fail(413, "Mensaje demasiado largo.")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise Fail(400, "Petición no válida.")
            with LOCK:
                if path == "/api/games":
                    result = create_game(body)
                else:
                    m = re.match(r"^/api/games/([a-z0-9]+)/([a-z]+)$", path)
                    if not m or m.group(2) not in ACTIONS or not ID_RE.match(m.group(1)):
                        raise Fail(404, "Ruta desconocida.")
                    result = ACTIONS[m.group(2)](need_game(m.group(1)), body)
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
