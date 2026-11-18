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
  if printf '%s' "$AZ_ERR" | grep -Eq 'RequestDisallowedBy(Policy|Azure)'; then
    local code allowed
    code="$(printf '%s' "$AZ_ERR" | grep -Eo 'RequestDisallowedBy(Policy|Azure)' | head -1)"
    printf '\n\033[31mAzure refused the %s in %s (%s).\033[0m\n' "$1" "$2" "$code" >&2
    az_try policy assignment list --query "[].parameters.listOfAllowedLocations.value[]" -o tsv
    allowed="$(printf '%s' "$AZ_OUT" | tr -d '\r' | tr '\n\t' '  ' | sed 's/  */ /g; s/^ //; s/ $//')"
    if [ -n "$allowed" ]; then
      echo "This subscription may deploy to: $allowed" >&2
      echo "    LOCATION=${allowed%% *} ./deploy/azure/deploy.sh" >&2
    else
      echo "Azure for Students subscriptions only allow some regions. Re-run with one of them, e.g.:" >&2
      echo "    LOCATION=southeastasia ./deploy/azure/deploy.sh     # or eastasia, westeurope, eastus" >&2
      echo "The allowed list is in the policy assignment:" >&2
      echo "    az policy assignment list --query \"[].{name:displayName, params:parameters}\" -o json" >&2
    fi
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

# ghcr_tags REPO: the sha- tags of a public GHCR package, one per line (nothing when they can't be listed).
ghcr_tags() {
  local token
  token="$(curl -fsS --max-time 20 "https://ghcr.io/token?scope=repository:$1:pull" 2>/dev/null |
    sed -E 's/.*"token":"([^"]+)".*/\1/' || true)"
  [ -n "$token" ] || return 0
  curl -fsS --max-time 20 -H "Authorization: Bearer $token" "https://ghcr.io/v2/$1/tags/list?n=1000" 2>/dev/null |
    grep -o '"sha-[0-9a-f]*"' | tr -d '"' || true
}

# latest_tag SLUG: sha-<7> of the newest main commit whose CI run passed (CI pushes ghcr.io/<owner>/coderag:sha-<7>
# only after every check passed), with a line on stderr saying where it came from. deploy and update both use it.
#   1. the GitHub API: the newest successful push run of ci.yml on main;
#   2. else: the newest commit on origin/main that has a sha-<7> image on GHCR.
latest_tag() {
  local json sha run when tags stale="" s
  json="$(curl -fsS --max-time 20 -H 'User-Agent: coderag-deploy' \
    "https://api.github.com/repos/$1/actions/workflows/ci.yml/runs?branch=main&event=push&status=success&per_page=1" 2>/dev/null || true)"
  sha="$(printf '%s' "$json" | grep -o '"head_sha": *"[0-9a-f]*"' | head -1 | grep -o '[0-9a-f]\{40\}' || true)"
  if [ -n "$sha" ]; then
    run="$(printf '%s' "$json" | grep -o '"run_number": *[0-9]*' | head -1 | grep -o '[0-9]*$' || true)"
    when="$(printf '%s' "$json" | grep -o '"updated_at": *"[^"]*"' | head -1 | sed -E 's/.*"([^"]*)"$/\1/' || true)"
    note "tag: sha-${sha:0:7}, the newest main commit whose CI passed (run #$run, $when)" >&2
    printf 'sha-%s' "${sha:0:7}"
    return 0
  fi
  note "GitHub API: no successful CI run on main found (or the API is unavailable)" >&2
  # fallback: the newest origin/main commit with an image on GHCR (an image exists only if its CI passed)
  git -C "$REPO_ROOT" fetch --quiet origin main 2>/dev/null || stale="; origin/main could not be fetched, so it may be older than GitHub"
  tags="$(ghcr_tags "$(printf '%s' "${1%%/*}" | tr '[:upper:]' '[:lower:]')/coderag")"
  for s in $(git -C "$REPO_ROOT" log origin/main --format=%H -n 100 2>/dev/null); do
    if printf '%s\n' "$tags" | grep -qx "sha-${s:0:7}"; then
      note "tag: sha-${s:0:7}, the newest origin/main commit with a published image (CI passed)$stale" >&2
      printf 'sha-%s' "${s:0:7}"
      return 0
    fi
  done
  die "Couldn't find an image for any recent main commit (GitHub API and GHCR both checked). Set IMAGE or TAG."
}

