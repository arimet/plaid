@echo off
rem Plaid launcher for Windows: double-click it. It needs Docker Desktop and opens the assistant in your browser.
chcp 65001 >nul
if "%PLAID_IMAGE%"=="" set "PLAID_IMAGE=ghcr.io/arimet/plaid:latest"
set "DD=%ProgramFiles%\Docker\Docker\Docker Desktop.exe"

if not exist "%DD%" (
  where docker >nul 2>nul || (
    echo Docker Desktop is missing. Install it, then this window carries on by itself.
    start "" "https://github.com/arimet/plaid#install-docker-desktop"
  )
)
:waitdocker
docker info >nul 2>nul && goto ready
if exist "%DD%" start "" "%DD%"
timeout /t 5 /nobreak >nul
goto waitdocker

:ready
rem Start Docker Desktop at sign-in, or the apps stop when the PC restarts (best effort).
powershell -NoProfile -Command "$f = Join-Path $env:APPDATA 'Docker\settings-store.json'; try { $j = Get-Content $f -Raw | ConvertFrom-Json; $j | Add-Member -Force AutoStart $true; $j | ConvertTo-Json -Depth 20 | Set-Content $f -Encoding UTF8 } catch {}"

docker container inspect arr-installer >nul 2>nul && (
  docker start arr-installer >nul
) || (
  docker run -d --name arr-installer --restart unless-stopped -p 7000:7000 -v /var/run/docker.sock:/var/run/docker.sock -v arr-installer-state:/state -e "HOST_HOME=%USERPROFILE%" -e HOST_PLATFORM=windows -e HOST_ADDR=localhost -e PUID=1000 -e PGID=1000 --label io.arr-installer=1 "%PLAID_IMAGE%" >nul
)
:waitpanel
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://localhost:7000/api/ping | Out-Null; exit 0 } catch { exit 1 }" && goto open
timeout /t 1 /nobreak >nul
goto waitpanel
:open
start "" http://localhost:7000
echo Plaid is open in your browser: http://localhost:7000. You can close this window.
