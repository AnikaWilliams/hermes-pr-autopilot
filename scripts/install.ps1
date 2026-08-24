[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(default|[A-Za-z0-9][A-Za-z0-9_.-]{0,63})$')]
    [string] $SourceProfile,

    [ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
    [string] $Repository = 'AnikaWilliams/hermes-pr-autopilot',

    [string] $HermesHome,

    [switch] $ReuseExistingWorkerProfiles,

    [switch] $EnableBackend,

    [switch] $ValidateOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($EnableBackend) {
    throw '-EnableBackend is not supported. Install the plugin disabled, then verify any existing controller is paused before you manually enable it or restart Hermes.'
}

function Assert-Command {
    param([Parameter(Mandatory = $true)][string] $Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' is not available on PATH."
    }
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)][string] $Command,
        [Parameter(Mandatory = $true)][string[]] $Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command '$Command $($Arguments -join ' ')' failed with exit code $LASTEXITCODE."
    }
}

function Get-HermesRoot {
    param([string] $RequestedRoot)

    if ($RequestedRoot) {
        return [System.IO.Path]::GetFullPath($RequestedRoot)
    }

    if ($env:HERMES_HOME) {
        $configured = [System.IO.Path]::GetFullPath($env:HERMES_HOME)
        $configuredItem = [System.IO.DirectoryInfo]::new($configured)
        if ($configuredItem.Parent -and $configuredItem.Parent.Name -eq 'profiles') {
            return $configuredItem.Parent.Parent.FullName
        }
        return $configured
    }

    if (-not $env:LOCALAPPDATA) {
        throw 'LOCALAPPDATA is not set. Supply -HermesHome with the base Hermes data directory.'
    }
    return Join-Path $env:LOCALAPPDATA 'hermes'
}

function Get-ProfilePath {
    param(
        [Parameter(Mandatory = $true)][string] $Root,
        [Parameter(Mandatory = $true)][string] $Profile
    )

    if ($Profile -eq 'default') {
        return $Root
    }
    return Join-Path (Join-Path $Root 'profiles') $Profile
}

function Get-Sha256Hash {
    param([Parameter(Mandatory = $true)][string] $Path)

    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try {
            return [System.BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '')
        }
        finally {
            $stream.Dispose()
        }
    }
    finally {
        $algorithm.Dispose()
    }
}

Assert-Command 'git'
Assert-Command 'gh'
Assert-Command 'hermes'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repositoryHost = 'github.com'
$resolvedHermesRoot = Get-HermesRoot -RequestedRoot $HermesHome
$sourceProfilePath = Get-ProfilePath -Root $resolvedHermesRoot -Profile $SourceProfile

if (-not (Test-Path -LiteralPath $sourceProfilePath -PathType Container)) {
    throw "Source profile '$SourceProfile' does not exist at '$sourceProfilePath'."
}

$workers = @(
    @{ Name = 'prtriage'; Skill = 'pr-autopilot-analyzer'; Description = 'Read-only analysis for PR Autopilot findings.' },
    @{ Name = 'prfix'; Skill = 'pr-autopilot-fixer'; Description = 'Bounded repair work for PR Autopilot findings.' },
    @{ Name = 'prverify'; Skill = 'pr-autopilot-verifier'; Description = 'Independent verification for PR Autopilot repairs.' }
)

foreach ($worker in $workers) {
    $skillSource = Join-Path (Join-Path (Join-Path $repositoryRoot 'skills') $worker.Skill) 'SKILL.md'
    if (-not (Test-Path -LiteralPath $skillSource -PathType Leaf)) {
        throw "Bundled skill is missing: $skillSource"
    }

    $profilePath = Get-ProfilePath -Root $resolvedHermesRoot -Profile $worker.Name
    if (Test-Path -LiteralPath $profilePath -PathType Container) {
        if (-not $ReuseExistingWorkerProfiles) {
            throw "Profile '$($worker.Name)' already exists. Review it, then rerun with -ReuseExistingWorkerProfiles if it is safe to use."
        }

        $skillTargetFile = Join-Path (Join-Path (Join-Path $profilePath 'skills') $worker.Skill) 'SKILL.md'
        if (-not (Test-Path -LiteralPath $skillTargetFile -PathType Leaf)) {
            if ($ValidateOnly) {
                throw "Reusable profile '$($worker.Name)' is missing bundled skill '$($worker.Skill)'."
            }
        }
        elseif ((Get-Sha256Hash -Path $skillSource) -cne (Get-Sha256Hash -Path $skillTargetFile)) {
            throw "Reusable profile '$($worker.Name)' has a different bundled skill '$($worker.Skill)'."
        }
    }
}

