[CmdletBinding()]
param(
    [switch]$Prepare,
    [switch]$PrepareLocalEmbedding,
    [switch]$Check,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
$DaeriRoot = Split-Path -Parent $PSScriptRoot
$DaeriExitCode = 1
if ((@($Prepare, $PrepareLocalEmbedding, $Check, $Help) | Where-Object { $_ }).Count -gt 1) {
    [Console]::Error.WriteLine('Usage: setup.ps1 [-Check|-Prepare|-PrepareLocalEmbedding]')
    exit 2
}
if ($Help) {
    Write-Output 'Usage: setup.ps1 [-Check|-Prepare|-PrepareLocalEmbedding]'
    Write-Output 'Default: local check only. -Prepare downloads private uv, managed Python, and locked base dependencies. -PrepareLocalEmbedding also includes the local embedding runtime. Preparation uses DAERI-private runtime and cache locations.'
    exit 0
}

$DaeriEnvironmentFilter = '^(UV_|_?PYTHON|CONDA_)|^(VIRTUAL_ENV|VIRTUAL_ENV_PROMPT|__PYVENV_LAUNCHER__|_OLD_VIRTUAL_PATH|_OLD_VIRTUAL_PYTHONHOME|PYLAUNCHER_DEBUG)$'
$DaeriOriginalEnvironment = @{}
Get-ChildItem Env: | Where-Object { $_.Name -match $DaeriEnvironmentFilter } | ForEach-Object {
    $DaeriOriginalEnvironment[$_.Name] = $_.Value
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
        return $Matches[1] -eq $DaeriPolicy.UV_INSTALL_VERSION
    }
    catch { return $false }
}
function Get-DaeriUv {
    $Tools = Join-Path $DaeriRoot '.runtime'
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
        [Console]::Error.WriteLine("Preparing private uv $($DaeriPolicy.UV_INSTALL_VERSION) from the pinned official release.")
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
    return $Program
}

