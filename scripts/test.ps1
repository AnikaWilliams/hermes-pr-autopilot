[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

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

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runId = [Guid]::NewGuid().ToString('N')
$outputRoot = Join-Path $repositoryRoot 'test-output'
$pytestRoot = Join-Path $outputRoot "pytest-$runId"
$doctorRoot = Join-Path $outputRoot "doctor-$runId"
$doctorPackageRoot = Join-Path $outputRoot "doctor-package-$runId"
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null

Push-Location $repositoryRoot
try {
    $originalHermesTestShims = $env:HERMES_TEST_SHIMS
    try {
        $env:HERMES_TEST_SHIMS = '1'
        Invoke-CheckedCommand -Command 'python' -Arguments @(
            '-m', 'pytest', 'tests', '-q', '-p', 'no:cacheprovider', '--basetemp', $pytestRoot
        )
    }
    finally {
        $env:HERMES_TEST_SHIMS = $originalHermesTestShims
    }
    Invoke-CheckedCommand -Command 'node' -Arguments @('--test', 'tests/desktop_plugin_contract.test.mjs')
    Invoke-CheckedCommand -Command 'python' -Arguments @(
        '-m', 'py_compile',
        '__init__.py',
        'plugin_worker_runtime.py',
        'pr_autopilot.py',
        'pr_reconciler.py',
        'standalone_controller.py',
        'standalone_runtime.py',
        'worker_entry.py',
        'dashboard/plugin_api.py'
    )
    Invoke-CheckedCommand -Command 'python' -Arguments @('scripts/secret_scan.py')

    if (Get-Command 'hermes' -ErrorAction SilentlyContinue) {
        New-Item -ItemType Directory -Path $doctorPackageRoot -Force | Out-Null
        $trackedFiles = & git ls-files
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not enumerate tracked files for Plugin Doctor.'
        }
        foreach ($relativePath in $trackedFiles) {
            $sourcePath = Join-Path $repositoryRoot $relativePath
            if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
                continue
            }
            $destinationPath = Join-Path $doctorPackageRoot $relativePath
            $destinationDirectory = Split-Path -Parent $destinationPath
            New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
            Copy-Item -LiteralPath $sourcePath -Destination $destinationPath -Force
        }
        $originalHermesHome = $env:HERMES_HOME
        try {
            $env:HERMES_HOME = $doctorRoot
            Invoke-CheckedCommand -Command 'hermes' -Arguments @(
                'plugins', 'doctor', $doctorPackageRoot, '--ci'
            )
        }
        finally {
            $env:HERMES_HOME = $originalHermesHome
        }
    }
    else {
        Write-Warning 'Hermes is not on PATH. Plugin Doctor was not run.'
    }
}
finally {
    Pop-Location
}

Write-Host 'All available release checks passed.'
