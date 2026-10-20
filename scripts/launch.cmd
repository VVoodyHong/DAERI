@echo off
setlocal
for %%I in ("%~dp0..") do set "DAERI_ROOT=%%~fI"
set "DAERI_PYTHON=%DAERI_ROOT%\.venv\Scripts\python.exe"
if not exist "%DAERI_PYTHON%" (
    >&2 echo DAERI environment is not prepared. Run: powershell -File "%DAERI_ROOT%\scripts\setup.ps1" -Prepare
    exit /b 1
)
pushd "%DAERI_ROOT%" || exit /b 1
"%DAERI_PYTHON%" -m daeri.server %*
set "DAERI_EXIT=%ERRORLEVEL%"
popd
exit /b %DAERI_EXIT%
