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

echo "1b/4 Stimme (Edge TTS) einrichten …"
UV="$(command -v uv || true)"
for k in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" "$HOME/.hermes/bin/uv" "$HOME"/.hermes/tools/uv-*/uv; do
  [ -z "$UV" ] && [ -x "$k" ] && UV="$k"
done
PY="/usr/bin/python3"
if [ -n "$UV" ]; then
  [ -x "$ZIEL/.venv/bin/python" ] || "$UV" venv --quiet "$ZIEL/.venv"
  if "$UV" pip install --quiet --python "$ZIEL/.venv/bin/python" "edge-tts==7.3.1"; then
    PY="$ZIEL/.venv/bin/python"
  else
    echo "   Hinweis: Stimme konnte nicht installiert werden – die App nutzt dann die iPhone-Stimme."
  fi
else
  echo "   Hinweis: uv nicht gefunden – die App nutzt die iPhone-Stimme."
fi

echo "1c/4 Lulu-Skill für Hermes installieren …"
SKILL_QUELLE="$(cd "$(dirname "$0")" && pwd)/skill/lulu"
if [ -d "$SKILL_QUELLE" ]; then
  mkdir -p "$HOME/.hermes/skills/lulu"
  cp -r "$SKILL_QUELLE"/. "$HOME/.hermes/skills/lulu"/
fi

echo "1d/4 Jarvis schlanker machen (unnötige Werkzeuge nur für die App aus) …"
hermes tools disable browser vision image_gen tts computer_use code_execution delegation clarify \
  --platform api_server >/dev/null 2>&1 || echo "   Hinweis: Werkzeuge konnten nicht angepasst werden."

echo "2/4 Hermes-Schnittstelle (nur lokal) einschalten …"
touch "$ENV"
grep -q '^API_SERVER_ENABLED=' "$ENV" || echo 'API_SERVER_ENABLED=true' >> "$ENV"
if ! grep -q '^API_SERVER_KEY=' "$ENV"; then
  echo "API_SERVER_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> "$ENV"
fi
chmod 600 "$ENV"

echo "3/4 Dienst einrichten …"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/jarvis-app.service" <<EOF
[Unit]
Description=Jarvis-App (Oberfläche für Hermes und Lulu)
After=network-online.target hermes-gateway.service

[Service]
ExecStart=$PY $ZIEL/server.py
Environment=JARVIS_NUTZER=HelenTefe@github
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable jarvis-app.service >/dev/null
systemctl --user restart jarvis-app.service

if [ "${1:-}" != "--nur-app" ]; then
  echo "4/4 Hermes neu starten (damit die Schnittstelle aktiv wird) …"
  systemctl --user restart hermes-gateway.service || hermes gateway restart || true
fi
sleep 5

if systemctl --user is-active --quiet jarvis-app.service; then
  echo "Fertig: Jarvis-App läuft auf 127.0.0.1:9130."
  echo "Als root einmalig ausführen:  tailscale serve --bg --https=8443 9130"
else
  echo "Jarvis-App startet nicht. Log:" >&2
  journalctl --user -u jarvis-app.service -n 30 --no-pager >&2 || true
  exit 1
fi
