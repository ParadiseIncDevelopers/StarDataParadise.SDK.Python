param([string]$Wheelhouse)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    & python -m venv out/venv
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    $python = Join-Path $PSScriptRoot 'out/venv/Scripts/python.exe'
    $sources = @()
    if ($Wheelhouse) {
        $sources = @('--no-index', '--find-links', $Wheelhouse)
    }

    & $python -m pip --disable-pip-version-check wheel --no-deps @sources -w out/packages .
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    & $python -m pip --disable-pip-version-check install --force-reinstall --no-index --no-deps out/packages/stardataparadise_sdk-0.1.0-py3-none-any.whl
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    & $python -I tests/smoke.py
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
