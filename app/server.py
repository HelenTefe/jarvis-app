#!/usr/bin/env python3
"""Jarvis-App – Helens eigene Oberfläche für Hermes und Lulu.

Läuft als Benutzer `jarvis` auf 127.0.0.1:9130 und wird nur über Tailscale Serve
(https://<server>.ts.net:8443) erreichbar gemacht. Nur Python-Standardbibliothek.

Schlüssel kommen aus ~/.hermes/.env (API_SERVER_KEY, LULU_GITHUB_TOKEN, LULU_SUPABASE_URL,
LULU_SUPABASE_KEY) – sie verlassen den Server nie und werden nie an den Browser geschickt.
"""
import json
import os
import re
import secrets
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
UPDATE_REPO = "HelenTefe/jarvis-app"
GESPRAECH_DATEI = HIER / ".gespraech"


def gespraech():
    try:
        return GESPRAECH_DATEI.read_text().strip() or "jarvis-app"
    except OSError:
        return "jarvis-app"


def neues_gespraech():
    name = "jarvis-app-" + time.strftime("%Y%m%d-%H%M%S")
    GESPRAECH_DATEI.write_text(name)
    return name

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
        for k in ("LULU_GITHUB_TOKEN", "LULU_SUPABASE_URL", "LULU_SUPABASE_KEY", "LULU_REPO"):
            if werte.get(k):
                os.environ[k] = werte[k]
            elif k in os.environ and k != "LULU_REPO":
                del os.environ[k]
    return werte


