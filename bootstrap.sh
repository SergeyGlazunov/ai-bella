#!/bin/sh
# Install AI-Contour orchestrator under /opt/ai-contour (WSL Ubuntu / Linux).
# Mutable data lives under /srv/ai-contour; config under /etc/ai-contour.
# Does not compose ai-stack, pull chat images, use nsenter, or enable the timer.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST=/opt/ai-contour
ETC=/etc/ai-contour
SRV=/srv/ai-contour
SERVICE_USER=ai-contour
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

ensure_service_user() {
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    return 0
  fi
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --home-dir "$SRV" --shell /usr/sbin/nologin "$SERVICE_USER"
    return 0
  fi
  echo "service user $SERVICE_USER missing and useradd unavailable" >&2
  exit 1
}

ensure_service_user

# Mutable layout (least privilege: package root root-owned; data owned by service user).
mkdir -p \
  "$SRV/state" \
  "$SRV/locks" \
  "$SRV/inbox/kb" \
  "$SRV/inbox/klon_opl" \
  "$SRV/sources/ubd" \
  "$SRV/sources/kb" \
  "$SRV/sources/klon_opl" \
  "$SRV/accepted/kb" \
  "$SRV/accepted/klon_opl" \
  "$SRV/logs/runs" \
  "$SRV/backups/state" \
  "$SRV/compose" \
  "$SRV/secrets"
chmod 700 "$SRV/secrets" "$SRV/locks"
chmod 750 "$SRV/state" "$SRV/logs" "$SRV/backups" "$SRV/inbox" "$SRV/accepted"
chown -R "$SERVICE_USER:$SERVICE_USER" "$SRV"

mkdir -p "$DEST/integrations" "$ETC"
chmod 755 "$DEST" "$ETC"

cp -r "$ROOT/controller" "$DEST/"
if [ -d "$ROOT/integrations" ]; then
  cp -r "$ROOT/integrations/." "$DEST/integrations/"
fi
if [ -f "$ROOT/requirements.lock" ]; then
  cp "$ROOT/requirements.lock" "$DEST/"
fi

if [ ! -f "$ETC/contour.yaml" ]; then
  cp "$ROOT/contour.example.yaml" "$ETC/contour.yaml"
  chmod 640 "$ETC/contour.yaml"
  chown "root:$SERVICE_USER" "$ETC/contour.yaml"
fi

if [ -f "$ROOT/integrations/alert.sh.example" ] && [ ! -f "$DEST/alert.sh" ]; then
  cp "$ROOT/integrations/alert.sh.example" "$DEST/alert.sh"
  chmod 700 "$DEST/alert.sh"
  chown "root:$SERVICE_USER" "$DEST/alert.sh"
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

if [ -f "$ROOT/systemd/ai-contour-tmpfiles.conf" ]; then
  cp "$ROOT/systemd/ai-contour-tmpfiles.conf" /etc/tmpfiles.d/ai-contour.conf
  if command -v systemd-tmpfiles >/dev/null 2>&1; then
    systemd-tmpfiles --create /etc/tmpfiles.d/ai-contour.conf || true
  fi
fi
if [ -f "$ROOT/systemd/ai-contour-logrotate" ]; then
  cp "$ROOT/systemd/ai-contour-logrotate" /etc/logrotate.d/ai-contour
fi

if ! "$DEST/venv/bin/contour" --config "$ETC/contour.yaml" validate; then
  echo "contour validate failed; edit $ETC/contour.yaml (adapters, alert.command, paths)." >&2
  echo "If alert.sh is still the kit stub, replace $DEST/alert.sh with a real channel." >&2
  exit 1
fi

if ! systemd_ready; then
  exit 1
fi

cp "$ROOT/systemd/ai-sync.service" "$ROOT/systemd/ai-sync.timer" /etc/systemd/system/
systemctl daemon-reload
# Do not enable the timer here. Cutover USER.md enables ai-sync.timer.

echo "bootstrap complete: package=$DEST config=$ETC/contour.yaml data=$SRV"
echo "timer installed but not enabled"
echo "service user: $SERVICE_USER"