$DaeriCandidate = $null
$DaeriDownload = $null
$DaeriLocked = $false
$DaeriLock = Join-Path $DaeriRoot '.daeri-setup.lock'
$DaeriEnvironment = Join-Path $DaeriRoot '.venv'
$DaeriPrevious = Join-Path $DaeriRoot '.venv.previous'
Push-Location $DaeriRoot
try {
    Get-ChildItem Env: | Where-Object { $_.Name -match $DaeriEnvironmentFilter } | ForEach-Object {
        Remove-Item -LiteralPath ('Env:' + $_.Name)
    }
    if ($Prepare -or $PrepareLocalEmbedding) {
        if ($env:DAERI_DATA_DIR) {
            $DaeriData = $env:DAERI_DATA_DIR
            if ($env:USERPROFILE) { $DaeriHome = $env:USERPROFILE } else { $DaeriHome = $HOME }
            if ($DaeriData -eq '~') { $DaeriData = $DaeriHome }
            elseif ($DaeriData -match '^~[\\/]') { $DaeriData = Join-Path $DaeriHome $DaeriData.Substring(2) }
            if ($DaeriData -notmatch '^[A-Za-z]:[\\/]' -and $DaeriData -notmatch '^[\\/]{2}[^\\/]+[\\/][^\\/]+') {
                $DaeriExitCode = 2
                throw 'DAERI_DATA_DIR must be an absolute path; current-user ~/ paths are supported.'
            }
        }
        elseif ($env:LOCALAPPDATA) { $DaeriData = Join-Path $env:LOCALAPPDATA 'DAERI' }
        else { $DaeriData = Join-Path $env:USERPROFILE 'AppData/Local/DAERI' }
        $DaeriRuntime = [IO.Path]::GetFullPath((Join-Path $DaeriData 'runtime'))
        try { New-Item -ItemType Directory -Path $DaeriLock | Out-Null; $DaeriLocked = $true }
        catch { throw 'DAERI preparation is already running or was interrupted. Check that no setup process is active before removing .daeri-setup.lock and retrying.' }
        if ((Test-Path -LiteralPath $DaeriPrevious) -and -not (Test-Path -LiteralPath $DaeriEnvironment)) {
            Move-Item -LiteralPath $DaeriPrevious -Destination $DaeriEnvironment
        }
        if (Test-Path -LiteralPath $DaeriPrevious) { throw 'DAERI preparation is incomplete: inspect .venv.previous before retrying.' }
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $DaeriRuntime 'python'
        $env:UV_CACHE_DIR = Join-Path $DaeriRuntime 'cache'
        $env:UV_PYTHON_INSTALL_BIN = '0'
        $env:UV_PYTHON_INSTALL_REGISTRY = '0'
        New-Item -ItemType Directory -Path $env:UV_PYTHON_INSTALL_DIR, $env:UV_CACHE_DIR -Force | Out-Null
        $DaeriUv = Get-DaeriUv
        $DaeriCandidate = Join-Path $DaeriRoot ('.venv.prepare.' + [Guid]::NewGuid().ToString('N'))
        & $DaeriUv venv --managed-python --no-config --relocatable --project $DaeriRoot $DaeriCandidate
        if ($LASTEXITCODE -ne 0) { $DaeriExitCode = $LASTEXITCODE; throw 'DAERI preparation is incomplete: candidate Python preparation failed.' }
        $env:UV_PROJECT_ENVIRONMENT = $DaeriCandidate
        $DaeriSyncArguments = @('sync', '--managed-python', '--no-config', '--frozen', '--no-dev', '--no-editable', '--project', $DaeriRoot)
        if ($PrepareLocalEmbedding) { $DaeriSyncArguments += @('--extra', 'local-embedding') }
        & $DaeriUv @DaeriSyncArguments
        if ($LASTEXITCODE -ne 0) { $DaeriExitCode = $LASTEXITCODE; throw 'DAERI preparation is incomplete: locked environment synchronization failed.' }
        $PortableDirectory = Join-Path $DaeriCandidate 'bin'
        New-Item -ItemType Directory -Path $PortableDirectory -Force | Out-Null
        $CandidatePython = Join-Path $PortableDirectory 'python.exe'
        Copy-Item -LiteralPath (Join-Path $DaeriCandidate 'Scripts/python.exe') -Destination $CandidatePython
        & $CandidatePython -I -c 'import pathlib,sys; assert pathlib.Path(sys.prefix).resolve() == pathlib.Path(sys.argv[1]).resolve()' $DaeriCandidate
        if ($LASTEXITCODE -ne 0) { $DaeriExitCode = $LASTEXITCODE; throw 'DAERI preparation is incomplete: portable Python prefix check failed.' }
        $DaeriCheckResult = & $CandidatePython -I -m daeri.cli check
        if ($LASTEXITCODE -ne 0) {
            $DaeriExitCode = $LASTEXITCODE
            [Console]::Error.WriteLine(($DaeriCheckResult -join [Environment]::NewLine))
            throw 'DAERI preparation is incomplete: candidate runtime check failed.'
        }
        if (Test-Path -LiteralPath $DaeriEnvironment) { Move-Item -LiteralPath $DaeriEnvironment -Destination $DaeriPrevious }
        try { Move-Item -LiteralPath $DaeriCandidate -Destination $DaeriEnvironment }
        catch {
            if (Test-Path -LiteralPath $DaeriPrevious) { Move-Item -LiteralPath $DaeriPrevious -Destination $DaeriEnvironment }
            throw 'DAERI preparation is incomplete: environment activation failed; the previous environment was restored.'
        }
        try {
            & (Join-Path $DaeriEnvironment 'bin/python.exe') -I (Join-Path $PSScriptRoot 'register_mcp.py')
            if ($LASTEXITCODE -ne 0) { throw 'DAERI preparation is incomplete: MCP registration failed.' }
        }
        catch {
            Move-Item -LiteralPath $DaeriEnvironment -Destination $DaeriCandidate
            if (Test-Path -LiteralPath $DaeriPrevious) { Move-Item -LiteralPath $DaeriPrevious -Destination $DaeriEnvironment }
            throw
        }
        $DaeriCandidate = $null
        if (Test-Path -LiteralPath $DaeriPrevious) { Remove-Item -LiteralPath $DaeriPrevious -Recurse -Force }
        Write-Output $DaeriCheckResult
        $DaeriExitCode = 0
    }
    else {
        $DaeriPython = Join-Path $DaeriEnvironment 'bin/python.exe'
        if (-not (Test-Path -LiteralPath $DaeriPython -PathType Leaf)) {
            throw 'DAERI environment is not prepared. Run setup.ps1 -Prepare.'
        }
        & $DaeriPython -I -m daeri.cli check
        $DaeriExitCode = $LASTEXITCODE
    }
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
}
finally {
    Get-ChildItem Env: | Where-Object { $_.Name -match $DaeriEnvironmentFilter } | ForEach-Object {
        Remove-Item -LiteralPath ('Env:' + $_.Name)
    }
    foreach ($Name in $DaeriOriginalEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($Name, $DaeriOriginalEnvironment[$Name], 'Process')
    }
    if ($DaeriCandidate -and (Test-Path -LiteralPath $DaeriCandidate)) { Remove-Item -LiteralPath $DaeriCandidate -Recurse -Force }
    if ($DaeriDownload -and (Test-Path -LiteralPath $DaeriDownload)) { Remove-Item -LiteralPath $DaeriDownload -Recurse -Force }
    if ($DaeriLocked) { Remove-Item -LiteralPath $DaeriLock -Force }
    Pop-Location
}
exit $DaeriExitCode
