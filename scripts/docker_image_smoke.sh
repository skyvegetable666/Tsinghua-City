#!/usr/bin/env bash
# Build and smoke-test the agent image without credentials or a live world.
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE="${CITY_DOCKER_IMAGE:-tsinghua-city-agent:0.1}"
DOCKER="${CITY_DOCKER_BIN:-docker}"

echo "building ${IMAGE}"
"${DOCKER}" build -t "${IMAGE}" -f deploy/agent.Dockerfile .

echo "checking sandbox baseline"
"${DOCKER}" run --rm --entrypoint sh "${IMAGE}" -c '
  set -e
  test "$(id -u)" = "1000"
  test -w /work
  test -w /shared
  command -v flock >/dev/null
  command -v claude >/dev/null
  claude --version
  python -c "import tsinghua_city"
  echo "sandbox smoke ok"
'