def alten_github_namen_umziehen():
    """Frühere Versionen speicherten GITHUB_TOKEN – den reicht Hermes aber nie an Skills weiter.
    Einmalig in LULU_GITHUB_TOKEN umbenennen und Hermes neu starten."""
    try:
        zeilen = HERMES_ENV.read_text().splitlines()
    except OSError:
        return
    alt = [z for z in zeilen if re.match(r"\s*(export\s+)?GITHUB_TOKEN\s*=", z)]
    if not alt:
        return
    neu_da = any(re.match(r"\s*(export\s+)?LULU_GITHUB_TOKEN\s*=", z) for z in zeilen)
    rest = [z for z in zeilen if z not in alt]
    if not neu_da:
        wert = alt[-1].split("=", 1)[1].strip()
        rest.append("LULU_GITHUB_TOKEN=" + wert)
    with _env_sperre:
        HERMES_ENV.write_text("\n".join(rest) + "\n")
        os.chmod(HERMES_ENV, 0o600)
    print("GitHub-Schlüssel auf LULU_GITHUB_TOKEN umgestellt.", flush=True)
    try:
        import subprocess
        subprocess.Popen(["systemctl", "--user", "restart", "hermes-gateway.service"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def lulu_bereit():
    return all(os.environ.get(k) for k in ("LULU_SUPABASE_URL", "LULU_SUPABASE_KEY"))


# ---------- Schlüssel eintragen (nur diese drei, Werte werden nie zurückgegeben) ----------

SCHLUESSEL_REGELN = {
    "LULU_GITHUB_TOKEN": re.compile(r"(github_pat_|ghp_)[A-Za-z0-9_]{20,255}"),
    "LULU_SUPABASE_URL": re.compile(r"https://[a-z0-9]{10,40}\.supabase\.co/?"),
    "LULU_SUPABASE_KEY": re.compile(r"[A-Za-z0-9_\-\.]{30,1200}"),
    "GROQ_API_KEY": re.compile(r"gsk_[A-Za-z0-9]{20,200}"),
}


def schluessel_speichern(name, wert):
    regel = SCHLUESSEL_REGELN.get(name)
    if not regel:
        raise lulu.Fehler("Diesen Schlüssel kann die App nicht setzen.")
    wert = (wert or "").strip()
    if not regel.fullmatch(wert):
        hilfe = {"LULU_GITHUB_TOKEN": "Ein GitHub-Token beginnt mit „github_pat_“.",
                 "LULU_SUPABASE_URL": "Die Adresse sieht so aus: https://….supabase.co",
                 "LULU_SUPABASE_KEY": "Bitte den ganzen Schlüssel ohne Leerzeichen einfügen.",
                 "GROQ_API_KEY": "Ein Groq-Schlüssel beginnt mit „gsk_“."}[name]
        raise lulu.Fehler("Das sieht nicht richtig aus. " + hilfe)
    with _env_sperre:
        zeilen = HERMES_ENV.read_text().splitlines() if HERMES_ENV.exists() else []
        zeilen = [z for z in zeilen if not re.match(r"\s*(export\s+)?" + name + r"\s*=", z)]
        zeilen.append(f"{name}={wert}")
        HERMES_ENV.write_text("\n".join(zeilen) + "\n")
        os.chmod(HERMES_ENV, 0o600)
    # Hermes neu starten, damit er den Schlüssel kennt (im Hintergrund, blockiert nicht).
    if name == "GROQ_API_KEY":
        return {"gespeichert": name}  # nur die App braucht ihn, sie liest ihn sofort
    try:
        import subprocess
        subprocess.Popen(["systemctl", "--user", "restart", "hermes-gateway.service"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    return {"gespeichert": name}


# ---------- Hermes ----------

ANWEISUNG = ("Du bist Jarvis, Helens persönlicher Sprach-Assistent für ihre Affiliate-Webseiten. "
             "Deine Antworten werden laut vorgelesen: antworte auf Deutsch, in ein bis drei kurzen, "
             "natürlichen Sätzen, ohne Listen, Tabellen, Links oder Markdown – außer Helen bittet ausdrücklich "
             "um Details. Für Lulu-Aufgaben nutze den Skill 'lulu'.")
# Hermes-Stufen (über das ChatGPT-Abo). Die schnellen Abo-Modelle (Spark, Mini, Luna) hat Helens
# Abo abgelehnt bzw. waren langsamer – deshalb: Standard-Modell mit kurzer oder langer Denkzeit.
TEMPO = {
    "gruendlich": {"model": None, "provider": None, "denken": "medium"},
    "_standard": {"model": None, "provider": None, "denken": "low"},
}


# ---------- Schneller Direktweg (Groq) – nach dem Vorbild von OpenJarvis' "simple chat" ----------

GROQ_API = os.environ.get("GROQ_API_BASE", "https://api.groq.com/openai/v1")
GROQ_MODELLE = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile"]
DIREKT_ANWEISUNG = (
    "Du bist Jarvis, Helens persönlicher Sprach-Assistent. Helen betreibt Affiliate-Webseiten; ihr "
    "Agenten-System heißt Lulu. Deine Antworten werden laut vorgelesen: antworte auf Deutsch, in ein bis "
    "drei kurzen, natürlichen Sätzen, ohne Listen, Links oder Markdown.\n"
    "Du selbst kannst nur reden, erklären, Ideen geben und Texte formulieren. Sobald Helen möchte, dass "
    "etwas GETAN oder NACHGESCHAUT wird – Lulu-Aufträge, Beiträge/Posts, Freigaben, Berichte, Themen, "
    "Webseiten prüfen, Kosten, Läufe, Aufgaben, Termine, Mails, Erinnerungen, Dateien oder aktuelle "
    "Informationen aus dem Internet – rufe sofort das Werkzeug 'an_agent_weitergeben' auf und erfinde "
    "nichts. Sag dabei höchstens einen ganz kurzen Satz wie 'Mache ich.'")
WEITERGEBEN = {
    "type": "function",
    "function": {
        "name": "an_agent_weitergeben",
        "description": "Gibt eine Aufgabe an Helens Agenten (Hermes und Lulu) weiter, die Dinge erledigen "
                       "und nachschauen können.",
        "parameters": {"type": "object", "properties": {
            "auftrag": {"type": "string", "description": "Was genau erledigt werden soll, in Helens Worten."}},
            "required": ["auftrag"]},
    },
}
_verlauf = []  # kurzer Gesprächsverlauf für den Direktweg (nur im Speicher)
_verlauf_sperre = threading.Lock()


def _verlauf_merken(rolle, text):
    with _verlauf_sperre:
        _verlauf.append({"role": rolle, "content": text[:2000]})
        del _verlauf[:-16]


AUDIO_ENDUNG = {"audio/mp4": "m4a", "audio/aac": "m4a", "audio/webm": "webm", "audio/ogg": "ogg",
                "audio/wav": "wav", "audio/mpeg": "mp3"}


def hoeren(roh, typ, env):
    """Wandelt eine Sprachaufnahme mit Groq Whisper in Text um."""
    schluessel = env.get("GROQ_API_KEY", "")
    if not schluessel:
        raise lulu.Fehler("Kein Groq-Schlüssel")
    endung = AUDIO_ENDUNG.get(typ.split(";")[0].strip().lower(), "m4a")
    grenze = "jarvis" + secrets.token_hex(12)
    teile = []
    for name, wert in (("model", "whisper-large-v3-turbo"), ("language", "de"),
                       ("response_format", "json"), ("temperature", "0"),
                       ("prompt", "Jarvis, Lulu, Instagram, Facebook, TikTok, Beitrag, freigeben.")):
        teile.append(f"--{grenze}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{wert}\r\n".encode())
    teile.append(f"--{grenze}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"sprache.{endung}\"\r\n"
                 f"Content-Type: {typ.split(';')[0]}\r\n\r\n".encode() + roh + b"\r\n")
    teile.append(f"--{grenze}--\r\n".encode())
    req = urllib.request.Request(
        GROQ_API + "/audio/transcriptions", data=b"".join(teile), method="POST",
        headers={"Authorization": "Bearer " + schluessel, "User-Agent": "jarvis-app",
                 "Content-Type": "multipart/form-data; boundary=" + grenze})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as a:
            text = str(json.loads(a.read()).get("text", "")).strip()
    except urllib.error.HTTPError as e:
        raise lulu.Fehler(f"Groq-Spracherkennung: HTTP {e.code} {e.read()[:200]!r}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise lulu.Fehler(f"Groq-Spracherkennung nicht erreichbar ({type(e).__name__})") from None
    sys.stderr.write(f"Hören: {len(roh)//1024} KB in {time.time() - start:.1f}s erkannt\n")
    # Whisper erfindet bei Stille gern Floskeln – die verwerfen.
    if re.fullmatch(r"(?i)\W*(untertitel.*|vielen dank\.?|danke\.?|tschüss\.?|amara\.org.*)?\W*", text):
        return ""
    return text


def direkt_strom(text, env, senden):
    """Antwortet über Groq. Gibt den Auftrag zurück, falls an Hermes weitergegeben werden soll."""
    schluessel = env.get("GROQ_API_KEY", "")
    with _verlauf_sperre:
        nachrichten = [{"role": "system", "content": DIREKT_ANWEISUNG}] + list(_verlauf) + \
                      [{"role": "user", "content": text}]
    letzter_fehler = None
    for modell in GROQ_MODELLE:
        daten = {"model": modell, "messages": nachrichten, "stream": True, "tools": [WEITERGEBEN],
                 "tool_choice": "auto", "temperature": 0.6, "max_completion_tokens": 600}
        if "gpt-oss" in modell:
            daten["reasoning_effort"] = "low"
        req = urllib.request.Request(
            GROQ_API + "/chat/completions", data=json.dumps(daten).encode(), method="POST",
            headers={"Authorization": "Bearer " + schluessel, "Content-Type": "application/json",
                     "User-Agent": "jarvis-app"})
        antwort, aufruf, start = "", "", time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as a:
                for roh in a:
                    zeile = roh.decode("utf-8", "replace").strip()
                    if not zeile.startswith("data:") or zeile[5:].strip() == "[DONE]":
                        continue
                    try:
                        delta = json.loads(zeile[5:])["choices"][0].get("delta") or {}
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if delta.get("content"):
                        if not antwort:
                            sys.stderr.write(f"Direkt ({modell}): erstes Wort nach {time.time() - start:.1f}s\n")
                        antwort += delta["content"]
                        senden({"t": "text", "d": delta["content"]})
                    for tc in delta.get("tool_calls") or []:
                        aufruf += ((tc.get("function") or {}).get("arguments") or "")
                        aufruf = aufruf or " "
        except urllib.error.HTTPError as e:
            letzter_fehler = f"Groq meldet Fehler {e.code}"
            if antwort:
                break
            continue  # nächstes Modell probieren
        except urllib.error.URLError:
            letzter_fehler = "Groq ist nicht erreichbar"
            break
        _verlauf_merken("user", text)
        if aufruf:
            try:
                auftrag = (json.loads(aufruf.strip() or "{}").get("auftrag") or "").strip()
            except json.JSONDecodeError:
                auftrag = ""
            sys.stderr.write(f"Direkt: weiter an Hermes nach {time.time() - start:.1f}s\n")
            return auftrag or text
        _verlauf_merken("assistant", antwort)
        return None
    raise lulu.Fehler(letzter_fehler or "Groq hat nicht geantwortet")


def _hermes_anfrage(text, env, stream, tempo="_standard"):
    schluessel = env.get("API_SERVER_KEY", "")
    if not schluessel:
        raise lulu.Fehler("Die Hermes-Schnittstelle ist noch nicht eingeschaltet.")
    stufe = TEMPO.get(tempo, TEMPO["_standard"])
    daten = {"model": "hermes-agent", "input": text, "instructions": ANWEISUNG,
             "conversation": gespraech(), "store": True, "stream": stream,
             "model_options": {"reasoning_effort": stufe["denken"]}}
    if stufe["model"]:
        daten["model"] = stufe["model"]
        daten["provider"] = stufe["provider"]
    return urllib.request.Request(
        HERMES_API + "/v1/responses", data=json.dumps(daten).encode(), method="POST",
        headers={"Authorization": "Bearer " + schluessel, "Content-Type": "application/json",
                 "Accept": "text/event-stream" if stream else "application/json"})


def hermes_strom(text, env, senden, tempo="_standard"):
    """Leitet Hermes' Antwort Stück für Stück weiter: senden({"t": "text"|"werkzeug"|"ende", ...})."""
    try:
        return _hermes_strom(text, env, senden, tempo)
    except _ModellFehlt:
        # Schnelles Modell im Abo nicht verfügbar → mit dem Standard-Modell weiter.
        senden({"t": "hinweis", "d": "Schnelles Modell nicht verfügbar – nutze Standard."})
        return _hermes_strom(text, env, senden, "_standard")


class _ModellFehlt(Exception):
    pass


FEHLERTEXT = re.compile(r"rejected the request|Provider said: HTTP [45]\d\d|Pick another model", re.I)


def _hermes_strom(text, env, senden, tempo):
    req = _hermes_anfrage(text, env, True, tempo)
    nebenbei = set()  # Zwischenkommentare (phase=commentary) nicht vorlesen
    status = None
    erstes = time.time()
    puffer = ""  # die ersten Zeichen halten, um Fehlermeldungen zu erkennen
    try:
        with urllib.request.urlopen(req, timeout=600) as a:
            for roh in a:
                zeile = roh.decode("utf-8", "replace").strip()
                if not zeile.startswith("data:"):
                    continue
                inhalt = zeile[5:].strip()
                if not inhalt or inhalt == "[DONE]":
                    continue
                try:
                    ev = json.loads(inhalt)
                except json.JSONDecodeError:
                    continue
                typ = ev.get("type", "")
                if typ == "response.output_item.added":
                    item = ev.get("item") or {}
                    if item.get("phase") == "commentary":
                        nebenbei.add(item.get("id"))
                    elif item.get("type") == "function_call":
                        senden({"t": "werkzeug", "n": item.get("name")})
                elif typ == "response.output_text.delta" and ev.get("item_id") not in nebenbei:
                    if ev.get("delta"):
                        if erstes:
                            sys.stderr.write(f"Tempo {tempo}: erstes Wort nach {time.time() - erstes:.1f}s\n")
                            erstes = None
                        if puffer is not None:
                            puffer += ev["delta"]
                            if len(puffer) < 90:
                                continue
                            if FEHLERTEXT.search(puffer):
                                if TEMPO[tempo]["model"]:
                                    raise _ModellFehlt()
                                raise lulu.Fehler("ChatGPT hat die Anfrage abgelehnt. Bitte gleich nochmal versuchen.")
                            senden({"t": "text", "d": puffer})
                            puffer = None
                            continue
                        senden({"t": "text", "d": ev["delta"]})
                elif typ in ("response.completed", "response.failed", "response.cancelled", "response.incomplete"):
                    status = (ev.get("response") or {}).get("status") or typ.rsplit(".", 1)[-1]
                    fehlertext = json.dumps((ev.get("response") or {}).get("error") or "")
                    if status == "failed" and TEMPO[tempo]["model"] and re.search(r"model|not supported|unknown", fehlertext, re.I):
                        raise _ModellFehlt()
    except urllib.error.HTTPError as e:
        if e.code in (400, 404) and TEMPO.get(tempo, {}).get("model"):
            raise _ModellFehlt() from None
        raise lulu.Fehler(f"Hermes meldet Fehler {e.code}.") from None
    except urllib.error.URLError:
        raise lulu.Fehler("Hermes ist gerade nicht erreichbar.") from None
    if puffer:
        if FEHLERTEXT.search(puffer):
            if TEMPO[tempo]["model"]:
                raise _ModellFehlt()
            raise lulu.Fehler("ChatGPT hat die Anfrage abgelehnt. Bitte gleich nochmal versuchen.")
        senden({"t": "text", "d": puffer})
    senden({"t": "ende", "status": status})


def hermes_fragen(text, env):
    req = _hermes_anfrage(text, env, False)
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


# ---------- Stimme (Microsoft Edge Neural, kostenlos) ----------

STIMME = os.environ.get("JARVIS_STIMME", "de-DE-ConradNeural")


def sprechtext(text):
    t = re.sub(r"https?://\S+", "Link", text)
    t = re.sub(r"[*_`#>|]", "", t)
    t = re.sub(r"^\s*[-•]\s*", "", t, flags=re.M)
    return re.sub(r"\s+", " ", t).strip()[:1500]


def sprechen(text):
    try:
        import asyncio
        import edge_tts
    except ImportError:
        raise lulu.Fehler("Sprachausgabe ist nicht installiert.") from None

    async def erzeugen():
        mp3 = bytearray()
        async for teil in edge_tts.Communicate(text, STIMME, rate="+4%").stream():
            if teil.get("type") == "audio":
                mp3 += teil["data"]
        return bytes(mp3)

    try:
        daten = asyncio.run(erzeugen())
    except Exception as e:  # Netzwerk/Dienst
        raise lulu.Fehler(f"Stimme gerade nicht verfügbar ({type(e).__name__}).") from None
    if not daten:
        raise lulu.Fehler("Stimme lieferte keinen Ton.")
    return daten


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
                try:
                    import edge_tts  # noqa: F401
                    stimme = True
                except ImportError:
                    stimme = False
                self._json(200, {"hermes": hermes_online(env), "lulu": lulu_bereit(), "stimme": stimme,
                                 "gesetzt": {k: bool(env.get(k)) for k in SCHLUESSEL_REGELN},
                                 "github": bool(os.environ.get("LULU_GITHUB_TOKEN")),
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
        if self.path.split("?", 1)[0] == "/api/hoeren":
            laenge = int(self.headers.get("Content-Length") or 0)
            if not 0 < laenge <= 15_000_000:
                self._json(400, {"fehler": "Aufnahme fehlt oder ist zu groß"})
                return
            roh = self.rfile.read(laenge)
            try:
                self._json(200, {"text": hoeren(roh, self.headers.get("Content-Type", "audio/mp4"), env_laden())})
            except lulu.Fehler as e:
                sys.stderr.write(f"{e}\n")
                self._json(502, {"fehler": str(e)})
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
            if pfad == "/api/chat-strom":
                text = str(daten.get("text", "")).strip()[:4000]
                if not text:
                    self._json(400, {"fehler": "Leere Nachricht"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                def senden(obj):
                    self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode())
                    self.wfile.flush()
                tempo = str(daten.get("tempo", "schnell"))
                try:
                    auftrag = text
                    if tempo != "gruendlich" and env.get("GROQ_API_KEY"):
                        try:
                            auftrag = direkt_strom(text, env, senden)
                        except lulu.Fehler as e:
                            sys.stderr.write(f"Direktweg fehlgeschlagen: {e}\n")
                            auftrag = text  # dann eben über Hermes
                        if auftrag is None:
                            senden({"t": "ende", "status": "completed", "weg": "direkt"})
                            return
                        senden({"t": "weiter", "auftrag": auftrag})
                    gesammelt = []

                    def mitschreiben(obj):
                        if obj.get("t") == "text":
                            gesammelt.append(obj["d"])
                        senden(obj)
                    hermes_strom(auftrag, env, mitschreiben, "gruendlich" if tempo == "gruendlich" else "_standard")
                    if env.get("GROQ_API_KEY") and gesammelt:
                        _verlauf_merken("assistant", "".join(gesammelt))
                except lulu.Fehler as e:
                    senden({"t": "fehler", "d": str(e)})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if pfad == "/api/aktualisieren":
                # Holt die neueste Version aus dem eigenen Repo und installiert sie – in einem eigenen
                # systemd-Lauf, damit der Neustart der App das Update nicht abbricht.
                skript = (
                    "set -e; d=$(mktemp -d); cd \"$d\"; "
                    "curl -fsSL -o a.tgz https://codeload.github.com/" + UPDATE_REPO + "/tar.gz/main; "
                    "tar xzf a.tgz; bash */installieren.sh --nur-app; rm -rf \"$d\"")
                import subprocess
                try:
                    subprocess.run(["systemd-run", "--user", "--collect", "--unit",
                                    "jarvis-update-" + time.strftime("%H%M%S"), "bash", "-c", skript],
                                   check=True, timeout=20, capture_output=True)
                except (OSError, subprocess.SubprocessError) as e:
                    raise lulu.Fehler(f"Update konnte nicht starten ({type(e).__name__}).") from None
                self._json(200, {"ok": True, "hinweis": "Update läuft – in etwa 30 Sekunden neu laden."})
                return
            if pfad == "/api/schluessel":
                self._json(200, schluessel_speichern(str(daten.get("name", "")), str(daten.get("wert", ""))))
                return
            if pfad == "/api/neu":
                with _verlauf_sperre:
                    _verlauf.clear()
                self._json(200, {"gespraech": neues_gespraech()})
                return
            if pfad == "/api/sprechen":
                text = sprechtext(str(daten.get("text", "")))
                if not text:
                    self._json(400, {"fehler": "Kein Text"})
                    return
                mp3 = sprechen(text)
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(mp3)))
                self.end_headers()
                self.wfile.write(mp3)
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
    alten_github_namen_umziehen()
    env_laden()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Jarvis-App läuft auf http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
