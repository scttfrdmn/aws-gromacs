#!/usr/bin/env bash
# Runs ON a spawned AL2023 build instance. Clones the public repo, installs
# Docker, builds one or more GROMACS images NATIVELY on this instance's silicon
# (so SIMD-tuned binaries are trustworthy), and pushes them to ECR.
#
# Native builds are the point: the arm image wants Graviton4, the amd images
# want Zen5 (-march=znver5), etc. Emulated cross-builds would undermine the very
# numbers the harness exists to defend.
#
# Env in:
#   ECR_REGISTRY   <acct>.dkr.ecr.<region>.amazonaws.com
#   AWS_REGION     region for ECR login
#   REPO_URL       git URL of this repo (public)
#   BUILDS         space-separated tag=dockerfile pairs, e.g.
#                  "x86-avx512=Dockerfile.x86 cuda=Dockerfile.cuda"
#   COMPLETION_FILE  sentinel for spawn --on-complete (touched on success only)
set -euo pipefail

ECR_REGISTRY="${ECR_REGISTRY:?set ECR_REGISTRY}"
AWS_REGION="${AWS_REGION:?set AWS_REGION}"
REPO_URL="${REPO_URL:?set REPO_URL}"
BUILDS="${BUILDS:?set BUILDS}"
COMPLETION_FILE="${COMPLETION_FILE:-/tmp/SPAWN_COMPLETE}"
REPO="${ECR_REGISTRY}/gromacs-bench"

echo "== install docker + git =="
sudo dnf install -y -q docker git
sudo systemctl start docker

echo "== clone repo =="
workdir="$(mktemp -d)"
git clone --depth 1 "$REPO_URL" "$workdir/src"
cd "$workdir/src"

echo "== ecr login =="
aws ecr get-login-password --region "$AWS_REGION" \
  | sudo docker login --username AWS --password-stdin "$ECR_REGISTRY"

for pair in $BUILDS; do
  tag="${pair%%=*}"; dockerfile="${pair##*=}"
  echo "== build $tag from build/$dockerfile (native $(uname -m)) =="
  # Context is the repo root so the Dockerfile's `COPY mdrun_wrapper.sh` resolves.
  sudo docker build -f "build/$dockerfile" -t "${REPO}:${tag}" .
  echo "== push ${REPO}:${tag} =="
  sudo docker push "${REPO}:${tag}"
done

echo "== all builds pushed; signalling completion =="
touch "$COMPLETION_FILE"
