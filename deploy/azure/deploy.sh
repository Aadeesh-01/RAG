#!/usr/bin/env bash
# Deploy coderag to Azure Container Apps. Idempotent: re-run it after fixing anything.
# The same steps as deploy.ps1 (see there for the details):
#   1. resource group, Log Analytics workspace, Container Apps environment (Consumption profile);
#   2. storage account + Azure Files share "coderag-data" (5 GB), registered in the environment,
#      mounted at /data with uid=1000,gid=1000 (the image's user);
#   3. scripts/predeploy_check.py against prod.env (abort on failure);
#   4. the container app (GHCR image, 1 vCPU / 2Gi, min 0 / max 1 replicas, external HTTPS ingress
#      on 7860, probes, secrets from prod.env as Container Apps secrets);
#   5. CODERAG_PUBLIC_URL=https://<fqdn> once the FQDN is known; wait until healthy.
# Max replicas is 1 by design: rate limits, the daily LLM cap, the answer cache and the indexing
# queue live in the process's memory, and a starting replica marks running indexing jobs as
# interrupted, so a second replica would split the limits and fail the other one's jobs.
# Secrets come from deploy/azure/prod.env (git-ignored) and are never printed.
#
#   ./deploy/azure/deploy.sh
#   LOCATION=southeastasia ./deploy/azure/deploy.sh
#   TAG=sha-1234567 ./deploy/azure/deploy.sh
#   IMAGE=ghcr.io/aadeesh-01/coderag:sha-1234567 ./deploy/azure/deploy.sh

RG="${RG:-coderag-rg}"
LOCATION="${LOCATION:-centralindia}"
APP="${APP:-coderag}"
IMAGE="${IMAGE:-}"                       # default: ghcr.io/<owner>/coderag:sha-<newest main commit that passed CI>
TAG="${TAG:-}"                           # a tag instead of the default (e.g. sha-1234567)
ENVIRONMENT="${ENVIRONMENT:-coderag-env}"
WORKSPACE="${WORKSPACE:-coderag-logs}"
STORAGE="${STORAGE:-}"                   # default: coderag<8 hex>, derived from the subscription and RG
SHARE="${SHARE:-coderag-data}"
SHARE_QUOTA_GB="${SHARE_QUOTA_GB:-5}"
ENV_FILE="${ENV_FILE:-$(cd "$(dirname "$0")" && pwd)/prod.env}"
PYTHON="${PYTHON:-}"                     # for predeploy_check.py (default: .venv, else python3)

# shellcheck source=lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

SECRET_NAMES=(session-secret token-key github-client-secret database-url groq-api-key)
SECRET_VARS=(CODERAG_SESSION_SECRET CODERAG_TOKEN_KEY CODERAG_GITHUB_CLIENT_SECRET CODERAG_DATABASE_URL GROQ_API_KEY)
PORT=7860
STORAGE_LINK=coderag-data                # the share's name inside the environment

# ── 0. preflight ─────────────────────────────────────────────────────────────
step preflight
az_exists account show || die "Not logged in: run az login"
SUB_ID="$(az_value account show --query id)"
note "subscription: $(az_value account show --query name)"

[ -f "$ENV_FILE" ] || die "$ENV_FILE not found: copy deploy/azure/prod.env.example to deploy/azure/prod.env and fill it in"
missing=""
for var in "${SECRET_VARS[@]}" CODERAG_GITHUB_CLIENT_ID; do
  [ -n "$(env_get "$ENV_FILE" "$var")" ] || missing="$missing $var"
done
[ -z "$missing" ] || die "empty in $(basename "$ENV_FILE"):$missing"
for var in "${SECRET_VARS[@]}"; do add_secret "$(env_get "$ENV_FILE" "$var")"; done
CLIENT_ID="$(env_get "$ENV_FILE" CODERAG_GITHUB_CLIENT_ID)"
PLAIN_ENV=(
  "CODERAG_GITHUB_CLIENT_ID=$CLIENT_ID"
  "CODERAG_TRUSTED_PROXY_HOPS=1"
  "CODERAG_DATA_DIR=/data"
  "CODERAG_DEMOS_DIR=/opt/demos"
  "CODERAG_MAX_LOADED_REPOS=3"
  "HF_HUB_OFFLINE=1"
  "CODERAG_LIVE_CHECK=false"
)
note "prod.env: all $(( ${#SECRET_VARS[@]} + 1 )) values set"

