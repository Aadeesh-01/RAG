<#
.SYNOPSIS
  Deploy coderag to Azure Container Apps. Idempotent: re-run it after fixing anything.

.DESCRIPTION
  Creates or updates, in order:
    1. the resource group, a Log Analytics workspace and a Container Apps environment (Consumption profile);
    2. a storage account with the Azure Files share "coderag-data" (5 GB), registered in the
       environment and mounted at /data (uid=1000,gid=1000: the image's user);
    3. runs scripts/predeploy_check.py against prod.env (abort on failure);
    4. the container app: the GHCR image, 1 vCPU / 2 GiB, min 0 / max 1 replicas, external HTTPS
       ingress on 7860, startup/readiness probes on /api/ready and liveness on /api/health,
       the secrets from prod.env as Container Apps secrets;
    5. CODERAG_PUBLIC_URL=https://<fqdn> once the app's FQDN is known, then waits until healthy.
  Secrets are read from deploy/azure/prod.env (git-ignored) and never printed.

  Max replicas is 1 by design: rate limits, the daily LLM cap, the answer cache and the
  indexing queue live in the process's memory, and a starting replica marks running
  indexing jobs as interrupted. A second replica would split the limits and fail
  the other replica's jobs.

.EXAMPLE
  .\deploy\azure\deploy.ps1
  .\deploy\azure\deploy.ps1 -Location southeastasia
  .\deploy\azure\deploy.ps1 -Image ghcr.io/aadeesh-01/coderag:sha-1234567
#>
param(
    [string]$RG = 'coderag-rg',
    [string]$Location = 'centralindia',
    [string]$App = 'coderag',
    [string]$Image = '',                    # default: ghcr.io/<owner>/coderag:sha-<newest main commit that passed CI>
    [string]$Environment = 'coderag-env',
    [string]$Workspace = 'coderag-logs',
    [string]$Storage = '',                  # default: coderag<8 hex>, derived from the subscription and RG
    [string]$Share = 'coderag-data',
    [int]$ShareQuotaGB = 5,
    [string]$EnvFile = (Join-Path $PSScriptRoot 'prod.env'),
    [string]$Python = ''                    # for predeploy_check.py (default: .venv, else python)
)

. (Join-Path $PSScriptRoot 'lib.ps1')

$SecretEnv = [ordered]@{                    # Container Apps secret name -> env var (and prod.env key)
    'session-secret'       = 'CODERAG_SESSION_SECRET'
    'token-key'            = 'CODERAG_TOKEN_KEY'
    'github-client-secret' = 'CODERAG_GITHUB_CLIENT_SECRET'
    'database-url'         = 'CODERAG_DATABASE_URL'
    'groq-api-key'         = 'GROQ_API_KEY'
}
$PlainEnv = [ordered]@{
    'CODERAG_GITHUB_CLIENT_ID'   = ''       # from prod.env
    'CODERAG_TRUSTED_PROXY_HOPS' = '1'
    'CODERAG_DATA_DIR'           = '/data'
    'CODERAG_DEMOS_DIR'          = '/opt/demos'
    'CODERAG_MAX_LOADED_REPOS'   = '3'
    'HF_HUB_OFFLINE'             = '1'
    'CODERAG_LIVE_CHECK'         = 'false'
}
$Port = 7860
$StorageLink = 'coderag-data'               # the share's name inside the environment

# -- 0. preflight -------------------------------------------------------------
Write-Step 'preflight'
$account = Get-AzJson @('account', 'show', '--query', '{id:id, name:name}')
if (-not $account) { Stop-Deploy 'Not logged in: run az login' }
Write-Note "subscription: $($account.name)"

if (-not (Test-Path $EnvFile)) {
    Stop-Deploy "$EnvFile not found: copy deploy/azure/prod.env.example to deploy/azure/prod.env and fill it in"
}
$values = Read-EnvFile $EnvFile
$needed = @($SecretEnv.Values) + @('CODERAG_GITHUB_CLIENT_ID')
$missing = @($needed | Where-Object { -not $values[$_] })
if ($missing.Count) { Stop-Deploy "empty in $(Split-Path -Leaf $EnvFile): $($missing -join ', ')" }
foreach ($name in $SecretEnv.Values) { Add-Secret $values[$name] }
$PlainEnv['CODERAG_GITHUB_CLIENT_ID'] = $values['CODERAG_GITHUB_CLIENT_ID']
Write-Note "prod.env: all $($needed.Count) values set"

$slug = Get-RepoSlug
$owner = $slug.Split('/')[0].ToLower()
if (-not $Image) { $Image = "ghcr.io/$owner/coderag:$(Resolve-LatestTag $slug)" }
Write-Note "image: $Image"
Test-ImagePullable $Image

# -- 1. resource group, logs, environment -------------------------------------
Write-Step "resource group $RG"
$group = Get-AzJson @('group', 'show', '-n', $RG)
if ($group) {
    Write-Note "exists ($($group.location))"
} else {
    Test-PolicyRefusal (Invoke-Az @('group', 'create', '-n', $RG, '-l', $Location) -AllowFail) 'resource group' $Location
    Write-Note "created in $Location"
}

Write-Step "Log Analytics workspace $Workspace"
$ws = Get-AzJson @('monitor', 'log-analytics', 'workspace', 'show', '-g', $RG, '-n', $Workspace)
if (-not $ws) {
    Test-PolicyRefusal (Invoke-Az @('monitor', 'log-analytics', 'workspace', 'create', '-g', $RG, '-n', $Workspace,
        '-l', $Location, '--retention-time', '30') -AllowFail) 'Log Analytics workspace' $Location
    $ws = Get-AzJson @('monitor', 'log-analytics', 'workspace', 'show', '-g', $RG, '-n', $Workspace)
    Write-Note 'created (30-day retention)'
} else { Write-Note 'exists' }

Write-Step "Container Apps environment $Environment"
$cae = Get-AzJson @('containerapp', 'env', 'show', '-n', $Environment, '-g', $RG)
if (-not $cae) {
    $wsKey = Get-AzValue @('monitor', 'log-analytics', 'workspace', 'get-shared-keys', '-g', $RG, '-n', $Workspace,
        '--query', 'primarySharedKey')
    Add-Secret $wsKey
    Write-Note 'creating (Consumption workload profile; takes a few minutes)'
    Test-PolicyRefusal (Invoke-Az @('containerapp', 'env', 'create', '-n', $Environment, '-g', $RG, '-l', $Location,
        '--environment-mode', 'WorkloadProfiles', '--logs-destination', 'log-analytics',
        '--logs-workspace-id', $ws.customerId, '--logs-workspace-key', $wsKey) -AllowFail) 'Container Apps environment' $Location
    $cae = Get-AzJson @('containerapp', 'env', 'show', '-n', $Environment, '-g', $RG)
    Write-Note 'created'
} else { Write-Note "exists ($($cae.location))" }
# Without workload profiles, new environments are "Express", which can't mount storage; the
# Consumption profile of a workload-profiles environment bills the same way (per second, free grant).
if ($cae.properties.environmentMode -eq 'Express') {
    Stop-Deploy ("$Environment is an Express environment, which can't mount Azure Files. Delete it " +
        "(az containerapp env delete -n $Environment -g $RG --yes) and re-run.")
}
$defaultDomain = $cae.properties.defaultDomain

# -- 2. Azure Files at /data --------------------------------------------------
if (-not $Storage) {
    $md5 = [System.Security.Cryptography.MD5]::Create()
    $hash = -join ($md5.ComputeHash([System.Text.Encoding]::UTF8.GetBytes("$($account.id)/$RG")) | ForEach-Object { $_.ToString('x2') })
    $Storage = 'coderag' + $hash.Substring(0, 8)
}
Write-Step "storage account $Storage, share $Share ($ShareQuotaGB GB)"
$sa = Get-AzJson @('storage', 'account', 'show', '-g', $RG, '-n', $Storage)
if (-not $sa) {
    $free = Get-AzJson @('storage', 'account', 'check-name', '-n', $Storage)
    if (-not $free.nameAvailable) { Stop-Deploy "storage account name $Storage is taken: pass -Storage <another name>" }
    Test-PolicyRefusal (Invoke-Az @('storage', 'account', 'create', '-g', $RG, '-n', $Storage, '-l', $Location,
        '--sku', 'Standard_LRS', '--kind', 'StorageV2', '--https-only', 'true', '--min-tls-version', 'TLS1_2',
        '--allow-blob-public-access', 'false') -AllowFail) 'storage account' $Location
    Write-Note 'account created'
} else { Write-Note 'account exists' }
$fileShare = Get-AzJson @('storage', 'share-rm', 'show', '-g', $RG, '--storage-account', $Storage, '-n', $Share)
if (-not $fileShare) {
    Invoke-Az @('storage', 'share-rm', 'create', '-g', $RG, '--storage-account', $Storage, '-n', $Share,
        '--quota', "$ShareQuotaGB", '--enabled-protocols', 'SMB') | Out-Null
    Write-Note 'share created'
} elseif ($fileShare.shareQuota -ne $ShareQuotaGB) {
    Invoke-Az @('storage', 'share-rm', 'update', '-g', $RG, '--storage-account', $Storage, '-n', $Share,
        '--quota', "$ShareQuotaGB") | Out-Null
    Write-Note "share quota set to $ShareQuotaGB GB"
} else { Write-Note 'share exists' }
$link = Get-AzJson @('containerapp', 'env', 'storage', 'show', '-n', $Environment, '-g', $RG, '--storage-name', $StorageLink)
if (-not $link -or $link.properties.azureFile.accountName -ne $Storage -or $link.properties.azureFile.shareName -ne $Share) {
    $key = Get-AzValue @('storage', 'account', 'keys', 'list', '-g', $RG, '-n', $Storage, '--query', '[0].value')
    Add-Secret $key
    Invoke-Az @('containerapp', 'env', 'storage', 'set', '-n', $Environment, '-g', $RG, '--storage-name', $StorageLink,
        '--azure-file-account-name', $Storage, '--azure-file-account-key', $key, '--azure-file-share-name', $Share,
        '--access-mode', 'ReadWrite') | Out-Null
    Write-Note "registered in the environment as $StorageLink"
} else { Write-Note "registered in the environment as $StorageLink" }

# -- 3. predeploy check -------------------------------------------------------
$predictedUrl = "https://$App.$defaultDomain"
Write-Step "predeploy check (scripts/predeploy_check.py, CODERAG_PUBLIC_URL=$predictedUrl)"
if (-not $Python) {
    $Python = @((Join-Path $Script:RepoRoot '.venv\Scripts\python.exe'), (Join-Path $Script:RepoRoot '.venv/bin/python')) |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Python) { $Python = 'python' }
}
$checkDir = Join-Path ([System.IO.Path]::GetTempPath()) 'coderag-predeploy'
New-Item -ItemType Directory -Force $checkDir | Out-Null
$localDemos = Join-Path $Script:RepoRoot 'data\demos'
$checkEnv = @{}
foreach ($k in $values.Keys) { $checkEnv[$k] = $values[$k] }
foreach ($k in $PlainEnv.Keys) { $checkEnv[$k] = $PlainEnv[$k] }
$checkEnv['CODERAG_PUBLIC_URL'] = $predictedUrl
$checkEnv['CODERAG_DATA_DIR'] = $checkDir                       # /data is the container's; check a local dir
$checkEnv['CODERAG_DEMOS_DIR'] = $(if (Test-Path $localDemos) { $localDemos } else { $checkDir })
Write-Note 'the data-dir and demos rows describe this machine; the image has its own demos in /opt/demos'
$saved = @{}
@(Get-ChildItem env: | Where-Object { $_.Name -like 'CODERAG_*' -or $checkEnv.ContainsKey($_.Name) }) |
    ForEach-Object { $saved[$_.Name] = $_.Value; Remove-Item "env:$($_.Name)" }
