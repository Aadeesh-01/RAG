# Helpers shared by deploy.ps1 and update.ps1 (dot-sourced). Windows PowerShell 5.1 and pwsh 7.
#
# az is run as a child process with its arguments quoted here, not through az.cmd: cmd.exe
# would split an argument at "&" (a Neon URL has one) and could echo it in an error.
# Every value registered with Add-Secret is masked in anything printed.

Set-StrictMode -Version 1.0
$ErrorActionPreference = 'Stop'

$Script:Az = $null                         # @{Exe; Prefix}, found on first use
$Script:SecretValues = New-Object System.Collections.Generic.List[string]
$Script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

function Add-Secret([string]$Value) {
    if ($Value -and $Value.Length -ge 4) { $Script:SecretValues.Add($Value) }
}

function Hide-Secrets([string]$Text) {
    if (-not $Text) { return $Text }
    foreach ($s in $Script:SecretValues) { $Text = $Text.Replace($s, '***') }
    return $Text
}

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Note([string]$Message) { Write-Host "    $Message" }

function Stop-Deploy([string]$Message) {
    Write-Host ''
    Write-Host "ERROR: $(Hide-Secrets $Message)" -ForegroundColor Red
    exit 1
}

# -- az -----------------------------------------------------------------------
function Find-Az {
    $candidates = @()
    $cmd = Get-Command az -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $candidates += @("$env:ProgramFiles\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
                     "${env:ProgramFiles(x86)}\Microsoft SDKs\Azure\CLI2\wbin\az.cmd")
    foreach ($c in $candidates) {
        if (-not $c -or -not (Test-Path $c)) { continue }
        if ($c -like '*.cmd') {
            $python = Join-Path (Split-Path (Split-Path $c)) 'python.exe'
            if (Test-Path $python) { return @{ Exe = $python; Prefix = @('-X', 'utf8', '-IBm', 'azure.cli') } }
        } else {
            return @{ Exe = $c; Prefix = @() }
        }
    }
    Stop-Deploy 'Azure CLI not found. Install it from https://aka.ms/installazurecli and run az login.'
}

