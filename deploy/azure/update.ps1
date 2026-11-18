<#
.SYNOPSIS
  Point the coderag container app at a new image tag and wait until the new revision is healthy.

.DESCRIPTION
  The default tag is sha-<7> of the newest commit on main whose CI run succeeded (CI pushes
  ghcr.io/<owner>/coderag:sha-<short> and :latest from main). Only the image changes; the
  secrets, env vars, probes and the /data volume stay as deploy.ps1 set them.

.EXAMPLE
  .\deploy\azure\update.ps1
  .\deploy\azure\update.ps1 -Tag sha-1234567
  .\deploy\azure\update.ps1 -Image ghcr.io/aadeesh-01/coderag:latest
#>
param(
    [string]$RG = 'coderag-rg',
    [string]$App = 'coderag',
    [string]$Tag = '',                      # default: the newest main commit that passed CI
    [string]$Image = '',                    # a full image reference instead of -Tag
    [int]$TimeoutSeconds = 600
)

. (Join-Path $PSScriptRoot 'lib.ps1')

Write-Step 'preflight'
$current = Get-AzJson @('containerapp', 'show', '-n', $App, '-g', $RG, '--query',
    '{image:properties.template.containers[0].image, revision:properties.latestRevisionName, fqdn:properties.configuration.ingress.fqdn}')
if (-not $current) { Stop-Deploy "container app $App not found in ${RG}: run deploy.ps1 first" }
if (-not $Image) {
    $slug = Get-RepoSlug
    if (-not $Tag) { $Tag = Resolve-LatestTag $slug }
    $Image = "ghcr.io/$($slug.Split('/')[0].ToLower())/coderag:$Tag"
}
Write-Note "running: $($current.image) ($($current.revision))"
Write-Note "target:  $Image"
Test-ImagePullable $Image

if ($current.image -eq $Image) {
    Write-Note 'already on this image; checking its health'
} else {
    Write-Step "updating $App"
    Invoke-Az @('containerapp', 'update', '-n', $App, '-g', $RG, '--container-name', $App, '--image', $Image) | Out-Null
}
$revision = Get-AzValue @('containerapp', 'show', '-n', $App, '-g', $RG, '--query', 'properties.latestRevisionName')
Write-Step "waiting for revision $revision"
Wait-RevisionHealthy $App $RG $TimeoutSeconds | Out-Null
$ready = Invoke-Probe "https://$($current.fqdn)/api/ready" 300
Write-Note "GET /api/ready -> $ready"
if ($ready -ne 200) { Stop-Deploy "the new revision is not ready. Logs: az containerapp logs show -n $App -g $RG --tail 100" }

Write-Host ''
Write-Host "Updated: https://$($current.fqdn) runs $Image ($revision)" -ForegroundColor Green