try {
    foreach ($k in $checkEnv.Keys) { Set-Item "env:$k" $checkEnv[$k] }
    & $Python (Join-Path $Script:RepoRoot 'scripts\predeploy_check.py')
    $checkCode = $LASTEXITCODE
} finally {
    foreach ($k in $checkEnv.Keys) { Remove-Item "env:$k" -ErrorAction SilentlyContinue }
    foreach ($k in $saved.Keys) { Set-Item "env:$k" $saved[$k] }
}
if ($checkCode -ne 0) { Stop-Deploy 'the predeploy check failed (above); fix prod.env and re-run' }

# -- 4. the container app -----------------------------------------------------
$secretArgs = @(foreach ($s in $SecretEnv.Keys) { "$s=$($values[$SecretEnv[$s]])" })
$envArgs = @(foreach ($k in $PlainEnv.Keys) { "$k=$($PlainEnv[$k])" }) +
           @(foreach ($s in $SecretEnv.Keys) { "$($SecretEnv[$s])=secretref:$s" })

Write-Step "container app $App"
$existing = Get-AzJson @('containerapp', 'show', '-n', $App, '-g', $RG)
if (-not $existing) {
    Write-Note 'creating (phase 1: image, secrets, ingress, scale)'
    Test-PolicyRefusal (Invoke-Az (@('containerapp', 'create', '-n', $App, '-g', $RG, '--environment', $Environment,
        '--image', $Image, '--cpu', '1', '--memory', '2Gi', '--min-replicas', '0', '--max-replicas', '1',
        '--ingress', 'external', '--target-port', "$Port", '--transport', 'auto', '--revisions-mode', 'single',
        '--secrets') + $secretArgs + @('--env-vars') + $envArgs) -AllowFail) 'container app' $Location
    $before = $null
} else {
    Write-Note 'exists: updating secrets and ingress'
    Invoke-Az (@('containerapp', 'secret', 'set', '-n', $App, '-g', $RG, '--secrets') + $secretArgs) | Out-Null
    Invoke-Az @('containerapp', 'ingress', 'enable', '-n', $App, '-g', $RG, '--type', 'external',
        '--target-port', "$Port", '--transport', 'auto') | Out-Null
    $before = $existing.properties.latestRevisionName
}
$fqdn = Get-AzValue @('containerapp', 'show', '-n', $App, '-g', $RG, '--query', 'properties.configuration.ingress.fqdn')
$publicUrl = "https://$fqdn"
if ($publicUrl -ne $predictedUrl) { Write-Note "FQDN differs from the predicted one: using $publicUrl" }

