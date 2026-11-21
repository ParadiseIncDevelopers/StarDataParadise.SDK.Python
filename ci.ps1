param(
    [Parameter(Mandatory)][string]$Wheelhouse,
    [Parameter(Mandatory)][string]$TestWheelhouse,
    [string]$Server, [string]$OpenSsl, [string]$FixtureHarness
)
$ErrorActionPreference = 'Stop'
$Wheelhouse = (Resolve-Path -LiteralPath $Wheelhouse).Path
$TestWheelhouse = (Resolve-Path -LiteralPath $TestWheelhouse).Path
$integration = @($Server, $OpenSsl, $FixtureHarness) | Where-Object { $_ }
if (@($integration).Count -notin 0,3) { throw 'Supply Server, OpenSsl and FixtureHarness together.' }
if ($FixtureHarness) {
    $Server = (Resolve-Path -LiteralPath $Server).Path
    $OpenSsl = (Resolve-Path -LiteralPath $OpenSsl).Path
    $FixtureHarness = (Resolve-Path -LiteralPath $FixtureHarness).Path
}
Push-Location $PSScriptRoot
try {
    $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
    $prefix = New-Item -ItemType Directory -Path ('out/v' + [Guid]::NewGuid().ToString('N').Substring(0,8)) -Force
    $contract = (Resolve-Path -LiteralPath src/stardataparadise_sdk/contracts/sdp-contract-v1.zip).Path
    if ((Get-FileHash -LiteralPath $contract -Algorithm SHA256).Hash -ne 'E6500900A3FA34910781EA92D5E41FE8716511FAC6F6D6C689FF9CCE15D578AB') {
        throw 'Pinned Server wire contract differs.'
    }
    $wheelDeps = @('--no-index','--find-links',$Wheelhouse,'--find-links',$TestWheelhouse)
    & python -m venv (Join-Path $prefix 'b')
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    $builder = Join-Path $prefix 'b/Scripts/python.exe'
    & $builder -m pip --disable-pip-version-check install @wheelDeps --require-hashes -r tests/requirements-build.txt
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    & $builder -B -m build --no-isolation --outdir out/packages .
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    $sdkVersion = & $builder -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])'
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    $wheel = (Resolve-Path -LiteralPath ("out/packages/stardataparadise_sdk-" + $sdkVersion + "-py3-none-any.whl")).Path
    $sdist = (Resolve-Path -LiteralPath ("out/packages/stardataparadise_sdk-" + $sdkVersion + ".tar.gz")).Path

    & python -m venv (Join-Path $prefix 'i')
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    $installed = Join-Path $prefix 'i/Scripts/python.exe'
    & $installed -m pip --disable-pip-version-check install @wheelDeps --require-hashes -r tests/requirements-build.txt
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    & $installed -m pip --disable-pip-version-check install --no-index --no-deps $wheel
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    & $installed -I -B -m pytest -q --import-mode=importlib -p no:cacheprovider tests/test_native.py
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    & $installed -I -B tests/package_scan.py $wheel $sdist
    if ($LASTEXITCODE) { exit $LASTEXITCODE }

    if ($FixtureHarness) {
        $accept = (Resolve-Path -LiteralPath tests/acceptance.py).Path
        & python -B $FixtureHarness --server $Server --openssl $OpenSsl --contract $contract --scratch out/integration --drop-op tx_commit --drop-read -- $installed -I -B $accept
    }
    else {
        & $installed -I -B -m pytest -q --import-mode=importlib -p no:cacheprovider tests/test_contract.py -k installed
    }
    if ($LASTEXITCODE) { exit $LASTEXITCODE }

    & $builder -m pip --disable-pip-version-check install @wheelDeps --no-build-isolation --no-deps $sdist
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    & $builder -I -B -m pytest -q --import-mode=importlib -p no:cacheprovider tests/test_contract.py -k installed
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}