function ConvertTo-ArgumentString([string[]]$Arguments) {
    # Windows command-line quoting (CommandLineToArgvW rules)
    $quoted = foreach ($a in $Arguments) {
        if ($a -eq '') { '""' }
        elseif ($a -notmatch '[\s"]') { $a }
        else { '"' + (($a -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"' }
    }
    return ($quoted -join ' ')
}

function Invoke-Az {
    <# Runs az; returns @{Code; Out; Err} (Err masked). Fails the deploy on a non-zero exit unless -AllowFail. #>
    param([Parameter(Mandatory)][string[]]$Arguments, [switch]$AllowFail)
    if (-not $Script:Az) { $Script:Az = Find-Az }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Script:Az.Exe
    $psi.Arguments = ConvertTo-ArgumentString (@($Script:Az.Prefix) + $Arguments + @('--only-show-errors'))
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'      # ignored under -I; -X utf8 (Find-Az) covers az.cmd's Python
    $psi.EnvironmentVariables['AZ_INSTALLER'] = 'MSI'
    $p = [System.Diagnostics.Process]::Start($psi)
    $out = $p.StandardOutput.ReadToEndAsync()
    $err = $p.StandardError.ReadToEndAsync()
    $p.WaitForExit()
    $result = [pscustomobject]@{ Code = $p.ExitCode; Out = $out.Result.Trim(); Err = (Hide-Secrets $err.Result.Trim()) }
    if ($result.Code -ne 0 -and -not $AllowFail) {
        Stop-Deploy "az $($Arguments[0..([Math]::Min(2, $Arguments.Count - 1))] -join ' ') failed:`n$($result.Err)"
    }
    return $result
}

function Get-AzJson([string[]]$Arguments) {
    <# az ... -o json, parsed; $null when the resource does not exist. #>
    $r = Invoke-Az -Arguments ($Arguments + @('-o', 'json')) -AllowFail
    if ($r.Code -ne 0) {
        if ($r.Err -match 'ResourceNotFound|ResourceGroupNotFound|NotFound|could not be found|was not found|does not exist') { return $null }
        Stop-Deploy "az $($Arguments[0..([Math]::Min(2, $Arguments.Count - 1))] -join ' ') failed:`n$($r.Err)"
    }
    if (-not $r.Out) { return $null }
    return ($r.Out | ConvertFrom-Json)
}

function Get-AzValue([string[]]$Arguments) {
    <# az ... -o tsv (one value). #>
    return (Invoke-Az -Arguments ($Arguments + @('-o', 'tsv'))).Out
}

function Test-PolicyRefusal($Result, [string]$What, [string]$Location) {
    if ($Result.Code -eq 0) { return }
    if ($Result.Err -match 'RequestDisallowedBy(Policy|Azure)') {
        Write-Host ''
        $code = $Matches[0]
        Write-Host "Azure refused the $What in '$Location' ($code)." -ForegroundColor Red
        $allowed = (Invoke-Az @('policy', 'assignment', 'list', '--query',
            "[].parameters.listOfAllowedLocations.value[]", '-o', 'tsv') -AllowFail).Out -split '\s+' | Where-Object { $_ }
        if ($allowed) {
            Write-Host "This subscription may deploy to: $($allowed -join ', ')"
            Write-Host "    .\deploy\azure\deploy.ps1 -Location $($allowed[0])"
        } else {
            Write-Host 'Azure for Students subscriptions only allow some regions. Re-run with one of them, e.g.:'
            Write-Host '    .\deploy\azure\deploy.ps1 -Location southeastasia     # or eastasia, westeurope, eastus'
            Write-Host 'The allowed list is in the policy assignment:'
            Write-Host '    az policy assignment list --query "[].{name:displayName, params:parameters}" -o json'
        }
        exit 1
    }
    Stop-Deploy "creating the $What failed:`n$($Result.Err)"
}

# -- prod.env -----------------------------------------------------------------
function Read-EnvFile([string]$Path) {
    <# KEY=value lines (# comments, optional quotes, optional "export ") -> hashtable. Same rules as predeploy_check.py. #>
    $values = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $line = $line.Trim()
        if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { continue }
        if ($line.StartsWith('export ')) { $line = $line.Substring(7) }
        $i = $line.IndexOf('=')
        $values[$line.Substring(0, $i).Trim()] = $line.Substring($i + 1).Trim().Trim("'`"")
    }
    return $values
}

# -- the image ----------------------------------------------------------------
function Get-RepoSlug {
    <# owner/repo of the git remote "origin" (github.com). #>
    $url = (& git -C $Script:RepoRoot remote get-url origin 2>$null)
    if ($url -match 'github\.com[:/]([^/]+)/([^/.]+?)(\.git)?$') { return "$($Matches[1])/$($Matches[2])" }
    Stop-Deploy 'Could not read the GitHub owner from "git remote get-url origin"; pass -Image.'
}

function Get-GhcrTags([string]$Repo) {
    <# The tags of a public GHCR package (anonymous, as Container Apps pulls); @() when they can't be listed. #>
    try {
        $token = (Invoke-RestMethod -UseBasicParsing -TimeoutSec 20 -Uri "https://ghcr.io/token?scope=repository:${Repo}:pull").token
        $list = Invoke-RestMethod -UseBasicParsing -TimeoutSec 20 -Headers @{ Authorization = "Bearer $token" } `
            -Uri "https://ghcr.io/v2/$Repo/tags/list?n=1000"
        return @($list.tags)
    } catch { return @() }
}

function Resolve-LatestTag([string]$Slug) {
    <#
      sha-<7> of the newest main commit whose CI run passed (CI pushes ghcr.io/<owner>/coderag:sha-<7>
      only after every check passed), with a line saying where it came from. deploy and update both use it.
      1. the GitHub API: the newest successful push run of ci.yml on main;
      2. else: the newest commit on origin/main that has a sha-<7> image on GHCR.
    #>
    try {
        $runs = Invoke-RestMethod -UseBasicParsing -TimeoutSec 20 -Headers @{ 'User-Agent' = 'coderag-deploy' } `
            -Uri "https://api.github.com/repos/$Slug/actions/workflows/ci.yml/runs?branch=main&event=push&status=success&per_page=1"
        if ($runs.workflow_runs.Count -gt 0) {
            $run = $runs.workflow_runs[0]
            $tag = 'sha-' + $run.head_sha.Substring(0, 7)
            $title = (($run.head_commit.message -split "`n")[0]).Trim()
            Write-Note "tag: $tag, the newest main commit whose CI passed (run #$($run.run_number), $($run.updated_at): $title)"
            return $tag
        }
        Write-Note 'no successful CI run on main found through the GitHub API'
    } catch {
        Write-Note "GitHub API unavailable ($($_.Exception.Message))"
    }
    # fallback: the newest origin/main commit with an image on GHCR (an image exists only if its CI passed)
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & git -C $Script:RepoRoot fetch --quiet origin main 2>$null
    $fetched = $LASTEXITCODE -eq 0
    $shas = @(& git -C $Script:RepoRoot log origin/main --format=%H -n 100 2>$null)
    $ErrorActionPreference = $prev
    $owner = $Slug.Split('/')[0].ToLower()
    $tags = Get-GhcrTags "$owner/coderag"
    foreach ($sha in $shas) {
        $tag = 'sha-' + $sha.Trim().Substring(0, 7)
        if ($tags -contains $tag) {
            $stale = if ($fetched) { '' } else { '; origin/main could not be fetched, so it may be older than GitHub' }
            Write-Note "tag: $tag, the newest origin/main commit with a published image (CI passed)$stale"
            return $tag
        }
    }
    Stop-Deploy "Couldn't find an image for any recent main commit (GitHub API and GHCR both checked). Pass -Image or -Tag."
}

function Resolve-Image([string]$Slug, [string]$Image = '', [string]$Tag = '') {
    <# The image deploy and update use: -Image as given, else ghcr.io/<owner>/coderag:<-Tag or the newest CI-passed tag>. #>
    if ($Image) { Write-Note "image: $Image (given with -Image)"; return $Image }
    $owner = $Slug.Split('/')[0].ToLower()
    if ($Tag) { Write-Note "tag: $Tag (given with -Tag)" } else { $Tag = Resolve-LatestTag $Slug }
    $Image = "ghcr.io/$owner/coderag:$Tag"
    Write-Note "image: $Image"
    return $Image
}

function Test-ImagePullable([string]$Image) {
    <# The image (ghcr.io/owner/name:tag) can be pulled anonymously, which is what Container Apps does. #>
    if ($Image -notmatch '^ghcr\.io/([^:]+):(.+)$') { Write-Note "not on ghcr.io; skipping the pull check"; return }
    $repo, $tag = $Matches[1], $Matches[2]
    try {
        $token = (Invoke-RestMethod -UseBasicParsing -TimeoutSec 20 -Uri "https://ghcr.io/token?scope=repository:${repo}:pull").token
        $accept = 'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json'
        Invoke-WebRequest -UseBasicParsing -Method Head -TimeoutSec 20 -Uri "https://ghcr.io/v2/$repo/manifests/$tag" `
            -Headers @{ Authorization = "Bearer $token"; Accept = $accept } | Out-Null
        Write-Note "$Image can be pulled anonymously"
    } catch {
        $status = $null
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        $owner = $repo.Split('/')[0]
        if ($status -eq 404) { Stop-Deploy "$Image does not exist (has CI pushed it yet?)" }
        if ($status -in 401, 403) {
            Stop-Deploy ("$Image can't be pulled anonymously: the package is private or the tag is missing.`n" +
                "Make it public: https://github.com/users/$owner/packages/container/$($repo.Split('/')[1])/settings -> Change visibility -> Public")
        }
        Write-Note "could not check the image ($($_.Exception.Message)); continuing"
    }
}

# -- health -------------------------------------------------------------------
function Invoke-Wake([string]$Url) {
    <# One GET through the ingress (it starts a replica of an app scaled to zero); the status, or 0. #>
    try { return [int](Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri $Url).StatusCode }
    catch { if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode } else { return 0 } }
}

