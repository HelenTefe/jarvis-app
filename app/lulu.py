#!/usr/bin/env python3
"""Lulu-Steuerung für Hermes (Jarvis).

Nur feste, ungefährliche Befehle – kein freier Datenbank- oder GitHub-Zugriff.

  python3 lulu.py auftrag <wochenbericht|themen|wartung|entwurf|posts> [thema]
  python3 lulu.py laeufe
  python3 lulu.py beitraege [wartet_auf_freigabe|freigegeben|abgelehnt|gepostet|fehler|alle]
  python3 lulu.py beitrag <id>
  python3 lulu.py freigeben <id>
  python3 lulu.py ablehnen <id>
  python3 lulu.py posten
  python3 lulu.py aufgaben
  python3 lulu.py kosten

Umgebungsvariablen: GITHUB_TOKEN, LULU_SUPABASE_URL, LULU_SUPABASE_KEY,
optional LULU_REPO (Standard: HelenTefe/Ai-agent-lulu).
Nur Python-Standardbibliothek.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO = os.environ.get("LULU_REPO", "HelenTefe/Ai-agent-lulu")
AUFTRAEGE = {"wochenbericht", "themen", "wartung", "entwurf", "posts"}
MIT_THEMA = {"entwurf", "posts"}
STATUS = {"wartet_auf_freigabe", "freigegeben", "abgelehnt", "gepostet", "fehler"}


class Fehler(Exception):
    pass


def _umgebung(name):
    wert = os.environ.get(name, "").strip()
    if not wert:
        raise Fehler(f"{name} fehlt. Bitte in Hermes unter Einstellungen → Keys eintragen.")
    return wert


def _anfrage(methode, url, kopf, daten=None):
    body = json.dumps(daten).encode() if daten is not None else None
    req = urllib.request.Request(url, data=body, method=methode, headers=kopf)
    try:
        with urllib.request.urlopen(req, timeout=30) as antwort:
            inhalt = antwort.read().decode() or "null"
            return json.loads(inhalt) if inhalt.strip() else None
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")[:300]
        raise Fehler(f"HTTP {e.code} bei {methode} {url.split('?')[0]}: {text}") from None
    except urllib.error.URLError as e:
        raise Fehler(f"Keine Verbindung: {e.reason}") from None


# ---------- GitHub ----------

def _gh(methode, pfad, daten=None):
    kopf = {
        "Authorization": "Bearer " + _umgebung("GITHUB_TOKEN"),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "lulu-hermes-skill",
    }
    if daten is not None:
        kopf["Content-Type"] = "application/json"
    return _anfrage(methode, f"https://api.github.com/repos/{REPO}{pfad}", kopf, daten)


def _workflow_starten(datei, inputs=None):
    daten = {"ref": "main"}
    if inputs:
        daten["inputs"] = inputs
    _gh("POST", f"/actions/workflows/{datei}/dispatches", daten)


# ---------- Supabase ----------

def _sb(methode, pfad, daten=None, zurueck=False):
    basis = _umgebung("LULU_SUPABASE_URL").rstrip("/")
    schluessel = _umgebung("LULU_SUPABASE_KEY")
    kopf = {"apikey": schluessel, "Authorization": "Bearer " + schluessel}
    if daten is not None:
        kopf["Content-Type"] = "application/json"
    if zurueck:
        kopf["Prefer"] = "return=representation"
    return _anfrage(methode, f"{basis}/rest/v1/{pfad}", kopf, daten)


def _id(wert):
    if not str(wert).isdigit():
        raise Fehler("Die Beitrags-Nummer muss eine Zahl sein.")
    return int(wert)


# ---------- Befehle ----------

def auftrag(args):
    if not args or args[0] not in AUFTRAEGE:
        raise Fehler("Erlaubte Aufträge: " + ", ".join(sorted(AUFTRAEGE)))
    name = args[0]
    thema = " ".join(args[1:]).strip()
    if name in MIT_THEMA and not thema:
        raise Fehler(f"Für '{name}' brauche ich ein Thema bzw. einen Artikel-Link.")
    if len(thema) > 300:
        raise Fehler("Thema ist zu lang (höchstens 300 Zeichen).")
    inputs = {"auftrag": name}
    if thema:
        inputs["thema"] = thema
    _workflow_starten("lulu.yml", inputs)
    return {"gestartet": name, "thema": thema or None,
            "hinweis": "Läuft auf GitHub, dauert meist 2–10 Minuten, kostet höchstens 1 $.",
            "verlauf": f"https://github.com/{REPO}/actions"}


def laeufe(_args):
    gh = _gh("GET", "/actions/runs?per_page=5")
    github = [{
        "workflow": r.get("name"), "status": r.get("status"), "ergebnis": r.get("conclusion"),
        "gestartet": r.get("created_at"), "link": r.get("html_url"),
    } for r in (gh or {}).get("workflow_runs", [])]
    protokoll = _sb("GET", "agent_laeufe?select=zeitpunkt,auftrag,thema,erfolgreich,kosten_usd,zusammenfassung"
                    "&order=zeitpunkt.desc&limit=5")
    return {"github_laeufe": github, "lulu_protokoll": protokoll}


def beitraege(args):
    status = args[0] if args else "wartet_auf_freigabe"
    filter_ = "" if status == "alle" else f"&status=eq.{status}"
    if status != "alle" and status not in STATUS:
        raise Fehler("Status muss einer davon sein: " + ", ".join(sorted(STATUS)) + ", alle")
    return _sb("GET", "beitraege?select=id,plattform,status,text,link,geplant_fuer,angelegt_am,fehler"
               f"{filter_}&order=angelegt_am.desc&limit=20")


def beitrag(args):
    if not args:
        raise Fehler("Welche Beitrags-Nummer?")
    zeilen = _sb("GET", f"beitraege?id=eq.{_id(args[0])}&select=*")
    if not zeilen:
        raise Fehler("Diesen Beitrag gibt es nicht.")
    return zeilen[0]


def _entscheiden(args, neuer_status):
    if len(args) != 1:
        raise Fehler("Bitte genau eine Beitrags-Nummer angeben.")
    nr = _id(args[0])
    daten = {"status": neuer_status}
    if neuer_status == "freigegeben":
        from datetime import datetime, timezone
        daten["freigegeben_am"] = datetime.now(timezone.utc).isoformat()
    # Nur Beiträge ändern, die noch auf Freigabe warten.
    zeilen = _sb("PATCH", f"beitraege?id=eq.{nr}&status=eq.wartet_auf_freigabe", daten, zurueck=True)
    if not zeilen:
        raise Fehler(f"Beitrag {nr} wartet nicht (mehr) auf Freigabe – nichts geändert.")
    z = zeilen[0]
    return {"beitrag": nr, "plattform": z.get("plattform"), "neuer_status": z.get("status")}


def freigeben(args):
    return _entscheiden(args, "freigegeben")


def ablehnen(args):
    return _entscheiden(args, "abgelehnt")


def posten(_args):
    _workflow_starten("posten.yml")
    return {"gestartet": "posten",
            "hinweis": "Veröffentlicht nur freigegebene, fällige Beiträge (max. 5). Kostet nichts."}


def aufgaben(_args):
    return _sb("GET", "aufgaben?select=id,titel,beschreibung,prioritaet,fuer,angelegt_am"
               "&status=eq.offen&order=prioritaet.asc,angelegt_am.asc&limit=20")


def kosten(_args):
    return _sb("GET", "kosten_pro_monat?select=*&limit=6")


BEFEHLE = {
    "auftrag": auftrag, "laeufe": laeufe, "beitraege": beitraege, "beitrag": beitrag,
    "freigeben": freigeben, "ablehnen": ablehnen, "posten": posten,
    "aufgaben": aufgaben, "kosten": kosten,
}


def main(argv):
    if not argv or argv[0] not in BEFEHLE:
        print(__doc__)
        return 2
    try:
        ergebnis = BEFEHLE[argv[0]](argv[1:])
    except Fehler as e:
        print(json.dumps({"fehler": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps(ergebnis, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
