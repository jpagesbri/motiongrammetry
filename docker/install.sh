#!/usr/bin/env bash
# Set up and start motiongrammetry in Docker: checks Docker and the GPU, prepares the data folder,
# builds (or reuses) the image, starts the web UI and opens it.
#
#   docker/install.sh                      detect the GPU, data in ~/motiongrammetry, port 8765
#   docker/install.sh --gpu none           CPU only (every step still works, slower)
#   docker/install.sh --workspaces DIR     use an existing folder of scans
#   docker/install.sh --with-vggt          add the optional VGGT step (asks you to accept its licenses)
#   docker/install.sh stop | logs | status | update
#
# Settings are remembered (in DATA/.env): later runs and `update` reuse them; flags change them.
# Options: --gpu auto|nvidia|none  --data DIR  --workspaces DIR  --videos DIR  --port N
#          --with-vggt / --without-vggt  --accept-vggt-license
#          --image NAME  --build (rebuild the image)  --no-open  --yes (no questions)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# empty = not given: the remembered value, else the default
cmd=up gpu="" data="$HOME/motiongrammetry" workspaces="" videos="" port="" image="" vggt=""
build=0 open_ui=1 yes=0 accept_vggt=0
while [ $# -gt 0 ]; do
  case "$1" in
    up|stop|logs|status|update) cmd="$1" ;;
    --gpu) gpu="$2"; shift ;;
    --data) data="$2"; shift ;;
    --workspaces) workspaces="$2"; shift ;;
    --videos) videos="$2"; shift ;;
    --port) port="$2"; shift ;;
    --image) image="$2"; shift ;;
    --with-vggt) vggt=1 ;;
    --without-vggt) vggt=0 ;;
    --accept-vggt-license) accept_vggt=1 ;;
    --build) build=1 ;;
    --no-open) open_ui=0 ;;
    --yes|-y) yes=1 ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
