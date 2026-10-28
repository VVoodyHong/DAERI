$ErrorActionPreference = 'Stop'
$DaeriRoot = Split-Path -Parent $PSScriptRoot
$DaeriEnvironment = Join-Path $DaeriRoot '.venv'
$DaeriPython = Join-Path $DaeriEnvironment 'bin/python.exe'
$DaeriProcess = $null
$DaeriArguments = @($args)
$DaeriConnect = $DaeriArguments.Count -gt 0 -and $DaeriArguments[0] -eq '--connect'
if ($DaeriConnect) { $DaeriArguments = @($DaeriArguments | Select-Object -Skip 1) }
$DaeriOriginalData = $env:DAERI_DATA_DIR
$DaeriDataChanged = $false
$DaeriPrepared = $false

function Get-DaeriItem([string]$Path) {
    try { Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch [System.Management.Automation.ItemNotFoundException] { return $null }
}

try {
    $DaeriMarkers = @(
        (Get-DaeriItem (Join-Path $DaeriRoot '.daeri-setup.lock'))
        (Get-DaeriItem (Join-Path $DaeriRoot '.venv.previous'))
        (Get-ChildItem -LiteralPath $DaeriRoot -Force -Filter '.venv.prepare.*')
    ) | Where-Object { $null -ne $_ }
    if ($DaeriMarkers) { throw 'Interrupted setup' }
    $DaeriEnvironmentItem = Get-DaeriItem $DaeriEnvironment
    if ($null -eq $DaeriEnvironmentItem -and -not $DaeriConnect) {
        Write-Output '{"status":"setup_required","message":"DAERI \uc138\ud305\uc774 \ud544\uc694\ud569\ub2c8\ub2e4. setup\uc744 \uc2e4\ud589\ud574 \uc8fc\uc138\uc694."}'
        exit 0
    }
    $DaeriBin = Get-DaeriItem (Join-Path $DaeriEnvironment 'bin')
    if ($DaeriConnect -and (($DaeriEnvironmentItem -and
        ($DaeriEnvironmentItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) -or
        ($DaeriBin -and ($DaeriBin.Attributes -band [IO.FileAttributes]::ReparsePoint)))) {
        throw 'Invalid runtime path'
    }
    if ($DaeriConnect) {
        $CliData = $null
        for ($Index = 0; $Index -lt $DaeriArguments.Count; $Index++) {
            $Argument = [string]$DaeriArguments[$Index]
            if ($Argument -eq '--data-dir' -and $Index + 1 -lt $DaeriArguments.Count) {
                $Index++; $CliData = [string]$DaeriArguments[$Index]
            }
            elseif ($Argument.StartsWith('--data-dir=')) { $CliData = $Argument.Substring(11) }
            else { break }
        }
        if ($null -ne $CliData) {
            $UserHome = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
            if ($CliData -eq '~') { $CliData = $UserHome }
            elseif ($CliData -match '^~[\\/]') { $CliData = Join-Path $UserHome $CliData.Substring(2) }
            if (-not [IO.Path]::IsPathRooted($CliData)) { $CliData = Join-Path $DaeriRoot $CliData }
            $env:DAERI_DATA_DIR = [IO.Path]::GetFullPath($CliData)
            $DaeriDataChanged = $true
        }
        $Configuration = Get-DaeriItem (Join-Path $DaeriRoot '.mcp.json')
        if ($Configuration -and ($Configuration.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Invalid MCP configuration path'
        }
        if (-not $env:DAERI_DATA_DIR -and -not (Test-Path -LiteralPath $DaeriPython -PathType Leaf) -and
            $Configuration -and -not $Configuration.PSIsContainer) {
            try {
                $Pinned = (Get-Content -LiteralPath $Configuration.FullName -Raw | ConvertFrom-Json).mcpServers.runtime.env.DAERI_DATA_DIR
                if ($Pinned -is [string] -and ($Pinned -match '^[A-Za-z]:[\\/]' -or $Pinned -match '^[\\/]{2}[^\\/]+[\\/][^\\/]+')) {
                    $env:DAERI_DATA_DIR = $Pinned
                    $DaeriDataChanged = $true
                }
            }
            catch { } # An older or malformed configuration has no reusable data pin.
        }
    }
    if ($DaeriConnect -and -not (Test-Path -LiteralPath $DaeriPython -PathType Leaf)) {
        & (Join-Path $PSScriptRoot 'setup.ps1') -Prepare | ForEach-Object { [Console]::Error.WriteLine($_) }
        if ($LASTEXITCODE -ne 0) { throw 'Runtime preparation failed' }
        $DaeriPrepared = $true
        $DaeriEnvironmentItem = Get-DaeriItem $DaeriEnvironment
    }
    $DaeriBin = Get-DaeriItem (Join-Path $DaeriEnvironment 'bin')
    $DaeriExecutable = Get-DaeriItem $DaeriPython
    if (-not $DaeriEnvironmentItem.PSIsContainer -or
        ($DaeriEnvironmentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        $null -eq $DaeriBin -or -not $DaeriBin.PSIsContainer -or
        ($DaeriBin.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        $null -eq $DaeriExecutable -or $DaeriExecutable.PSIsContainer -or
        ($DaeriExecutable.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Invalid runtime path'
    }
    if ($DaeriConnect) {
        $StatusScript = Join-Path $PSScriptRoot 'runtime_status.py'
        $DaeriData = & $DaeriPython -I $StatusScript --data-dir @DaeriArguments
        if ($LASTEXITCODE -ne 0) { throw 'Storage selection failed' }
        $env:DAERI_DATA_DIR = $DaeriData
        $DaeriDataChanged = $true
        & $DaeriPython -I $StatusScript --check @DaeriArguments
        $StatusCode = $LASTEXITCODE
        if ($StatusCode -eq 10) {
            & (Join-Path $PSScriptRoot 'setup.ps1') -Prepare | ForEach-Object { [Console]::Error.WriteLine($_) }
            if ($LASTEXITCODE -ne 0) { throw 'Runtime preparation failed' }
            $DaeriPrepared = $true
            & $DaeriPython -I $StatusScript --check @DaeriArguments
            $StatusCode = $LASTEXITCODE
        }
        if ($StatusCode -eq 2) { exit 2 }
        if ($StatusCode -notin 0, 11) { throw 'Runtime inspection failed' }
        if ($StatusCode -eq 11) {
            & (Join-Path $PSScriptRoot 'setup.ps1') -PrepareLocalEmbedding | ForEach-Object { [Console]::Error.WriteLine($_) }
            if ($LASTEXITCODE -ne 0) { throw 'Local runtime preparation failed' }
            $DaeriPrepared = $true
            & $DaeriPython -I $StatusScript --check @DaeriArguments
            if ($LASTEXITCODE -ne 0) { throw 'Local runtime inspection failed' }
        }
        $OriginalPrepared = $env:DAERI_RUNTIME_PREPARED
        try {
            $env:DAERI_RUNTIME_PREPARED = [string][int]$DaeriPrepared
            $Connection = & $DaeriPython -I (Join-Path $PSScriptRoot 'register_mcp.py') --report
        }
        finally { [Environment]::SetEnvironmentVariable('DAERI_RUNTIME_PREPARED', $OriginalPrepared, 'Process') }
        if ($LASTEXITCODE -ne 0) { throw 'MCP registration failed' }
        [Console]::Error.WriteLine($Connection)
    }
    $DaeriStart = New-Object System.Diagnostics.ProcessStartInfo
    $DaeriStart.FileName = $DaeriPython
    $DaeriStart.WorkingDirectory = $DaeriRoot
    $DaeriStart.UseShellExecute = $false
    # Preserve empty arguments, JSON quotes and trailing slashes on Windows PowerShell.
    $DaeriQuotedArguments = foreach ($DaeriArgument in (@('-I', '-m', 'daeri.cli') + $DaeriArguments)) {
        '"' + ([regex]::Replace([string]$DaeriArgument, '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
    }
    $DaeriStart.Arguments = $DaeriQuotedArguments -join ' '
    @($DaeriStart.EnvironmentVariables.Keys) |
        Where-Object { $_ -match '^(UV_|_?PYTHON|CONDA_)|^(VIRTUAL_ENV|VIRTUAL_ENV_PROMPT|__PYVENV_LAUNCHER__|_OLD_VIRTUAL_PATH|_OLD_VIRTUAL_PYTHONHOME|PYLAUNCHER_DEBUG)$' } |
        ForEach-Object { $DaeriStart.EnvironmentVariables.Remove($_) }
    $DaeriProcess = New-Object System.Diagnostics.Process
    $DaeriProcess.StartInfo = $DaeriStart
    $DaeriProcess.Start() | Out-Null
    $DaeriProcess.WaitForExit()
    $DaeriExit = $DaeriProcess.ExitCode
}
catch {
    Write-Output '{"status":"error","data":null,"error":{"code":"RUNTIME_UNAVAILABLE","message":"DAERI \uc138\ud305 \uc0c1\ud0dc\ub97c \ud655\uc778\ud574 \uc8fc\uc138\uc694.","next_action":"setup"}}'
    $DaeriExit = 1
}
finally {
    if ($null -ne $DaeriProcess) { $DaeriProcess.Dispose() }
    if ($DaeriDataChanged) { [Environment]::SetEnvironmentVariable('DAERI_DATA_DIR', $DaeriOriginalData, 'Process') }
}
exit $DaeriExit