# the same image update.sh picks: the newest main commit whose CI passed (or TAG / IMAGE)
IMAGE="$(resolve_image "$(repo_slug)" "$IMAGE" "$TAG")" || exit 1
image_pullable "$IMAGE"

# ── 1. resource group, logs, environment ─────────────────────────────────────
step "resource group $RG"
if az_exists group show -n "$RG"; then
  note "exists ($(az_value group show -n "$RG" --query location))"
else
  az_try group create -n "$RG" -l "$LOCATION" -o none; policy_check "resource group" "$LOCATION"
  note "created in $LOCATION"
fi

step "Log Analytics workspace $WORKSPACE"
if az_exists monitor log-analytics workspace show -g "$RG" -n "$WORKSPACE"; then
  note exists
else
  az_try monitor log-analytics workspace create -g "$RG" -n "$WORKSPACE" -l "$LOCATION" --retention-time 30 -o none
  policy_check "Log Analytics workspace" "$LOCATION"
  note "created (30-day retention)"
fi

step "Container Apps environment $ENVIRONMENT"
if az_exists containerapp env show -n "$ENVIRONMENT" -g "$RG"; then
  note "exists ($(az_value containerapp env show -n "$ENVIRONMENT" -g "$RG" --query location))"
else
  WS_ID="$(az_value monitor log-analytics workspace show -g "$RG" -n "$WORKSPACE" --query customerId)"
  WS_KEY="$(az_value monitor log-analytics workspace get-shared-keys -g "$RG" -n "$WORKSPACE" --query primarySharedKey)"
  add_secret "$WS_KEY"
  note "creating (Consumption workload profile; takes a few minutes)"
  az_try containerapp env create -n "$ENVIRONMENT" -g "$RG" -l "$LOCATION" --environment-mode WorkloadProfiles \
    --logs-destination log-analytics --logs-workspace-id "$WS_ID" --logs-workspace-key "$WS_KEY" -o none
  policy_check "Container Apps environment" "$LOCATION"
  note created
fi
DEFAULT_DOMAIN="$(az_value containerapp env show -n "$ENVIRONMENT" -g "$RG" --query properties.defaultDomain)"
ENV_LOCATION="$(az_value containerapp env show -n "$ENVIRONMENT" -g "$RG" --query location)"
# Without workload profiles, new environments are "Express", which can't mount storage; the
# Consumption profile of a workload-profiles environment bills the same way (per second, free grant).
[ "$(az_value containerapp env show -n "$ENVIRONMENT" -g "$RG" --query properties.environmentMode)" != Express ] ||
  die "$ENVIRONMENT is an Express environment, which can't mount Azure Files. Delete it (az containerapp env delete -n $ENVIRONMENT -g $RG --yes) and re-run."

# ── 2. Azure Files at /data ──────────────────────────────────────────────────
if [ -z "$STORAGE" ]; then
  if command -v md5sum >/dev/null 2>&1; then hash="$(printf '%s' "$SUB_ID/$RG" | md5sum | cut -c1-8)"
  else hash="$(printf '%s' "$SUB_ID/$RG" | md5 -q | cut -c1-8)"; fi
  STORAGE="coderag$hash"
fi
step "storage account $STORAGE, share $SHARE ($SHARE_QUOTA_GB GB)"
if az_exists storage account show -g "$RG" -n "$STORAGE"; then
  note "account exists"
else
  [ "$(az_value storage account check-name -n "$STORAGE" --query nameAvailable)" = true ] ||
    die "storage account name $STORAGE is taken: set STORAGE=<another name>"
  az_try storage account create -g "$RG" -n "$STORAGE" -l "$LOCATION" --sku Standard_LRS --kind StorageV2 \
    --https-only true --min-tls-version TLS1_2 --allow-blob-public-access false -o none
  policy_check "storage account" "$LOCATION"
  note "account created"
fi
if az_exists storage share-rm show -g "$RG" --storage-account "$STORAGE" -n "$SHARE"; then
  quota="$(az_value storage share-rm show -g "$RG" --storage-account "$STORAGE" -n "$SHARE" --query shareQuota)"
  if [ "$quota" != "$SHARE_QUOTA_GB" ]; then
    az_run storage share-rm update -g "$RG" --storage-account "$STORAGE" -n "$SHARE" --quota "$SHARE_QUOTA_GB" -o none
    note "share quota set to $SHARE_QUOTA_GB GB"
  else
    note "share exists"
  fi
