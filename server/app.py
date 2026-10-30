"""Small API for the pair-labeling site. Name-only login; every answer is saved to SQLite right away.
Standard library only. Reads DATA_DIR/pairs.json, writes DATA_DIR/labels.db."""
import csv
import io
import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DATA = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
PORT = int(os.environ.get("PORT", "8096"))
CODE = os.environ.get("SURVEY_CODE", "")  # goes in the doctors' link as ?k=...; empty = no check
ADMIN = os.environ.get("ADMIN_CODE", "")  # needed for /export.csv and /progress
ORIGINS = set(os.environ.get("ALLOW_ORIGINS", "https://sh-islam.github.io").split(","))

PAIRS = json.load(open(os.path.join(DATA, "pairs.json"), encoding="utf-8"))["pairs"]
DB = os.path.join(DATA, "labels.db")
FIELDS = ["name", "pair_id", "q1", "q2", "q3", "unsure", "comment", "ms", "first_at", "updated_at"]


def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


with db() as _con:
    _con.execute("PRAGMA journal_mode=WAL")
    _con.execute("""CREATE TABLE IF NOT EXISTS labels (name TEXT, pair_id TEXT, q1 INT, q2 INT, q3 INT,
        unsure INT, comment TEXT, ms INT, first_at TEXT, updated_at TEXT, PRIMARY KEY (name, pair_id))""")
    _con.execute("CREATE TABLE IF NOT EXISTS log (at TEXT, name TEXT, pair_id TEXT, body TEXT)")


def assigned(name):
    # optional DATA_DIR/assign.json {"name": ["p001", ...]}; anyone not listed gets every pair
    path = os.path.join(DATA, "assign.json")
    ids = json.load(open(path, encoding="utf-8")).get(name) if os.path.exists(path) else None
    return [p for p in PAIRS if p["id"] in set(ids)] if ids else PAIRS


def clean_name(s):
    return " ".join(str(s or "").lower().split())[:40]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Handler(BaseHTTPRequestHandler):
    def cors(self):
        origin = self.headers.get("Origin")
        if origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/health":
            return self.reply(200, {"ok": True, "pairs": len(PAIRS)})

        if url.path == "/session":
            if CODE and q.get("k") != CODE:
                return self.reply(403, {"error": "bad link"})
            name = clean_name(q.get("name"))
            if not name:
                return self.reply(400, {"error": "name required"})
            with db() as con:
                rows = con.execute("SELECT * FROM labels WHERE name=?", (name,)).fetchall()
            labels = {r["pair_id"]: {k: r[k] for k in ("q1", "q2", "q3", "unsure", "comment")} for r in rows}
            return self.reply(200, {"name": name, "pairs": assigned(name), "labels": labels})

        if url.path in ("/export.csv", "/progress"):
            if not ADMIN or q.get("k") != ADMIN:
                return self.reply(403, {"error": "admin only"})
            with db() as con:
                rows = con.execute("SELECT * FROM labels ORDER BY name, pair_id").fetchall()
            if url.path == "/progress":
                done = {}
                for r in rows:
                    done[r["name"]] = done.get(r["name"], 0) + 1
                return self.reply(200, {n: f"{c}/{len(assigned(n))}" for n, c in done.items()})
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(FIELDS)
            w.writerows([[r[f] for f in FIELDS] for r in rows])
            return self.reply(200, out.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8")

        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/label":
            return self.reply(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 20000:
                raise ValueError("too big")
            b = json.loads(self.rfile.read(n))
        except (ValueError, json.JSONDecodeError):
            return self.reply(400, {"error": "bad body"})
        if CODE and b.get("k") != CODE:
            return self.reply(403, {"error": "bad link"})

        name, pid = clean_name(b.get("name")), b.get("pair_id")
        qs = [b.get(f"q{i}") for i in (1, 2, 3)]
        if not name or pid not in {p["id"] for p in assigned(name)} or any(v not in (1, 2, 3) for v in qs):
            return self.reply(400, {"error": "bad label"})
        unsure = 1 if b.get("unsure") else 0
        comment = str(b.get("comment") or "")[:2000]
        ms = max(0, min(int(b.get("ms") or 0), 3600_000))
        t = now()
        with db() as con:
            # revisits overwrite the answers but add to the time spent
            con.execute("""INSERT INTO labels VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(name, pair_id) DO UPDATE SET q1=excluded.q1, q2=excluded.q2, q3=excluded.q3,
                unsure=excluded.unsure, comment=excluded.comment, ms=labels.ms+excluded.ms,
                updated_at=excluded.updated_at""", (name, pid, *qs, unsure, comment, ms, t, t))
            con.execute("INSERT INTO log VALUES (?,?,?,?)", (t, name, pid, json.dumps(
                {"q": qs, "unsure": unsure, "comment": comment, "ms": ms})))
            done = con.execute("SELECT COUNT(*) FROM labels WHERE name=?", (name,)).fetchone()[0]
        self.reply(200, {"ok": True, "done": done})

    def log_request(self, code="-", size="-"):
        # path only: the query holds names and the link code
        print(f"{now()} {self.command} {urlparse(self.path).path} {code}", flush=True)


if __name__ == "__main__":
    print(f"{len(PAIRS)} pairs, port {PORT}, code check {'on' if CODE else 'off'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
