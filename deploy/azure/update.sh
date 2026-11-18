#!/usr/bin/env bash
# Point the coderag container app at a new image tag and wait until the new revision is healthy.
# The default tag is sha-<7> of the newest commit on main whose CI run succeeded (CI pushes
# ghcr.io/<owner>/coderag:sha-<short> and :latest from main). Only the image changes; the
# secrets, env vars, probes and the /data volume stay as deploy.sh set them.
#
#   ./deploy/azure/update.sh
#   TAG=sha-1234567 ./deploy/azure/update.sh
#   IMAGE=ghcr.io/aadeesh-01/coderag:latest ./deploy/azure/update.sh

RG="${RG:-coderag-rg}"
APP="${APP:-coderag}"
TAG="${TAG:-}"                           # default: the newest main commit that passed CI
IMAGE="${IMAGE:-}"                       # a full image reference instead of TAG
TIMEOUT="${TIMEOUT:-600}"

# shellcheck source=lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

step preflight
az_exists containerapp show -n "$APP" -g "$RG" || die "container app $APP not found in $RG: run deploy.sh first"
CURRENT="$(az_value containerapp show -n "$APP" -g "$RG" --query 'properties.template.containers[0].image')"
FQDN="$(az_value containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn)"
if [ -z "$IMAGE" ]; then
  SLUG="$(repo_slug)"
  [ -n "$TAG" ] || TAG="$(latest_tag "$SLUG")"
  IMAGE="ghcr.io/$(printf '%s' "${SLUG%%/*}" | tr '[:upper:]' '[:lower:]')/coderag:$TAG"
fi
note "running: $CURRENT"
note "target:  $IMAGE"
image_pullable "$IMAGE"

if [ "$CURRENT" = "$IMAGE" ]; then
  note "already on this image; checking its health"
else
  step "updating $APP"
  az_run containerapp update -n "$APP" -g "$RG" --container-name "$APP" --image "$IMAGE" -o none
fi
REVISION="$(az_value containerapp show -n "$APP" -g "$RG" --query properties.latestRevisionName)"
step "waiting for revision $REVISION"
wait_healthy "$APP" "$RG" "$TIMEOUT"
READY="$(probe_url "https://$FQDN/api/ready" 300)"
note "GET /api/ready -> $READY"
[ "$READY" = 200 ] || die "the new revision is not ready. Logs: az containerapp logs show -n $APP -g $RG --tail 100"

echo
printf '\033[32mUpdated: https://%s runs %s (%s)\033[0m\n' "$FQDN" "$IMAGE" "$REVISION"