# phase 2: the whole template (PUBLIC_URL, probes, the /data volume). The YAML holds no secret
# values: env vars refer to the secrets by name, and a PATCH keeps the app's secrets.
Write-Step "configuring $App (phase 2: CODERAG_PUBLIC_URL=$publicUrl, probes, /data)"
function ConvertTo-YamlString([string]$s) { "'" + $s.Replace("'", "''") + "'" }
$envYaml = @(foreach ($k in $PlainEnv.Keys) { "      - name: $k`n        value: $(ConvertTo-YamlString $PlainEnv[$k])" }) +
           @("      - name: CODERAG_PUBLIC_URL`n        value: $(ConvertTo-YamlString $publicUrl)") +
           @(foreach ($s in $SecretEnv.Keys) { "      - name: $($SecretEnv[$s])`n        secretRef: $s" })
$yaml = @"
location: $(ConvertTo-YamlString $cae.location)
properties:
  template:
    containers:
    - name: $App
      image: $(ConvertTo-YamlString $Image)
      resources:
        cpu: 1.0
        memory: 2Gi
      env:
$($envYaml -join "`n")
      probes:
      - type: Startup
        httpGet:
          path: /api/ready
          port: $Port
        initialDelaySeconds: 20
        periodSeconds: 10
        timeoutSeconds: 5
        failureThreshold: 10
      - type: Readiness
        httpGet:
          path: /api/ready
          port: $Port
        periodSeconds: 15
        timeoutSeconds: 5
        failureThreshold: 3
      - type: Liveness
        httpGet:
          path: /api/health
          port: $Port
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
      storageName: $StorageLink
      mountOptions: uid=1000,gid=1000
