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
rem The PC's address on the local network: the links must open from the TV or the phone too.
set "ADDR="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | Select-Object -First 1).IPv4Address.IPAddress"`) do set "ADDR=%%i"
if not defined ADDR set "ADDR=localhost"
rem Start Docker Desktop at sign-in, or the apps stop when the PC restarts (best effort).
powershell -NoProfile -Command "$f = Join-Path $env:APPDATA 'Docker\settings-store.json'; try { $j = Get-Content $f -Raw | ConvertFrom-Json; $j | Add-Member -Force AutoStart $true; $j | ConvertTo-Json -Depth 20 | Set-Content $f -Encoding UTF8 } catch {}"

rem A newer Plaid when there is one (offline, the one here). An older Plaid container is replaced: its settings
rem live in the plaid-state volume, not in the container.
docker pull -q "%PLAID_IMAGE%" >nul 2>nul
set "RUNNING="
set "LATEST="
for /f "usebackq delims=" %%i in (`docker container inspect -f "{{.Image}}" plaid 2^>nul`) do set "RUNNING=%%i"
for /f "usebackq delims=" %%i in (`docker image inspect -f "{{.Id}}" "%PLAID_IMAGE%" 2^>nul`) do set "LATEST=%%i"
if defined RUNNING if not "%RUNNING%"=="%LATEST%" docker rm -f plaid >nul 2>nul
docker container inspect plaid >nul 2>nul && (
  docker start plaid >nul
) || (
  docker run -d --name plaid --restart unless-stopped -p 7575:7575 -v /var/run/docker.sock:/var/run/docker.sock -v plaid-state:/state -e "HOST_HOME=%USERPROFILE%" -e HOST_PLATFORM=windows -e "HOST_ADDR=%ADDR%" -e PUID=1000 -e PGID=1000 --label io.plaid=1 "%PLAID_IMAGE%" >nul || (
    docker rm -f plaid >nul 2>nul
    echo Plaid could not start: port 7575 is probably used by another program.
    pause
    exit /b 1
  )
)
:waitpanel
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://localhost:7575/api/ping | Out-Null; exit 0 } catch { exit 1 }" && goto open
timeout /t 1 /nobreak >nul
goto waitpanel
:open
start "" http://localhost:7575
echo Plaid is open in your browser: http://localhost:7575.
if not "%ADDR%"=="localhost" echo From another device on your network: http://%ADDR%:7575
echo You can close this window.
