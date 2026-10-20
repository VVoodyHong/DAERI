[CmdletBinding()]
param(
    [switch]$Prepare,
    [switch]$Check,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
$DaeriRoot = Split-Path -Parent $PSScriptRoot
$DaeriExitCode = 1
if (($Prepare -and $Check) -or ($Help -and ($Prepare -or $Check))) {
    [Console]::Error.WriteLine('Usage: setup.ps1 [-Check|-Prepare]')
    exit 2
}
if ($Help) {
    Write-Output 'Usage: setup.ps1 [-Check|-Prepare]'
    Write-Output 'Default: local check only. -Prepare may install user-local uv, update user PATH, and download Python and locked dependencies.'
    exit 0
}

$DaeriPolicy = @{}
Get-Content -LiteralPath (Join-Path $PSScriptRoot 'dependencies.env') | ForEach-Object {
    if ($_ -match '^([A-Z][A-Z0-9_]*)=(.+)$') { $DaeriPolicy[$Matches[1]] = $Matches[2] }
}
function Test-DaeriUv([string]$Program) {
    if (-not $Program -or -not (Test-Path -LiteralPath $Program -PathType Leaf)) { return $false }
    try {
        $Output = & $Program --version 2>$null
        if ($LASTEXITCODE -ne 0 -or $Output -notmatch '^uv (\d+\.\d+\.\d+)(\s|$)') { return $false }
        $Version = [version]$Matches[1]
        return $Version.Major -eq 0 -and $Version -ge [version]$DaeriPolicy.UV_MIN_VERSION
    }
    catch { return $false }
}
function Get-DaeriUv {
    $Existing = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
    if ($Existing -and (Test-DaeriUv $Existing.Source)) { return $Existing.Source }
    $Tools = Join-Path $env:LOCALAPPDATA "DAERI/tools/uv/$($DaeriPolicy.UV_INSTALL_VERSION)"
    $Program = Join-Path $Tools 'uv.exe'
    if (-not (Test-DaeriUv $Program)) {
        try { $Architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() }
        catch { $Architecture = $env:PROCESSOR_ARCHITECTURE }
        switch ($Architecture.ToUpperInvariant()) {
            'ARM64' { $Target = 'aarch64-pc-windows-msvc'; $ExpectedHash = $DaeriPolicy.UV_WINDOWS_ARM64_SHA256 }
            { $_ -in 'X64', 'AMD64' } { $Target = 'x86_64-pc-windows-msvc'; $ExpectedHash = $DaeriPolicy.UV_WINDOWS_X64_SHA256 }
            default { throw 'DAERI preparation is incomplete: unsupported Windows CPU.' }
        }
        $script:DaeriDownload = Join-Path ([IO.Path]::GetTempPath()) ('daeri-uv-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $script:DaeriDownload | Out-Null
        $Archive = Join-Path $script:DaeriDownload 'uv.zip'
        $Url = "$($DaeriPolicy.UV_RELEASE_BASE)/$($DaeriPolicy.UV_INSTALL_VERSION)/uv-$Target.zip"
        [Console]::Error.WriteLine("Preparing uv $($DaeriPolicy.UV_INSTALL_VERSION) from the pinned official release in the user tools directory.")
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Url -OutFile $Archive -UseBasicParsing -TimeoutSec 120 | Out-Null
        if ((Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ExpectedHash) {
            throw 'DAERI preparation is incomplete: uv archive checksum mismatch.'
        }
        Expand-Archive -LiteralPath $Archive -DestinationPath (Join-Path $script:DaeriDownload 'unpacked')
        $Source = Join-Path $script:DaeriDownload 'unpacked/uv.exe'
        if (-not (Test-DaeriUv $Source)) { throw 'DAERI preparation is incomplete: downloaded uv cannot run.' }
        New-Item -ItemType Directory -Path $Tools -Force | Out-Null
        Copy-Item -LiteralPath $Source -Destination (Join-Path $Tools 'uv.new.exe')
        Move-Item -LiteralPath (Join-Path $Tools 'uv.new.exe') -Destination $Program -Force
    }
    $UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($Tools -notin ($UserPath -split ';')) {
        [Environment]::SetEnvironmentVariable('Path', "$Tools;$UserPath", 'User')
    }
    $env:PATH = "$Tools;$env:PATH"
    [Console]::Error.WriteLine('User-local uv is ready. Restart the host so it receives the updated PATH; then reconnect the DAERI MCP server.')
    return $Program
}

$DaeriCandidate = $null
$DaeriDownload = $null
$DaeriLocked = $false
$DaeriLock = Join-Path $DaeriRoot '.daeri-setup.lock'
$DaeriEnvironment = Join-Path $DaeriRoot '.venv'
$DaeriPrevious = Join-Path $DaeriRoot '.venv.previous'
$PreviousEnvironment = $env:UV_PROJECT_ENVIRONMENT
Push-Location $DaeriRoot
try {
    if ($Prepare) {
        try { New-Item -ItemType Directory -Path $DaeriLock | Out-Null; $DaeriLocked = $true }
        catch { throw 'DAERI preparation is already running or was interrupted. Check that no setup process is active before removing .daeri-setup.lock and retrying.' }
        if ((Test-Path -LiteralPath $DaeriPrevious) -and -not (Test-Path -LiteralPath $DaeriEnvironment)) {
            Move-Item -LiteralPath $DaeriPrevious -Destination $DaeriEnvironment
        }
        if (Test-Path -LiteralPath $DaeriPrevious) { throw 'DAERI preparation is incomplete: inspect .venv.previous before retrying.' }
        $DaeriUv = Get-DaeriUv
        $DaeriCandidate = Join-Path $DaeriRoot ('.venv.prepare.' + [Guid]::NewGuid().ToString('N'))
        & $DaeriUv venv --relocatable --project $DaeriRoot $DaeriCandidate
        if ($LASTEXITCODE -ne 0) { $DaeriExitCode = $LASTEXITCODE; throw 'DAERI preparation is incomplete: candidate Python preparation failed.' }
        $env:UV_PROJECT_ENVIRONMENT = $DaeriCandidate
        & $DaeriUv sync --frozen --no-dev --no-editable --project $DaeriRoot
        if ($LASTEXITCODE -ne 0) { $DaeriExitCode = $LASTEXITCODE; throw 'DAERI preparation is incomplete: locked environment synchronization failed.' }
        $CandidatePython = Join-Path $DaeriCandidate 'Scripts/python.exe'
        & $CandidatePython -m daeri.cli check
        if ($LASTEXITCODE -ne 0) { $DaeriExitCode = $LASTEXITCODE; throw 'DAERI preparation is incomplete: candidate runtime check failed.' }
        if (Test-Path -LiteralPath $DaeriEnvironment) { Move-Item -LiteralPath $DaeriEnvironment -Destination $DaeriPrevious }
        try { Move-Item -LiteralPath $DaeriCandidate -Destination $DaeriEnvironment; $DaeriCandidate = $null }
        catch {
            if (Test-Path -LiteralPath $DaeriPrevious) { Move-Item -LiteralPath $DaeriPrevious -Destination $DaeriEnvironment }
            throw 'DAERI preparation is incomplete: environment activation failed; the previous environment was restored.'
        }
        if (Test-Path -LiteralPath $DaeriPrevious) { Remove-Item -LiteralPath $DaeriPrevious -Recurse -Force }
        $DaeriExitCode = 0
    }
    else {
        $DaeriCheckUv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
        if (-not $DaeriCheckUv -or -not (Test-DaeriUv $DaeriCheckUv.Source)) {
            throw 'DAERI MCP launch tool uv is missing or unsupported in this host PATH. Run setup.ps1 -Prepare, then restart the host and check its PATH.'
        }
        $DaeriPython = Join-Path $DaeriEnvironment 'Scripts/python.exe'
        if (-not (Test-Path -LiteralPath $DaeriPython -PathType Leaf)) {
            throw 'DAERI environment is not prepared. Run setup.ps1 -Prepare.'
        }
        & $DaeriPython -m daeri.cli check
        $DaeriExitCode = $LASTEXITCODE
    }
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
}
finally {
    $env:UV_PROJECT_ENVIRONMENT = $PreviousEnvironment
    if ($DaeriCandidate -and (Test-Path -LiteralPath $DaeriCandidate)) { Remove-Item -LiteralPath $DaeriCandidate -Recurse -Force }
    if ($DaeriDownload -and (Test-Path -LiteralPath $DaeriDownload)) { Remove-Item -LiteralPath $DaeriDownload -Recurse -Force }
    if ($DaeriLocked) { Remove-Item -LiteralPath $DaeriLock -Force }
    Pop-Location
}
exit $DaeriExitCode
