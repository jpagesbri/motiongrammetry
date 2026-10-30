#!/usr/bin/env bash
# Set up and start motiongrametry in Docker: checks Docker and the GPU, prepares the data folder,
# builds (or reuses) the image, starts the web UI and opens it.
#
#   docker/install.sh                      detect the GPU, data in ~/motiongrametry, port 8765
#   docker/install.sh --gpu none           CPU only (every step still works, slower)
#   docker/install.sh --workspaces DIR     use an existing folder of scans
#   docker/install.sh stop | logs | status | update
#
# Options: --gpu auto|nvidia|none  --data DIR  --workspaces DIR  --videos DIR  --port N
#          --image NAME  --build (rebuild the image)  --no-open  --yes (no questions)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmd=up gpu=auto data="$HOME/motiongrametry" workspaces="" videos="" port=8765 image="motiongrametry:nvidia"
build=0 open_ui=1 yes=0
while [ $# -gt 0 ]; do
  case "$1" in
    up|stop|logs|status|update) cmd="$1" ;;
    --gpu) gpu="$2"; shift ;;
    --data) data="$2"; shift ;;
    --workspaces) workspaces="$2"; shift ;;
    --videos) videos="$2"; shift ;;
    --port) port="$2"; shift ;;
    --image) image="$2"; shift ;;
    --build) build=1 ;;
    --no-open) open_ui=0 ;;
    --yes|-y) yes=1 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
ask() {  # ask "question" default(y|n)
  [ "$yes" = 1 ] || [ ! -t 0 ] && { [ "$2" = y ]; return; }
  local a; read -r -p "  $1 [$( [ "$2" = y ] && echo Y/n || echo y/N )] " a
  a="${a:-$2}"; [[ "$a" =~ ^[Yy] ]]
}
data="$(realpath -m "$data")"
envfile="$data/.env"
compose() {
  local files=(-f "$here/compose.yml")
  [ "$(sed -n 's/^MG_GPU=//p' "$envfile" 2>/dev/null)" = nvidia ] && files+=(-f "$here/compose.nvidia.yml")
  docker compose "${files[@]}" --env-file "$envfile" "$@"
}

case "$cmd" in
  stop) compose down; exit ;;
  logs) compose logs -f --tail 200; exit ;;
  status) compose ps; exit ;;
esac

bold "Docker"
command -v docker >/dev/null || die "Docker is not installed: https://docs.docker.com/engine/install/ (Linux) or Docker Desktop"
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is missing (package docker-compose / docker-compose-plugin)"
if ! docker info >/dev/null 2>&1; then
  die "the Docker daemon is not reachable: start it (sudo systemctl start docker) and make sure you are in the docker group (sudo usermod -aG docker \$USER, then log out and in)"
fi
ok "Docker $(docker version --format '{{.Server.Version}}') is running"

bold "GPU"
nvidia_ok() { docker run --rm --gpus all --entrypoint nvidia-smi "$image" -L >/dev/null 2>&1; }
toolkit_help() {
  local id; id="$( . /etc/os-release 2>/dev/null; echo "${ID:-} ${ID_LIKE:-}")"
  echo "    Docker cannot use the NVIDIA GPU yet: install the NVIDIA Container Toolkit (needs sudo), then rerun this script."
  case "$id" in
    *arch*) echo "      sudo pacman -S nvidia-container-toolkit" ;;
    *ubuntu*|*debian*) echo "      see https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html#with-apt"
                       echo "      sudo apt install nvidia-container-toolkit   (after adding NVIDIA's apt repository as shown there)" ;;
    *fedora*|*rhel*) echo "      see https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html#with-dnf" ;;
    *) echo "      https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html" ;;
  esac
  echo "      sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
}
if [ "$gpu" = auto ]; then
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then gpu=nvidia
  elif [ -e /dev/kfd ] && lspci 2>/dev/null | grep -qiE "vga|display|3d.*(amd|ati)"; then
    warn "AMD GPU found: not supported by this image yet (ROCm variant planned); using the CPU"; gpu=none
  else gpu=none; fi
fi
case "$gpu" in
  nvidia) ok "NVIDIA GPU: $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -1)" ;;
  none) ok "no GPU: every step runs on the CPU" ;;
  *) die "--gpu must be auto, nvidia or none" ;;
esac

bold "Data folder"
workspaces="$(realpath -m "${workspaces:-$data/workspaces}")"
videos="$(realpath -m "${videos:-$data/videos}")"
mkdir -p "$data/home" "$data/models" "$workspaces" "$videos"
ok "$data (scans: $workspaces, videos you can open by path: $videos → /videos)"

bold "Image"
if [ "$build" = 1 ] || [ "$cmd" = update ] || ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "  building $image (the first time takes 30-60 minutes: OpenMVS compiles with CUDA)"
  docker build -f "$here/Dockerfile" -t "$image" "$here/.." || die "the build failed (see above)"
fi
ok "$image ($(docker image inspect "$image" --format '{{.Size}}' | awk '{printf "%.1f GB", $1/1e9}'))"

if [ "$gpu" = nvidia ] && ! nvidia_ok; then
  toolkit_help
  if ask "Start without the GPU for now?" y; then gpu=none; else exit 1; fi
fi
[ "$gpu" = nvidia ] && ok "Docker can use the GPU"

cat > "$envfile" <<EOF
# written by docker/install.sh; read by docker compose (see docker/compose.yml)
MG_IMAGE=$image
MG_GPU=$gpu
MG_UID=$(id -u)
MG_GID=$(id -g)
MG_PORT=$port
MG_DATA=$data
MG_WORKSPACES=$workspaces
MG_VIDEOS=$videos
EOF

bold "Start"
if ss -ltn 2>/dev/null | grep -q "127.0.0.1:$port \|\*:$port \|0.0.0.0:$port "; then
  compose ps --status running 2>/dev/null | grep -q motiongrametry \
    || die "port $port is in use (another mg ui?): stop it, or use --port 8766"
fi
compose up -d --force-recreate >/dev/null
url="http://127.0.0.1:$port"
for _ in $(seq 60); do curl -fs "$url/api/options" >/dev/null 2>&1 && break; sleep 1; done
curl -fs "$url/api/options" >/dev/null 2>&1 || die "it did not come up: docker/install.sh logs"
ok "motiongrametry is running at $url"
echo "    stop: docker/install.sh stop · logs: docker/install.sh logs · rebuild after an update: docker/install.sh update"
if [ "$open_ui" = 1 ]; then (xdg-open "$url" || open "$url") >/dev/null 2>&1 || true; fi
