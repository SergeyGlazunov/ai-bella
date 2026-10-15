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

run_as_service() {
  # Prefer runuser so supplementary groups (e.g. docker) are applied.
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$SERVICE_USER" -- "$@"
  else
    su -s /bin/sh "$SERVICE_USER" -c 'exec "$@"' -- "$@"
  fi
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

docker_socket_path() {
  host="${DOCKER_HOST:-unix:///var/run/docker.sock}"
  case "$host" in
    unix://*)
      printf '%s\n' "${host#unix://}"
      ;;
    /*)
      printf '%s\n' "$host"
      ;;
    *)
      # tcp:// or other remotes: no local socket group to join
      printf '\n'
      ;;
  esac
}

ensure_docker_access() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker CLI not found; install Docker Engine, then re-run bootstrap." >&2
    exit 1
  fi

  sock=$(docker_socket_path)
  if [ -n "$sock" ]; then
    if [ ! -S "$sock" ]; then
      echo "Docker socket missing: $sock" >&2
      echo "Start Docker (or set DOCKER_HOST), then re-run bootstrap." >&2
      echo "Do not chmod the socket world-writable." >&2
      exit 1
    fi
    sock_gid=$(stat -c '%g' "$sock" 2>/dev/null || true)
    if [ -z "$sock_gid" ]; then
      echo "Cannot read group of Docker socket $sock." >&2
      echo "Grant $SERVICE_USER access to the Engine endpoint, then re-run bootstrap." >&2
      exit 1
    fi
    sock_group=$(getent group "$sock_gid" | cut -d: -f1 || true)
    if [ -z "$sock_group" ]; then
      echo "Docker socket $sock is gid $sock_gid with no group name." >&2
      echo "Create a group for that gid, add $SERVICE_USER, then re-run bootstrap." >&2
      echo "Do not chmod the socket world-writable." >&2
      exit 1
    fi
    if ! id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx "$sock_group"; then
      if command -v usermod >/dev/null 2>&1; then
        usermod -aG "$sock_group" "$SERVICE_USER"
      else
        echo "usermod unavailable; add $SERVICE_USER to group '$sock_group' manually, then re-run." >&2
        exit 1
      fi
    fi
  fi

  if ! run_as_service docker info >/dev/null 2>&1; then
    echo "Docker is not usable as $SERVICE_USER (read-only check failed)." >&2
    if [ -n "${sock:-}" ]; then
      echo "Socket: $sock. Ensure $SERVICE_USER is in the socket group and re-login is not required for runuser." >&2
    else
      echo "DOCKER_HOST=$DOCKER_HOST — grant $SERVICE_USER credentials for that endpoint." >&2
    fi
    echo "Do not open the Docker socket to all users. Fix access, then re-run bootstrap." >&2
    exit 1
  fi
}

ensure_alert_permissions() {
  if [ ! -f "$DEST/alert.sh" ]; then
    return 0
  fi
  chown "root:$SERVICE_USER" "$DEST/alert.sh"
  chmod 750 "$DEST/alert.sh"
  if ! run_as_service test -x "$DEST/alert.sh"; then
    echo "$DEST/alert.sh is not executable by $SERVICE_USER (need root:$SERVICE_USER mode 0750)." >&2
    exit 1
  fi
}

ensure_service_user
ensure_docker_access

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
fi
ensure_alert_permissions

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

# Service user must be able to read the installed package and config.
chown -R "root:$SERVICE_USER" "$DEST"
chmod -R g+rX "$DEST"
chmod 750 "$DEST/venv/bin/contour" 2>/dev/null || true

if [ -f "$ROOT/systemd/ai-contour-tmpfiles.conf" ]; then
  cp "$ROOT/systemd/ai-contour-tmpfiles.conf" /etc/tmpfiles.d/ai-contour.conf
  if command -v systemd-tmpfiles >/dev/null 2>&1; then
    systemd-tmpfiles --create /etc/tmpfiles.d/ai-contour.conf || true
  fi
fi
if [ -f "$ROOT/systemd/ai-contour-logrotate" ]; then
  cp "$ROOT/systemd/ai-contour-logrotate" /etc/logrotate.d/ai-contour
fi

if ! run_as_service "$DEST/venv/bin/contour" --config "$ETC/contour.yaml" validate; then
  echo "contour validate failed as $SERVICE_USER; edit $ETC/contour.yaml (adapters, alert.command, paths)." >&2
  echo "If alert.sh is still the kit stub, replace $DEST/alert.sh with a real channel (mode 0750, root:$SERVICE_USER)." >&2
  echo "Ensure $SERVICE_USER can read any secrets used by alert.command." >&2
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
echo "service user: $SERVICE_USER (Docker access verified)"
