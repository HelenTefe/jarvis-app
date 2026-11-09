#!/usr/bin/env python3
"""Jarvis-App – Helens eigene Oberfläche für Hermes und Lulu.

Läuft als Benutzer `jarvis` auf 127.0.0.1:9130 und wird nur über Tailscale Serve
(https://<server>.ts.net:8443) erreichbar gemacht. Nur Python-Standardbibliothek.

Schlüssel kommen aus ~/.hermes/.env (API_SERVER_KEY, GITHUB_TOKEN, LULU_SUPABASE_URL,
LULU_SUPABASE_KEY) – sie verlassen den Server nie und werden nie an den Browser geschickt.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HIER = Path(__file__).resolve().parent
WEB = HIER / "web"
HERMES_ENV = Path(os.environ.get("HERMES_ENV_DATEI", Path.home() / ".hermes" / ".env"))
HERMES_API = os.environ.get("HERMES_API", "http://127.0.0.1:8642")
PORT = int(os.environ.get("JARVIS_PORT", "9130"))
GESPRAECH = "jarvis-app"

sys.path.insert(0, str(HIER))
import lulu  # noqa: E402  (eigene Kopie des Lulu-Skripts)

TYPEN = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".json": "application/json",
         ".webmanifest": "application/manifest+json", ".png": "image/png", ".svg": "image/svg+xml"}

_env_sperre = threading.Lock()


def env_laden():
    """Liest ~/.hermes/.env bei jeder Anfrage neu (neue Schlüssel gelten sofort)."""
    werte = {}
    try:
        for zeile in HERMES_ENV.read_text().splitlines():
            zeile = zeile.strip()
            if not zeile or zeile.startswith("#") or "=" not in zeile:
                continue
            k, v = zeile.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            werte[k] = v
    except OSError:
        pass
    with _env_sperre:
        for k in ("GITHUB_TOKEN", "LULU_SUPABASE_URL", "LULU_SUPABASE_KEY", "LULU_REPO"):
            if werte.get(k):
                os.environ[k] = werte[k]
            elif k in os.environ and k != "LULU_REPO":
                del os.environ[k]
    return werte


def lulu_bereit():
    return all(os.environ.get(k) for k in ("LULU_SUPABASE_URL", "LULU_SUPABASE_KEY"))


# ---------- Hermes ----------

def hermes_fragen(text, env):
    schluessel = env.get("API_SERVER_KEY", "")
    if not schluessel:
        raise lulu.Fehler("Die Hermes-Schnittstelle ist noch nicht eingeschaltet.")
    anweisung = ("Du bist Jarvis, Helens persönlicher Assistent für ihre Affiliate-Webseiten. "
                 "Antworte auf Deutsch, kurz, freundlich und einfach – die Antwort wird oft vorgelesen. "
                 "Für Lulu-Aufgaben nutze den Skill 'lulu'.")
    daten = {"model": "hermes-agent", "input": text, "instructions": anweisung,
             "conversation": GESPRAECH, "store": True}
    req = urllib.request.Request(
        HERMES_API + "/v1/responses", data=json.dumps(daten).encode(), method="POST",
        headers={"Authorization": "Bearer " + schluessel, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as a:
            antwort = json.loads(a.read().decode())
    except urllib.error.HTTPError as e:
        raise lulu.Fehler(f"Hermes meldet Fehler {e.code}.") from None
    except urllib.error.URLError:
        raise lulu.Fehler("Hermes ist gerade nicht erreichbar.") from None
    teile, werkzeuge = [], []
    for item in antwort.get("output", []):
        if item.get("type") == "message" and item.get("phase") != "commentary":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    teile.append(c.get("text", ""))
        elif item.get("type") == "function_call":
            werkzeuge.append(item.get("name"))
    text = "\n\n".join(t for t in teile if t).strip() or "(keine Antwort)"
    return {"antwort": text, "status": antwort.get("status"), "werkzeuge": werkzeuge}


def hermes_online(env):
    try:
        with urllib.request.urlopen(HERMES_API + "/health", timeout=3) as a:
            return a.status == 200 and bool(env.get("API_SERVER_KEY"))
    except Exception:
        return False


# ---------- Zahlen ----------

def zahlen():
    seit = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    kz = lulu._sb("GET", f"kennzahlen_taeglich?select=besucher&datum=gte.{seit}&limit=5000") or []
    ein = lulu._sb("GET", f"einnahmen_taeglich?select=provision_eur&datum=gte.{seit}&limit=5000") or []
    monat = datetime.now(timezone.utc).strftime("%Y-%m-01")
    kosten = lulu._sb("GET", f"kosten_pro_monat?select=kosten_usd,laeufe&monat=eq.{monat}") or []
    return {
        "besucher_30t": sum(int(z.get("besucher") or 0) for z in kz),
        "provision_30t": round(sum(float(z.get("provision_eur") or 0) for z in ein), 2),
        "ki_kosten_monat": float(kosten[0]["kosten_usd"] or 0) if kosten else 0.0,
        "laeufe_monat": int(kosten[0]["laeufe"] or 0) if kosten else 0,
        "budget_usd": 50,
    }


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "Jarvis/1.0"

    def log_message(self, fmt, *args):  # knappe Logs, keine Inhalte
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # Zugriff nur über Tailscale Serve (setzt diesen Kopf für Geräte im eigenen Netz).
    def _erlaubt(self):
        nutzer = self.headers.get("Tailscale-User-Login", "")
        erlaubt = os.environ.get("JARVIS_NUTZER", "").strip().lower()
        if not nutzer:
            return False
        return not erlaubt or nutzer.lower() == erlaubt

    def _json(self, code, daten):
        roh = json.dumps(daten, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(roh)))
        self.end_headers()
        self.wfile.write(roh)

    def _sicherheitskoepfe(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def end_headers(self):
        self._sicherheitskoepfe()
        super().end_headers()

    def _datei(self, pfad):
        if pfad in ("", "/"):
            pfad = "/index.html"
        ziel = (WEB / pfad.lstrip("/")).resolve()
        if WEB not in ziel.parents or not ziel.is_file():
            self.send_error(404)
            return
        roh = ziel.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPEN.get(ziel.suffix, "application/octet-stream"))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(roh)))
        self.end_headers()
        self.wfile.write(roh)

    def do_GET(self):
        if not self._erlaubt():
            self.send_error(403, "Nur über Tailscale")
            return
        pfad = self.path.split("?", 1)[0]
        if not pfad.startswith("/api/"):
            self._datei(pfad)
            return
        env = env_laden()
        try:
            if pfad == "/api/status":
                self._json(200, {"hermes": hermes_online(env), "lulu": lulu_bereit(),
                                 "github": bool(os.environ.get("GITHUB_TOKEN")),
                                 "nutzer": self.headers.get("Tailscale-User-Name", "")})
            elif pfad == "/api/beitraege":
                self._json(200, lulu.beitraege([]) if lulu_bereit() else [])
            elif pfad == "/api/zahlen":
                self._json(200, zahlen() if lulu_bereit() else {})
            elif pfad == "/api/laeufe":
                self._json(200, lulu.laeufe([]))
            else:
                self._json(404, {"fehler": "Unbekannt"})
        except lulu.Fehler as e:
            self._json(502, {"fehler": str(e)})

    def do_POST(self):
        if not self._erlaubt():
            self.send_error(403, "Nur über Tailscale")
            return
        # Schutz gegen fremde Seiten: eigener Kopf + gleiche Herkunft.
        herkunft = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        if self.headers.get("X-Jarvis") != "1" or (herkunft and herkunft.split("//", 1)[-1] != host):
            self._json(403, {"fehler": "Abgelehnt"})
            return
        laenge = min(int(self.headers.get("Content-Length") or 0), 20000)
        try:
            daten = json.loads(self.rfile.read(laenge) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"fehler": "Ungültige Anfrage"})
            return
        env = env_laden()
        pfad = self.path.split("?", 1)[0]
        try:
            if pfad == "/api/chat":
                text = str(daten.get("text", "")).strip()[:4000]
                if not text:
                    self._json(400, {"fehler": "Leere Nachricht"})
                    return
                self._json(200, hermes_fragen(text, env))
                return
            m = re.fullmatch(r"/api/beitraege/(\d+)/(freigeben|ablehnen)", pfad)
            if m:
                aktion = lulu.freigeben if m.group(2) == "freigeben" else lulu.ablehnen
                self._json(200, aktion([m.group(1)]))
                return
            self._json(404, {"fehler": "Unbekannt"})
        except lulu.Fehler as e:
            self._json(502, {"fehler": str(e)})


def main():
    env_laden()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Jarvis-App läuft auf http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
