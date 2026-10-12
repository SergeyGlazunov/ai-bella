#!/bin/sh
# Install AI-Contour orchestrator under /opt/ai-sync (WSL Ubuntu).
# Does not compose ai-stack, pull chat images, or use nsenter.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST=/opt/ai-sync
BLOCKER='WSL/systemd blocker: fix Ubuntu access (sharing violation on ext4.vhdx?) or enable systemctl; do not use nsenter.'

systemd_ready() {
  if command -v systemctl >/dev/null 2>&1 && systemctl --version >/dev/null 2>&1; then
    return 0
  fi
  if command -v wsl.exe >/dev/null 2>&1; then
    if ! wsl.exe -d Ubuntu -e true >/dev/null 2>&1; then
      echo "$BLOCKER" >&2
      return 1
    fi
  fi
  echo "$BLOCKER" >&2
  return 1
}

mkdir -p /srv/ai/secrets /srv/ai/logs
chmod 700 /srv/ai/secrets
mkdir -p "$DEST/integrations"

cp -r "$ROOT/controller" "$DEST/"
if [ -d "$ROOT/integrations" ]; then
  cp -r "$ROOT/integrations/." "$DEST/integrations/"
fi
if [ -f "$ROOT/requirements.lock" ]; then
  cp "$ROOT/requirements.lock" "$DEST/"
fi
if [ ! -f "$DEST/config.yaml" ]; then
  cp "$ROOT/contour.example.yaml" "$DEST/config.yaml"
  chmod 600 "$DEST/config.yaml"
fi
if [ -f "$ROOT/integrations/alert.sh.example" ] && [ ! -f "$DEST/alert.sh" ]; then
  cp "$ROOT/integrations/alert.sh.example" "$DEST/alert.sh"
  chmod 700 "$DEST/alert.sh"
fi
if [ -f "$DEST/integrations/idle_bsl.sh" ]; then
  chmod 755 "$DEST/integrations/idle_bsl.sh"
fi
for probe in probe_code_index.sh probe_graph.sh reload_bsl_watch.sh; do
  if [ -f "$DEST/integrations/$probe" ]; then
    chmod 755 "$DEST/integrations/$probe"
  fi
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found; install Python 3.11+ in WSL Ubuntu." >&2
  exit 1
fi

python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --upgrade pip
if [ -f "$DEST/requirements.lock" ]; then
  "$DEST/venv/bin/pip" install -r "$DEST/requirements.lock"
fi
"$DEST/venv/bin/pip" install -e "$DEST/controller"

if ! "$DEST/venv/bin/contour" --config "$DEST/config.yaml" validate; then
  echo "contour validate failed; edit $DEST/config.yaml (adapters, alert.command, paths)." >&2
  echo "If alert.sh is still the kit stub, replace it with a real channel (R09)." >&2
  exit 1
fi

if ! systemd_ready; then
  exit 1
fi

cp "$ROOT/systemd/ai-sync.service" "$ROOT/systemd/ai-sync.timer" /etc/systemd/system/
systemctl daemon-reload
# Do not enable the timer here. Cutover USER.md enables ai-sync.timer.

echo "bootstrap complete: $DEST"
echo "timer installed but not enabled"
