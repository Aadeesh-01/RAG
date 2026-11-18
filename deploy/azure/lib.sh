# Helpers shared by deploy.sh and update.sh (sourced). bash 3.2+ (macOS), Linux, Cloud Shell, Git Bash.
# Every value registered with add_secret is masked in anything printed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SECRETS=()

add_secret() { if [ "${#1}" -ge 4 ]; then SECRETS+=("$1"); fi; }

hide_secrets() {
  local text="$1" s
  for s in ${SECRETS[@]+"${SECRETS[@]}"}; do text="${text//"$s"/***}"; done
  printf '%s' "$text"
}

step() { printf '\033[36m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$(hide_secrets "$*")" >&2; exit 1; }

# ── az ───────────────────────────────────────────────────────────────────────
command -v az >/dev/null 2>&1 || die "Azure CLI not found. Install it from https://aka.ms/installazurecli and run az login."
AZ_ERR_FILE="$(mktemp)"
trap 'rm -f "$AZ_ERR_FILE"' EXIT
AZ_OUT="" AZ_ERR="" AZ_CODE=0

# az_try ARGS...: runs az; sets AZ_OUT, AZ_ERR (masked) and AZ_CODE, never exits.
az_try() {
  set +e
  AZ_OUT="$(az "$@" --only-show-errors 2>"$AZ_ERR_FILE")"
  AZ_CODE=$?
  set -e
  AZ_OUT="${AZ_OUT%$'\r'}"
  AZ_ERR="$(hide_secrets "$(cat "$AZ_ERR_FILE")")"
}

# az_run ARGS...: az_try, and exit on failure.
az_run() {
  az_try "$@"
  [ "$AZ_CODE" -eq 0 ] || die "az $1 ${2:-} ${3:-} failed:
$AZ_ERR"
}

# az_exists ARGS...: 0 if the `show` command finds the resource, 1 if not found; exits on other errors.
az_exists() {
  az_try "$@" -o none
  [ "$AZ_CODE" -eq 0 ] && return 0
  if printf '%s' "$AZ_ERR" | grep -Eqi 'ResourceNotFound|ResourceGroupNotFound|NotFound|could not be found|was not found|does not exist'; then
    return 1
  fi
  die "az $1 ${2:-} ${3:-} failed:
$AZ_ERR"
}

# az_value ARGS...: one value (-o tsv) on stdout.
az_value() { az_run "$@" -o tsv; printf '%s' "${AZ_OUT//$'\r'/}"; }

# policy_check WHAT LOCATION: after an az_try that creates something.
policy_check() {
  [ "$AZ_CODE" -eq 0 ] && return 0
  if printf '%s' "$AZ_ERR" | grep -q 'RequestDisallowedByPolicy'; then
    printf '\n\033[31mAzure Policy refused the %s in %s (RequestDisallowedByPolicy).\033[0m\n' "$1" "$2" >&2
    echo "Azure for Students subscriptions only allow some regions. Re-run with one of them, e.g.:" >&2
    echo "    LOCATION=southeastasia ./deploy/azure/deploy.sh     # or eastasia, westeurope, eastus" >&2
    echo "The allowed list is in the policy assignment:" >&2
    echo "    az policy assignment list --query \"[].{name:displayName, params:parameters}\" -o json" >&2
    exit 1
  fi
  die "creating the $1 failed:
$AZ_ERR"
}

# ── prod.env ─────────────────────────────────────────────────────────────────
# env_get FILE KEY: the value of KEY (same rules as predeploy_check.py: # comments, optional
# quotes, optional "export "); never sourced, so "&" or "$" in a value stay literal.
env_get() {
  local file="$1" key="$2" line k value=""
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in ''|'#'*) continue ;; esac
    line="${line#export }"
    case "$line" in *=*) ;; *) continue ;; esac
    k="${line%%=*}"; k="${k%"${k##*[![:space:]]}"}"
    [ "$k" = "$key" ] || continue
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"
    value="${value#[\"\']}"; value="${value%[\"\']}"
  done < "$file"
  printf '%s' "$value"
}

# env_keys FILE: the keys in FILE, one per line.
env_keys() {
  sed -e 's/\r$//' -e 's/^[[:space:]]*//' -e 's/^export //' "$1" | grep -E '^[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=' |
    sed -e 's/[[:space:]]*=.*//' | sort -u
}

