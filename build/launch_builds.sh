#!/usr/bin/env bash
# Orchestrate the five GROMACS image builds on native silicon (issue #2).
# Launches 3 spawned instances, each building one or more images and pushing to
# ECR, then self-terminating on completion. Run from the repo root.
#
#   AWS_PROFILE=aws AWS_REGION=us-east-1 \
#   AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text) \
#   bash build/launch_builds.sh
#
# Each instance: native build (correct SIMD), scoped ECR-push IAM, 3h TTL +
# idle-timeout backstop, --on-complete terminate so cost is bounded, and
# --terminate-on-error so a failed bootstrap can't strand a box.
set -euo pipefail

: "${AWS_REGION:?export AWS_REGION}"
: "${AWS_ACCOUNT:?export AWS_ACCOUNT}"
REPO_URL="${REPO_URL:-https://github.com/scttfrdmn/aws-gromacs.git}"
ECR_REGISTRY="${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
POLICY="$(cd "$(dirname "$0")" && pwd)/ecr-push-policy.json"
TTL="3h"; IDLE="30m"

# arch-native instance -> images it builds. amd builds both SIMD variants on one
# Zen5 box; cuda builds on Intel (no GPU needed to compile CUDA GROMACS).
declare -A INSTANCE=(
  [arm]="c8g.8xlarge"
  [amd]="c8a.8xlarge"
  [intel]="c8i.8xlarge"
)
declare -A BUILDS=(
  [arm]="arm-sve=Dockerfile.arm"
  [amd]="amd-avx512=Dockerfile.amd amd-avx2=Dockerfile.amd-avx2"
  [intel]="intel-avx512=Dockerfile.intel cuda=Dockerfile.cuda"
)

launch() {
  local key="$1" itype="${INSTANCE[$1]}" builds="${BUILDS[$1]}"
  local name="gromacs-build-${key}"
  # The wrapper reads its inputs from the environment; pass them through the
  # remote shell that --command runs. curl the build script from the public repo.
  local remote="export ECR_REGISTRY='${ECR_REGISTRY}' AWS_REGION='${AWS_REGION}' \
REPO_URL='${REPO_URL}' BUILDS='${builds}' COMPLETION_FILE=/tmp/SPAWN_COMPLETE; \
curl -fsSL ${REPO_URL%.git}/raw/main/build/build_and_push.sh | bash"

  echo "== launching ${name} (${itype}) for: ${builds} =="
  spawn launch "${name}" \
    --instance-type "${itype}" \
    --region "${AWS_REGION}" \
    --ttl "${TTL}" \
    --idle-timeout "${IDLE}" \
    --iam-policy-file "${POLICY}" \
    --completion-file /tmp/SPAWN_COMPLETE \
    --on-complete terminate \
    --terminate-on-error \
    --command "${remote}" \
    -o json
}

for key in arm amd intel; do
  launch "$key"
done

echo
echo "3 build instances launched. Monitor: spawn list --region ${AWS_REGION}"
echo "When all terminate, verify tags:"
echo "  aws ecr list-images --repository-name gromacs-bench --region ${AWS_REGION}"