else
  az_run storage share-rm create -g "$RG" --storage-account "$STORAGE" -n "$SHARE" --quota "$SHARE_QUOTA_GB" \
    --enabled-protocols SMB -o none
  note "share created"
fi
linked=""
if az_exists containerapp env storage show -n "$ENVIRONMENT" -g "$RG" --storage-name "$STORAGE_LINK"; then
  linked="$(az_value containerapp env storage show -n "$ENVIRONMENT" -g "$RG" --storage-name "$STORAGE_LINK" \
    --query "join('/', [properties.azureFile.accountName, properties.azureFile.shareName])")"
fi
if [ "$linked" != "$STORAGE/$SHARE" ]; then
  KEY="$(az_value storage account keys list -g "$RG" -n "$STORAGE" --query '[0].value')"
  add_secret "$KEY"
  az_run containerapp env storage set -n "$ENVIRONMENT" -g "$RG" --storage-name "$STORAGE_LINK" \
    --azure-file-account-name "$STORAGE" --azure-file-account-key "$KEY" --azure-file-share-name "$SHARE" \
    --access-mode ReadWrite -o none
fi
note "registered in the environment as $STORAGE_LINK"

# ── 3. predeploy check ───────────────────────────────────────────────────────
PREDICTED_URL="https://$APP.$DEFAULT_DOMAIN"
step "predeploy check (scripts/predeploy_check.py, CODERAG_PUBLIC_URL=$PREDICTED_URL)"
if [ -z "$PYTHON" ]; then
  for p in "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/.venv/Scripts/python.exe"; do
    [ -x "$p" ] && { PYTHON="$p"; break; }
  done
  [ -n "$PYTHON" ] || PYTHON=python3