# ── the image ────────────────────────────────────────────────────────────────
repo_slug() {
  local url
  url="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
  url="${url%.git}"
  case "$url" in
    *github.com[:/]*) printf '%s' "${url#*github.com[:/]}" ;;
    *) die 'Could not read the GitHub owner from "git remote get-url origin"; set IMAGE.' ;;
  esac
}

# latest_tag SLUG: sha-<7> of the newest main commit whose CI run succeeded (GitHub API; else origin/main).
latest_tag() {
  local sha
  sha="$(curl -fsS --max-time 20 -H 'User-Agent: coderag-deploy' \
    "https://api.github.com/repos/$1/actions/workflows/ci.yml/runs?branch=main&event=push&status=success&per_page=1" 2>/dev/null |
    grep -m1 '"head_sha"' | sed -E 's/.*"head_sha": *"([0-9a-f]+)".*/\1/' || true)"
  if [ -z "$sha" ]; then
    note "no successful CI run on main found; using origin/main" >&2
    git -C "$REPO_ROOT" fetch --quiet origin main 2>/dev/null || true
    sha="$(git -C "$REPO_ROOT" rev-parse origin/main)"
  fi
  printf 'sha-%s' "${sha:0:7}"
}

# image_pullable IMAGE: ghcr.io images must be pullable anonymously (that is what Container Apps does).
image_pullable() {
  local image="$1" repo tag token code
  case "$image" in ghcr.io/*:*) ;; *) note "not on ghcr.io; skipping the pull check"; return 0 ;; esac
  repo="${image#ghcr.io/}"; tag="${repo##*:}"; repo="${repo%:*}"
  token="$(curl -fsS --max-time 20 "https://ghcr.io/token?scope=repository:${repo}:pull" 2>/dev/null |
    sed -E 's/.*"token":"([^"]+)".*/\1/' || true)"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -I \
    -H "Authorization: Bearer $token" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json' \
    "https://ghcr.io/v2/$repo/manifests/$tag" || true)"
  case "$code" in
    200) note "$image can be pulled anonymously" ;;
    404) die "$image does not exist (has CI pushed it yet?)" ;;
    401|403) die "$image can't be pulled anonymously: the package is private or the tag is missing.
Make it public: https://github.com/users/${repo%%/*}/packages/container/${repo#*/}/settings -> Change visibility -> Public" ;;
    *) note "could not check the image (HTTP $code); continuing" ;;
  esac
}

# ── health ───────────────────────────────────────────────────────────────────
# wait_healthy APP RG TIMEOUT: until the latest revision is provisioned, the latest ready one and not
# unhealthy. With min replicas 0 it may then sit at zero replicas, so callers confirm with probe_url.
wait_healthy() {
  local app="$1" rg="$2" deadline=$(( $(date +%s) + ${3:-600} )) last="" state revs latest ready info prov health running
  while [ "$(date +%s)" -lt "$deadline" ]; do
    revs="$(az_value containerapp show -n "$app" -g "$rg" \
      --query "join('|', [properties.latestRevisionName || '', properties.latestReadyRevisionName || ''])")"
    IFS='|' read -r latest ready <<EOF
$revs
EOF
    state="waiting for the revision"
    if [ -n "$latest" ]; then
      info="$(az_value containerapp revision show -n "$app" -g "$rg" --revision "$latest" \
        --query "join('|', [properties.provisioningState || 'None', properties.healthState || 'None', properties.runningState || 'None'])")"
      IFS='|' read -r prov health running <<EOF
$info
EOF
      state="$latest: provisioning $prov, health $health, running $running"
      if [ "$prov" = Failed ] || [ "$running" = Failed ]; then
        die "revision $latest failed. Logs: az containerapp logs show -n $app -g $rg --tail 100"
      fi
      if [ "$prov" = Provisioned ] && [ "$ready" = "$latest" ] && [ "$health" != Unhealthy ]; then
        note "$state"; return 0
      fi
    fi
    [ "$state" = "$last" ] || { note "$state"; last="$state"; }
    sleep 10
  done
  die "the revision was not healthy in time. Logs: az containerapp logs show -n $app -g $rg --tail 100"
}

# probe_url URL TIMEOUT: GET until 200 (the first request after scale-to-zero waits for a cold start); prints the status.
probe_url() {
  local deadline=$(( $(date +%s) + ${2:-240} )) code=000
  while [ "$(date +%s)" -lt "$deadline" ]; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 "$1" || true)"
    [ "$code" = 200 ] && break
    sleep 5
  done
  printf '%s' "$code"
}