function Wait-RevisionHealthy([string]$App, [string]$RG, [int]$TimeoutSeconds = 300) {
    <#
      Waits until the latest revision is ready; returns the app's FQDN. Ready: provisioned, the latest
      ready revision, and Healthy (running, or scaled to zero after passing its probes). A provisioned
      revision without a healthy replica (scaled to zero, or not started) is woken with GET /api/ready
      through the ingress, at most every 20 s; a 200 from it also counts as ready. Fails after
      $TimeoutSeconds (5 min) with the last state it saw.
    #>
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $last = ''
    $state = 'waiting for the revision'
    $lastWake = [datetime]::MinValue
    # polling: a failed call (Azure answers 500s now and then) is retried, not fatal
    function Get-Polled([string[]]$Arguments) {
        $p = Invoke-Az -Arguments ($Arguments + @('-o', 'json')) -AllowFail
        if ($p.Code -ne 0 -or -not $p.Out) { return $null }
        return ($p.Out | ConvertFrom-Json)
    }
    while ((Get-Date) -lt $deadline) {
        $a = Get-Polled @('containerapp', 'show', '-n', $App, '-g', $RG, '--query',
            '{latest:properties.latestRevisionName, ready:properties.latestReadyRevisionName, fqdn:properties.configuration.ingress.fqdn}')
        if ($a -and $a.latest) {
            $r = Get-Polled @('containerapp', 'revision', 'show', '-n', $App, '-g', $RG, '--revision', $a.latest, '--query',
                '{prov:properties.provisioningState, health:properties.healthState, running:properties.runningState, replicas:properties.replicas}')
            if ($r) {
                $state = "$($a.latest): provisioning $($r.prov), health $($r.health), running $($r.running), replicas $([int]$r.replicas)"
                if ($r.prov -eq 'Failed' -or $r.running -eq 'Failed') {
                    Stop-Deploy "revision $($a.latest) failed ($state). Logs: az containerapp logs show -n $App -g $RG --type system --tail 50"
                }
                $provisioned = $r.prov -eq 'Provisioned' -and $a.ready -eq $a.latest
                if ($provisioned -and $r.health -eq 'Healthy') {             # running, or ScaledToZero after its probes passed
                    Write-Note $state
                    return $a.fqdn
                }
                if ($r.prov -eq 'Provisioned' -and $a.fqdn -and ((Get-Date) - $lastWake).TotalSeconds -ge 20) {
                    $lastWake = Get-Date
                    $code = Invoke-Wake "https://$($a.fqdn)/api/ready"     # scaled to zero or not started: wake it
                    $state += "; woke it: GET /api/ready -> $(if ($code) { $code } else { 'no answer' })"
                    if ($provisioned -and $code -eq 200) { Write-Note $state; return $a.fqdn }
                }
            }
        }
        if ($state -ne $last) { Write-Note $state; $last = $state }
        Start-Sleep -Seconds 10
    }
    $minutes = [Math]::Round($TimeoutSeconds / 60, 1)
    Stop-Deploy ("the new revision was not ready after $minutes min (last seen: $state).`n" +
        "It may still be starting. Check: az containerapp revision list -n $App -g $RG -o table`n" +
        "and the logs: az containerapp logs show -n $App -g $RG --type system --tail 50")
}

function Invoke-Probe([string]$Url, [int]$TimeoutSeconds = 240) {
    <# GET until 200 (the first request after scale-to-zero waits for a cold start); returns the status. #>
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $status = 0
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri $Url
            return [int]$resp.StatusCode
        } catch {
            if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode } else { $status = 0 }
            Start-Sleep -Seconds 5
        }
    }
    return $status
}