abspath() { case "$1" in /*) printf '%s\n' "${1%/}" ;; *) printf '%s\n' "$PWD/${1%/}" ;; esac; }
data="$(abspath "$data")"
envfile="$data/.env"
# one Docker project per data folder: a second setup (a test, another folder) never replaces yours
if [ "$data" = "$HOME/motiongrammetry" ]; then project=motiongrammetry
else project="motiongrammetry-$(basename "$data" | tr -c 'a-z0-9_-' '-' | sed 's/-*$//')"; fi
# Until 2026-10-31 the project was spelled "motiongrametry". Containers of that old service which use
# this data folder are removed (they hold the port), and the old default data folder moves, once.
retire_old() {
  local c
  for c in $(docker ps -aq --filter label=com.docker.compose.service=motiongrametry 2>/dev/null); do
    docker inspect -f '{{range .Mounts}}{{println .Source}}{{end}}' "$c" 2>/dev/null | grep -qxF -- "$1" || continue
    docker rm -f "$c" >/dev/null && ok "removed the old motiongrametry container"
  done
}
legacy="$HOME/motiongrametry"
if [ "$data" = "$HOME/motiongrammetry" ] && [ ! -e "$data" ] && [ -d "$legacy" ]; then
  retire_old "$legacy"
  mv "$legacy" "$data" && ok "moved $legacy to $data (the new spelling)"
  [ -f "$envfile" ] && sed -i "s#^MG_DATA=$legacy\$#MG_DATA=$data#; s#=$legacy/#=$data/#; s#motiongrametry:#motiongrammetry:#g" "$envfile"
fi
retire_old "$data"
[ -f "$envfile" ] && sed -i 's#motiongrametry:#motiongrammetry:#g' "$envfile"
saved() { [ -f "$envfile" ] || return 0; sed -n "s/^$1=//p" "$envfile" | tail -1; }  # nothing on a first install
gpu="${gpu:-$(saved MG_GPU_WANTED)}"; gpu="${gpu:-auto}"
workspaces="${workspaces:-$(saved MG_WORKSPACES)}"
videos="${videos:-$(saved MG_VIDEOS)}"
port="${port:-$(saved MG_PORT)}"; port="${port:-8765}"
image="${image:-$(saved MG_BASE_IMAGE)}"; image="${image:-motiongrammetry:nvidia}"
vggt_given=$([ -n "$vggt" ] && echo 1 || echo 0)
vggt="${vggt:-$(saved MG_VGGT)}"; vggt="${vggt:-0}"
vggt_accepted="$(saved MG_VGGT_ACCEPTED)"
gpu_wanted="$gpu"
version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$here/../pyproject.toml" | head -1)"
build() {  # build() TITLE docker-build-args...: the full log goes to DATA/build.log, its end shown on failure
  local title="$1"; shift
  local log="$data/build.log"
  echo "  $title (log: $log)"
  if ! docker build "$@" >"$log" 2>&1; then tail -25 "$log" | sed 's/^/    /'; die "the build failed: $log"; fi
}
compose() {
  local files=(-f "$here/compose.yml")
  [ "$(sed -n 's/^MG_GPU=//p' "$envfile" 2>/dev/null)" = nvidia ] && files+=(-f "$here/compose.nvidia.yml")
  MG_CONTAINER="$project" docker compose -p "$project" "${files[@]}" --env-file "$envfile" "$@"
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

if [ -z "$(saved MG_VGGT)" ] && [ "$vggt_given" = 0 ] && [ "$gpu" = nvidia ] && [ -t 0 ] && [ "$yes" = 0 ]; then
  bold "Optional: VGGT"
  echo "    An extra way to get the raw mesh: an AI model predicts it from the keyframes on the GPU in about a"
  echo "    minute (less detailed than the default path, handy for a quick look). Non-commercial use only."
  if ask "Add it? (you can add or remove it later: --with-vggt / --without-vggt)" n; then vggt=1; fi
fi
if [ "$vggt" = 1 ] && [ -z "$vggt_accepted" ]; then
  bold "VGGT licenses"
  cat <<'TERMS'
  VGGT predicts a raw mesh straight from the keyframes on the GPU. It comes with its own terms:
    - the model weights (facebook/VGGT-1B, downloaded on first use): CC BY-NC 4.0, NON-COMMERCIAL use only
      (Meta publishes facebook/VGGT-1B-Commercial, gated, for commercial use)
    - the VGGT code: Meta's VGGT License; pass a copy on if you redistribute it, cite VGGT in publications
    - PyTorch's CUDA libraries: NVIDIA's license
  Full texts: the About page of the app, or `mg notices`. It adds about 11 GB to the image (PyTorch with its CUDA libraries).
TERMS
  if [ "$accept_vggt" = 1 ]; then ok "license terms accepted (--accept-vggt-license)"
  elif [ -t 0 ] && [ "$yes" = 0 ]; then
    read -r -p "    Type 'accept' to install it: " a
    [ "$a" = accept ] || die "VGGT not installed (rerun with --with-vggt to try again)"
  else die "VGGT needs its license terms accepted: rerun interactively, or add --accept-vggt-license"; fi
  vggt_accepted="$(date -I)"
fi

bold "Data folder"
workspaces="$(abspath "${workspaces:-$data/workspaces}")"
videos="$(abspath "${videos:-$data/videos}")"
mkdir -p "$data/home" "$data/models" "$workspaces" "$videos"
ok "$data (scans: $workspaces, videos you can open by path: $videos → /videos)"

bold "Image"
if [ "$build" = 1 ] || [ "$cmd" = update ] || ! docker image inspect "$image" >/dev/null 2>&1; then
  build "building $image (the first time takes 30-60 minutes: OpenMVS compiles with CUDA)" -f "$here/Dockerfile" --build-arg VERSION="$version" -t "$image" "$here/.."
fi
ok "$image ($(docker image inspect "$image" --format '{{.Size}}' | awk '{printf "%.1f GB", $1/1e9}'))"
run_image="$image"

if [ "$vggt" = 1 ]; then
  bold "VGGT (optional step)"
  run_image="${image}-vggt"
  # rebuilt when asked, missing, or older than the image it builds on
  base_t="$(docker image inspect "$image" --format '{{.Created}}')"
  vggt_t="$(docker image inspect "$run_image" --format '{{.Created}}' 2>/dev/null || true)"
  if [ "$build" = 1 ] || [ "$cmd" = update ] || [ -z "$vggt_t" ] || [[ "$vggt_t" < "$base_t" ]]; then
    build "building $run_image (PyTorch and VGGT: about 10 minutes the first time)" -f "$here/Dockerfile.vggt" --build-arg BASE="$image" -t "$run_image" "$here/.."
  fi
  ok "$run_image ($(docker image inspect "$run_image" --format '{{.Size}}' | awk '{printf "%.1f GB", $1/1e9}'))"
  # weights this machine already downloaded (e.g. a direct install) need no second 5 GB download
  hf="$data/models/huggingface/hub/models--facebook--VGGT-1B" own="$HOME/.cache/huggingface/hub/models--facebook--VGGT-1B"
  if [ ! -e "$hf" ] && [ -d "$own" ]; then
    mkdir -p "$(dirname "$hf")" && cp -a --reflink=auto "$own" "$hf" && ok "reused the VGGT weights already on this machine"
  fi
fi

if [ "$gpu" = nvidia ] && ! nvidia_ok; then
  toolkit_help
  if ask "Start without the GPU for now?" y; then gpu=none; else exit 1; fi
fi
[ "$gpu" = nvidia ] && ok "Docker can use the GPU"

cat > "$envfile" <<EOF
# written by docker/install.sh; read by docker compose (see docker/compose.yml) and by the next run
MG_IMAGE=$run_image
MG_BASE_IMAGE=$image
MG_GPU=$gpu
MG_GPU_WANTED=$gpu_wanted
MG_VGGT=$vggt
MG_VGGT_ACCEPTED=$vggt_accepted
MG_UID=$(id -u)
MG_GID=$(id -g)
MG_PORT=$port
MG_DATA=$data
MG_WORKSPACES=$workspaces
MG_VIDEOS=$videos
EOF

bold "Start"
port_busy() { { ss -ltn 2>/dev/null || netstat -an 2>/dev/null | grep -i listen; } | grep -qE "[.:]$port[[:space:]]"; }
if port_busy; then
  compose ps --status running 2>/dev/null | grep -q "$project" \
    || die "port $port is in use (another mg ui?): stop it, or use --port 8766"
fi
compose up -d --force-recreate --quiet-pull >/dev/null 2>&1 || { compose up -d; die "it did not start (see above)"; }
url="http://127.0.0.1:$port"
for _ in $(seq 60); do curl -fs "$url/api/options" >/dev/null 2>&1 && break; sleep 1; done
curl -fs "$url/api/options" >/dev/null 2>&1 || die "it did not come up: docker/install.sh logs"
ok "motiongrammetry $version is running at $url"
echo "    stop: docker/install.sh stop · logs: docker/install.sh logs · rebuild after an update: docker/install.sh update"
if [ "$open_ui" = 1 ]; then (xdg-open "$url" || open "$url") >/dev/null 2>&1 || true; fi