fi
CHECK_DIR="$(mktemp -d)"
DEMOS="$REPO_ROOT/data/demos"; [ -d "$DEMOS" ] || DEMOS="$CHECK_DIR"
note "the data-dir and demos rows describe this machine; the image has its own demos in /opt/demos"
(
  for v in $(env | sed -n 's/^\(CODERAG_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
  while IFS= read -r k; do export "$k=$(env_get "$ENV_FILE" "$k")"; done < <(env_keys "$ENV_FILE")
  for kv in "${PLAIN_ENV[@]}"; do export "${kv?}"; done
  export CODERAG_PUBLIC_URL="$PREDICTED_URL" CODERAG_DATA_DIR="$CHECK_DIR" CODERAG_DEMOS_DIR="$DEMOS"
  "$PYTHON" "$REPO_ROOT/scripts/predeploy_check.py"
) || { rm -rf "$CHECK_DIR"; die "the predeploy check failed (above); fix prod.env and re-run"; }
rm -rf "$CHECK_DIR"

# ── 4. the container app ─────────────────────────────────────────────────────
SECRET_ARGS=() ENV_ARGS=("${PLAIN_ENV[@]}")
for i in "${!SECRET_NAMES[@]}"; do
  SECRET_ARGS+=("${SECRET_NAMES[$i]}=$(env_get "$ENV_FILE" "${SECRET_VARS[$i]}")")
  ENV_ARGS+=("${SECRET_VARS[$i]}=secretref:${SECRET_NAMES[$i]}")
done

step "container app $APP"
BEFORE=""
if az_exists containerapp show -n "$APP" -g "$RG"; then
  note "exists: updating secrets and ingress"
  BEFORE="$(az_value containerapp show -n "$APP" -g "$RG" --query properties.latestRevisionName)"
  az_run containerapp secret set -n "$APP" -g "$RG" --secrets "${SECRET_ARGS[@]}" -o none
  az_run containerapp ingress enable -n "$APP" -g "$RG" --type external --target-port "$PORT" --transport auto -o none
else
  note "creating (phase 1: image, secrets, ingress, scale)"
  az_try containerapp create -n "$APP" -g "$RG" --environment "$ENVIRONMENT" --image "$IMAGE" \
    --cpu 1 --memory 2Gi --min-replicas 0 --max-replicas 1 \
    --ingress external --target-port "$PORT" --transport auto --revisions-mode single \
    --secrets "${SECRET_ARGS[@]}" --env-vars "${ENV_ARGS[@]}" -o none
  policy_check "container app" "$LOCATION"
fi
FQDN="$(az_value containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn)"
PUBLIC_URL="https://$FQDN"
[ "$PUBLIC_URL" = "$PREDICTED_URL" ] || note "FQDN differs from the predicted one: using $PUBLIC_URL"

# phase 2: the whole template (PUBLIC_URL, probes, the /data volume). The YAML holds no secret
# values: env vars refer to the secrets by name, and a PATCH keeps the app's secrets.
step "configuring $APP (phase 2: CODERAG_PUBLIC_URL=$PUBLIC_URL, probes, /data)"
yaml_str() { printf "'%s'" "${1//\'/\'\'}"; }
YAML_FILE="$(mktemp)"
{
  echo "location: $(yaml_str "$ENV_LOCATION")"
  echo "properties:"
  echo "  template:"
  echo "    containers:"
  echo "    - name: $APP"
  echo "      image: $(yaml_str "$IMAGE")"
  echo "      resources:"
  echo "        cpu: 1.0"
  echo "        memory: 2Gi"
  echo "      env:"
  for kv in "${PLAIN_ENV[@]}" "CODERAG_PUBLIC_URL=$PUBLIC_URL"; do
    echo "      - name: ${kv%%=*}"
    echo "        value: $(yaml_str "${kv#*=}")"
  done
  for i in "${!SECRET_NAMES[@]}"; do
    echo "      - name: ${SECRET_VARS[$i]}"
    echo "        secretRef: ${SECRET_NAMES[$i]}"
  done
  cat <<EOF
      probes:
      - type: Startup
        httpGet:
          path: /api/ready
          port: $PORT
        initialDelaySeconds: 20
        periodSeconds: 10
        timeoutSeconds: 5
        failureThreshold: 10
      - type: Readiness
        httpGet:
          path: /api/ready
          port: $PORT
        periodSeconds: 15
        timeoutSeconds: 5
        failureThreshold: 3
      - type: Liveness
        httpGet:
          path: /api/health
          port: $PORT
        periodSeconds: 30
        timeoutSeconds: 5
        failureThreshold: 3
      volumeMounts:
      - volumeName: data
        mountPath: /data
    scale:
      minReplicas: 0
      maxReplicas: 1
    volumes:
    - name: data
      storageType: AzureFile
      storageName: $STORAGE_LINK
      mountOptions: uid=1000,gid=1000
EOF
} > "$YAML_FILE"
az_try containerapp update -n "$APP" -g "$RG" --yaml "$YAML_FILE" -o none
rm -f "$YAML_FILE"
[ "$AZ_CODE" -eq 0 ] || die "az containerapp update --yaml failed:
$AZ_ERR"

AFTER="$(az_value containerapp show -n "$APP" -g "$RG" --query properties.latestRevisionName)"
if [ -n "$BEFORE" ] && [ "$AFTER" = "$BEFORE" ]; then
  note "no template change: restarting $AFTER so it reads the current secrets"
  az_run containerapp revision restart -n "$APP" -g "$RG" --revision "$AFTER" -o none
fi

# ── 5. healthy? ──────────────────────────────────────────────────────────────
step "waiting for revision $AFTER"
wait_healthy "$APP" "$RG" 300
READY="$(probe_url "$PUBLIC_URL/api/ready" 300)"
PAGE="$(probe_url "$PUBLIC_URL/app" 120)"
note "GET /api/ready -> $READY, GET /app -> $PAGE"
[ "$READY" = 200 ] && [ "$PAGE" = 200 ] || die "the app is not answering. Logs: az containerapp logs show -n $APP -g $RG --tail 100"

az_try containerapp logs show -n "$APP" -g "$RG" --tail 300 --format text
HOPS="$(printf '%s\n' "$AZ_OUT" | grep 'proxy hops:' | tail -1 || true)"

echo
printf '\033[32mDeployed.\033[0m\n'
echo "  URL:                   $PUBLIC_URL"
echo "  image:                 $IMAGE"
echo "  GitHub OAuth callback: $PUBLIC_URL/api/auth/github/callback"
echo "                         (github.com/settings/developers -> your OAuth app -> Authorization callback URL)"
if [ -n "$HOPS" ]; then echo "  proxy hops log:        $(hide_secrets "$HOPS")"
else echo "  proxy hops log:        not in the last 300 lines; az containerapp logs show -n $APP -g $RG --tail 300 | grep 'proxy hops'"; fi