# resolve_image SLUG IMAGE TAG: the image deploy and update use (on stdout; what was chosen on stderr):
# IMAGE as given, else ghcr.io/<owner>/coderag:<TAG, or the newest CI-passed tag>.
resolve_image() {
  local slug="$1" image="$2" tag="$3"
  if [ -n "$image" ]; then note "image: $image (given with IMAGE)" >&2; printf '%s' "$image"; return 0; fi
  if [ -n "$tag" ]; then note "tag: $tag (given with TAG)" >&2; else tag="$(latest_tag "$slug")" || exit 1; fi
  image="ghcr.io/$(printf '%s' "${slug%%/*}" | tr '[:upper:]' '[:lower:]')/coderag:$tag"
  note "image: $image" >&2
  printf '%s' "$image"
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
# wake_url URL: one GET through the ingress (it starts a replica of an app scaled to zero); prints the status.
wake_url() { curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$1" || true; }

# wait_healthy APP RG TIMEOUT: until the latest revision is ready. Ready: provisioned, the latest ready
# revision, and Healthy (running, or scaled to zero after passing its probes). A provisioned revision
# without a healthy replica (scaled to zero, or not started) is woken with GET /api/ready through the
# ingress, at most every 20 s; a 200 from it also counts as ready. Fails after TIMEOUT s (default 5 min).
wait_healthy() {
  local app="$1" rg="$2" timeout="${3:-300}" deadline last="" state="waiting for the revision" revs latest ready fqdn
  local info prov health running replicas code last_wake=0 now
  deadline=$(( $(date +%s) + timeout ))
  # polling: a failed call (Azure answers 500s now and then) is retried, not fatal
  while [ "$(date +%s)" -lt "$deadline" ]; do
    az_try containerapp show -n "$app" -g "$rg" -o tsv \
      --query "join('|', [properties.latestRevisionName || '', properties.latestReadyRevisionName || '', properties.configuration.ingress.fqdn || ''])"
    revs="||"; [ "$AZ_CODE" -ne 0 ] || revs="${AZ_OUT//$'\r'/}"
    IFS='|' read -r latest ready fqdn <<EOF
$revs
EOF
    if [ -n "$latest" ]; then
      az_try containerapp revision show -n "$app" -g "$rg" --revision "$latest" -o tsv \
        --query "join('|', [properties.provisioningState || 'None', properties.healthState || 'None', properties.runningState || 'None', to_string(properties.replicas || \`0\`)])"
      info="None|None|None|0"; [ "$AZ_CODE" -ne 0 ] || info="${AZ_OUT//$'\r'/}"
      IFS='|' read -r prov health running replicas <<EOF
$info
EOF
      state="$latest: provisioning $prov, health $health, running $running, replicas $replicas"
      if [ "$prov" = Failed ] || [ "$running" = Failed ]; then
        die "revision $latest failed ($state). Logs: az containerapp logs show -n $app -g $rg --type system --tail 50"
      fi
      if [ "$prov" = Provisioned ] && [ "$ready" = "$latest" ] && [ "$health" = Healthy ]; then
        note "$state"; return 0                                    # running, or ScaledToZero after its probes passed
      fi
      now="$(date +%s)"
      if [ "$prov" = Provisioned ] && [ -n "$fqdn" ] && [ $((now - last_wake)) -ge 20 ]; then
        last_wake="$now"
        code="$(wake_url "https://$fqdn/api/ready")"               # scaled to zero or not started: wake it
        state="$state; woke it: GET /api/ready -> ${code:-000}"
        if [ "$ready" = "$latest" ] && [ "$code" = 200 ]; then note "$state"; return 0; fi
      fi
    fi
    [ "$state" = "$last" ] || { note "$state"; last="$state"; }
    sleep 10
  done
  die "the new revision was not ready after $((timeout / 60)) min (last seen: $state).
It may still be starting. Check: az containerapp revision list -n $app -g $rg -o table
and the logs: az containerapp logs show -n $app -g $rg --type system --tail 50"
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