Invoke-CheckedCommand -Command 'gh' -Arguments @('auth', 'status', '--hostname', $repositoryHost, '--active')
Invoke-CheckedCommand -Command 'gh' -Arguments @('api', '--hostname', $repositoryHost, "repos/$Repository", '--jq', '.full_name')

if ($ValidateOnly) {
    Write-Host 'Setup validation passed. No files or profiles changed.'
    Write-Host "Hermes root: $resolvedHermesRoot"
    Write-Host "Source profile: $SourceProfile"
    exit 0
}

$originalHermesHome = $env:HERMES_HOME
$allWorkerProfilesReady = $true
$pluginInstalled = $false
try {
    $env:HERMES_HOME = $resolvedHermesRoot

    foreach ($worker in $workers) {
        $profilePath = Get-ProfilePath -Root $resolvedHermesRoot -Profile $worker.Name
        $installSkill = $false
        if (-not (Test-Path -LiteralPath $profilePath -PathType Container)) {
            if ($PSCmdlet.ShouldProcess($worker.Name, "Create Hermes worker profile from '$SourceProfile' and install its bundled skill")) {
                Invoke-CheckedCommand -Command 'hermes' -Arguments @(
                    'profile', 'create', $worker.Name,
                    '--clone-from', $SourceProfile,
                    '--no-alias',
                    '--description', $worker.Description
                )
                if (-not (Test-Path -LiteralPath $profilePath -PathType Container)) {
                    throw "Hermes reported success but profile '$($worker.Name)' was not created."
                }
                $installSkill = $true
            }
            else {
                $allWorkerProfilesReady = $false
            }
        }
        else {
            $skillTargetDirectory = Join-Path (Join-Path $profilePath 'skills') $worker.Skill
            $skillTargetFile = Join-Path $skillTargetDirectory 'SKILL.md'
            if (-not (Test-Path -LiteralPath $skillTargetFile -PathType Leaf)) {
                if ($PSCmdlet.ShouldProcess($skillTargetDirectory, "Install skill '$($worker.Skill)'")) {
                    $installSkill = $true
                }
                else {
                    $allWorkerProfilesReady = $false
                }
            }
        }

        $skillSourceDirectory = Join-Path (Join-Path $repositoryRoot 'skills') $worker.Skill
        $skillTargetDirectory = Join-Path (Join-Path $profilePath 'skills') $worker.Skill
        if ($installSkill) {
            New-Item -ItemType Directory -Path $skillTargetDirectory -Force | Out-Null
            Copy-Item -LiteralPath (Join-Path $skillSourceDirectory 'SKILL.md') -Destination (Join-Path $skillTargetDirectory 'SKILL.md') -Force
        }
    }

    if ($allWorkerProfilesReady) {
        $installAction = 'Install the Hermes plugin backend in the disabled state'
        if ($PSCmdlet.ShouldProcess($Repository, $installAction)) {
            Invoke-CheckedCommand -Command 'hermes' -Arguments @('plugins', 'install', $Repository, '--no-enable')
            $pluginInstalled = $true
        }
    }
    else {
        Write-Warning 'Setup is incomplete. Create all three worker profiles before you install the plugin.'
    }
}
finally {
    $env:HERMES_HOME = $originalHermesHome
}

Write-Host ''
if (-not $pluginInstalled) {
    Write-Host 'PR Autopilot setup is incomplete. The plugin was not installed.'
}
else {
    Write-Host 'PR Autopilot setup is complete.'
    Write-Host 'The backend is disabled. Review the safety policy before you enable it.'
    Write-Host 'Verify that any existing controller is paused before you manually enable the backend or restart Hermes.'
    Write-Host 'When ready, run: hermes plugins enable pr-autopilot'
}
Write-Host 'In Hermes Desktop, open Settings > Plugins and enable the PR Autopilot dashboard.'
Write-Host 'Open PR Autopilot from the Desktop sidebar. Review repository scope, then resume the controller.'