"@
$yamlFile = Join-Path ([System.IO.Path]::GetTempPath()) "coderag-app-$PID.yaml"
[System.IO.File]::WriteAllText($yamlFile, $yaml.Replace("`r`n", "`n"), (New-Object System.Text.UTF8Encoding $false))
try {
    Invoke-Az @('containerapp', 'update', '-n', $App, '-g', $RG, '--yaml', $yamlFile) | Out-Null
} finally { Remove-Item $yamlFile -ErrorAction SilentlyContinue }

$after = Get-AzValue @('containerapp', 'show', '-n', $App, '-g', $RG, '--query', 'properties.latestRevisionName')
if ($before -and $after -eq $before) {
    Write-Note "no template change: restarting $after so it reads the current secrets"
    Invoke-Az @('containerapp', 'revision', 'restart', '-n', $App, '-g', $RG, '--revision', $after) | Out-Null
}

# -- 5. healthy? --------------------------------------------------------------
Write-Step "waiting for revision $after"
Wait-RevisionHealthy $App $RG 600 | Out-Null
$ready = Invoke-Probe "$publicUrl/api/ready" 300
$page = Invoke-Probe "$publicUrl/app" 120
Write-Note "GET /api/ready -> $ready, GET /app -> $page"
if ($ready -ne 200 -or $page -ne 200) {
    Stop-Deploy "the app is not answering. Logs: az containerapp logs show -n $App -g $RG --tail 100"
}

$hops = (Invoke-Az @('containerapp', 'logs', 'show', '-n', $App, '-g', $RG, '--tail', '300', '--format', 'text') -AllowFail).Out -split "`n" |
    Where-Object { $_ -match 'proxy hops:' } | Select-Object -Last 1

Write-Host ''
Write-Host 'Deployed.' -ForegroundColor Green
Write-Host "  URL:                  $publicUrl"
Write-Host "  image:                $Image"
Write-Host "  GitHub OAuth callback: $publicUrl/api/auth/github/callback"
Write-Host '                        (github.com/settings/developers -> your OAuth app -> Authorization callback URL)'
if ($hops) { Write-Host "  proxy hops log:       $(Hide-Secrets $hops.Trim())" }
else { Write-Host "  proxy hops log:       not in the last 300 lines; az containerapp logs show -n $App -g $RG --tail 300 | Select-String 'proxy hops'" }
