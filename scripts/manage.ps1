$ErrorActionPreference = 'Stop'
$DaeriRoot = Split-Path -Parent $PSScriptRoot
$DaeriEnvironment = Join-Path $DaeriRoot '.venv'
$DaeriPython = Join-Path $DaeriEnvironment 'bin/python.exe'
$DaeriProcess = $null

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
    if ($null -eq $DaeriEnvironmentItem) {
        Write-Output '{"status":"setup_required","message":"DAERI \uc138\ud305\uc774 \ud544\uc694\ud569\ub2c8\ub2e4. setup\uc744 \uc2e4\ud589\ud574 \uc8fc\uc138\uc694."}'
        exit 0
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
    $DaeriStart = New-Object System.Diagnostics.ProcessStartInfo
    $DaeriStart.FileName = $DaeriPython
    $DaeriStart.WorkingDirectory = $DaeriRoot
    $DaeriStart.UseShellExecute = $false
    # Preserve empty arguments, JSON quotes and trailing slashes on Windows PowerShell.
    $DaeriQuotedArguments = foreach ($DaeriArgument in (@('-I', '-m', 'daeri.cli') + $args)) {
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
}
exit $DaeriExit
