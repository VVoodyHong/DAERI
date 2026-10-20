@echo off
setlocal
for /f "tokens=1 delims==" %%V in ('set UV_ 2^>nul') do set "%%V="
for /f "tokens=1 delims==" %%V in ('set PYTHON 2^>nul') do set "%%V="
for /f "tokens=1 delims==" %%V in ('set _PYTHON 2^>nul') do set "%%V="
for /f "tokens=1 delims==" %%V in ('set CONDA_ 2^>nul') do set "%%V="
set "VIRTUAL_ENV="
set "VIRTUAL_ENV_PROMPT="
set "__PYVENV_LAUNCHER__="
set "_OLD_VIRTUAL_PATH="
set "_OLD_VIRTUAL_PYTHONHOME="
set "PYLAUNCHER_DEBUG="
for %%I in ("%~dp0..") do set "DAERI_ROOT=%%~fI"
set "DAERI_PYTHON=%DAERI_ROOT%\.venv\bin\python.exe"
if not exist "%DAERI_PYTHON%" (
    >&2 echo DAERI environment is not prepared. Run: powershell -File "%DAERI_ROOT%\scripts\setup.ps1" -Prepare
    exit /b 1
)
pushd "%DAERI_ROOT%" || exit /b 1
"%DAERI_PYTHON%" -I -m daeri.server %*
set "DAERI_EXIT=%ERRORLEVEL%"
popd
exit /b %DAERI_EXIT%
