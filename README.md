# Jarvis-App

Helens eigene Sprach-Oberfläche für Hermes (KI-Assistent) und Lulu (Affiliate-Agenten).

- `app/server.py` – kleiner Server (nur Python-Standardbibliothek), läuft auf `127.0.0.1:9130`
- `app/web/` – die Oberfläche (Handy zuerst, als App auf dem Startbildschirm installierbar)
- `app/lulu.py` – feste, sichere Lulu-Befehle (Beiträge freigeben/ablehnen, Zahlen)
- `installieren.sh` – richtet alles als Dienst ein

**Keine Schlüssel in diesem Repo.** Alle Zugangsdaten liegen nur auf dem Server in `~/.hermes/.env`.

## Sicherheit

- Der Server lauscht nur lokal; erreichbar ausschließlich über Tailscale Serve
  (`https://jarvis.<tailnet>.ts.net:8443`), also nur von Helens eigenen Geräten.
- Jede Anfrage muss den Tailscale-Kopf `Tailscale-User-Login` tragen und zu `JARVIS_NUTZER` passen.
- Änderungen (Chat, Freigeben, Ablehnen) brauchen zusätzlich den Kopf `X-Jarvis: 1` und dieselbe Herkunft.
- Freigeben geht nur einzeln, nur für Beiträge im Status „wartet_auf_freigabe“, mit Doppel-Tipp.

## Installieren / Aktualisieren (auf dem Server)

```bash
su - jarvis
curl -fsSL --proto-default https -o jarvis-app.tgz codeload.github.com/HelenTefe/jarvis-app/tar.gz/main
tar xzf jarvis-app.tgz
bash jarvis-app-main/installieren.sh
exit
tailscale serve --bg --https=8443 9130     # einmalig, als root
```
