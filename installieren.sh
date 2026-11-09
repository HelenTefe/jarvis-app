#!/usr/bin/env bash
# Jarvis-App installieren oder aktualisieren.
# Ausführen als Benutzer "jarvis" im entpackten Ordner:  bash installieren.sh
set -euo pipefail

if [ "$(id -un)" != "jarvis" ]; then
  echo "Bitte als Benutzer jarvis ausführen (su - jarvis)." >&2
  exit 1
fi

QUELLE="$(cd "$(dirname "$0")" && pwd)/app"
ZIEL="$HOME/jarvis-app"
ENV="$HOME/.hermes/.env"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "1/4 Dateien kopieren …"
mkdir -p "$ZIEL"
cp -r "$QUELLE"/. "$ZIEL"/
chmod 700 "$ZIEL"

echo "2/4 Hermes-Schnittstelle (nur lokal) einschalten …"
touch "$ENV"
grep -q '^API_SERVER_ENABLED=' "$ENV" || echo 'API_SERVER_ENABLED=true' >> "$ENV"
if ! grep -q '^API_SERVER_KEY=' "$ENV"; then
  echo "API_SERVER_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> "$ENV"
fi
chmod 600 "$ENV"

echo "3/4 Dienst einrichten …"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/jarvis-app.service" <<'EOF'
[Unit]
Description=Jarvis-App (Oberfläche für Hermes und Lulu)
After=network-online.target hermes-gateway.service

[Service]
ExecStart=/usr/bin/python3 %h/jarvis-app/server.py
Environment=JARVIS_NUTZER=HelenTefe@github
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable jarvis-app.service >/dev/null
systemctl --user restart jarvis-app.service

echo "4/4 Hermes neu starten (damit die Schnittstelle aktiv wird) …"
systemctl --user restart hermes-gateway.service || hermes gateway restart || true
sleep 5

if systemctl --user is-active --quiet jarvis-app.service; then
  echo "Fertig: Jarvis-App läuft auf 127.0.0.1:9130."
  echo "Als root einmalig ausführen:  tailscale serve --bg --https=8443 9130"
else
  echo "Jarvis-App startet nicht. Log:" >&2
  journalctl --user -u jarvis-app.service -n 30 --no-pager >&2 || true
  exit 1
fi
