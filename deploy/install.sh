#!/usr/bin/env bash
# =============================================================================
# Install / update the Waveshare relay simulator on a Docker host
# (Proxmox LXC or VM running Debian/Ubuntu).
#
# One-liner (public repo), run as root inside the LXC/VM:
#   curl -fsSL https://raw.githubusercontent.com/RomainBenetiere/Simu-WS/main/deploy/install.sh \
#     | bash -s -- --repo RomainBenetiere/Simu-WS [--auto-update]
#
# Private repo / private GHCR package: export a GitHub token first
# (classic PAT with scopes `repo` + `read:packages`):
#   export GITHUB_TOKEN=ghp_xxx GITHUB_USER=<login>
#
# Options:
#   --repo owner/repo   GitHub repository (default: RomainBenetiere/Simu-WS)
#   --branch name       branch holding deploy/ files (default: main)
#   --tag tag           image tag to run (default: latest)
#   --dir path          install directory (default: /opt/waveshare-relay)
#   --auto-update       daily cron: pull new image & restart if changed
#
# Re-running the script is safe: it updates the compose file, keeps your .env
# and pulls the latest image.
# =============================================================================
set -euo pipefail

REPO="RomainBenetiere/Simu-WS"
BRANCH="main"
TAG="latest"
DIR="/opt/waveshare-relay"
AUTO_UPDATE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --auto-update) AUTO_UPDATE=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo -i)"
[[ -n "$REPO" ]] || die "--repo owner/repo is required"

REPO_LC="$(echo "$REPO" | tr '[:upper:]' '[:lower:]')"   # GHCR names are lowercase
IMAGE="ghcr.io/${REPO_LC}:${TAG}"
RAW_URL="https://raw.githubusercontent.com/${REPO}/${BRANCH}/deploy"

# ---- 1. Docker -------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker Engine (get.docker.com)..."
  apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || die "docker compose plugin missing"
systemctl enable --now docker >/dev/null 2>&1 || true

# ---- 2. Files --------------------------------------------------------------
log "Installing into ${DIR}"
mkdir -p "$DIR"
cd "$DIR"

CURL_AUTH=()
[[ -n "${GITHUB_TOKEN:-}" ]] && CURL_AUTH=(-H "Authorization: token ${GITHUB_TOKEN}")

curl -fsSL "${CURL_AUTH[@]}" "${RAW_URL}/docker-compose.yml" -o docker-compose.yml \
  || die "cannot download ${RAW_URL}/docker-compose.yml (private repo? set GITHUB_TOKEN)"

if [[ ! -f .env ]]; then
  # First install: generate .env with this host's LAN IP.
  HOST_IP="$(hostname -I | awk '{print $1}')"
  curl -fsSL "${CURL_AUTH[@]}" "${RAW_URL}/.env.example" -o .env
  sed -i \
    -e "s|^WAVESHARE_IMAGE=.*|WAVESHARE_IMAGE=${IMAGE}|" \
    -e "s|^WAVESHARE_IP=.*|WAVESHARE_IP=${HOST_IP}|" \
    .env
  log "Generated .env (IP ${HOST_IP}) — edit ${DIR}/.env to customise"
else
  sed -i "s|^WAVESHARE_IMAGE=.*|WAVESHARE_IMAGE=${IMAGE}|" .env
  log "Keeping existing .env"
fi

# ---- 3. Registry login (only for private packages) -------------------------
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  log "Logging in to ghcr.io"
  echo "$GITHUB_TOKEN" | docker login ghcr.io -u "${GITHUB_USER:-${REPO%%/*}}" --password-stdin
fi

# ---- 4. Start --------------------------------------------------------------
log "Pulling ${IMAGE}"
docker compose pull
docker compose up -d --remove-orphans

# ---- 5. Optional auto-update -----------------------------------------------
if [[ $AUTO_UPDATE -eq 1 ]]; then
  cat >/etc/cron.d/waveshare-relay-update <<EOF
# Daily 04:15: pull new image published by GitHub Actions, restart if changed.
15 4 * * * root cd ${DIR} && docker compose pull -q && docker compose up -d && docker image prune -f >/dev/null 2>&1
EOF
  log "Auto-update enabled (/etc/cron.d/waveshare-relay-update)"
fi

set -a; . ./.env; set +a
log "Done!"
echo "   Web UI     : http://${WAVESHARE_IP}:${HOST_WEB_PORT:-8080}/"
echo "   Modbus TCP : ${WAVESHARE_IP}:${HOST_MODBUS_PORT:-5020}"
echo "   Logs       : cd ${DIR} && docker compose logs -f"
