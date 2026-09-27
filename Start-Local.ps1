param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$sessionRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$pythonPath = Join-Path $sessionRoot 'work\.venv\Scripts\python.exe'
$launcherPath = Join-Path $sessionRoot 'work\local_crm.py'
$postgresPath = Join-Path $sessionRoot 'work\postgres\pgsql\bin\postgres.exe'
$dataPath = Join-Path $sessionRoot 'work\pgdata'
if (!(Test-Path -LiteralPath $pythonPath) -or !(Test-Path -LiteralPath $launcherPath)) {
    throw 'The prepared local runtime is missing. Follow README.md for a standard installation.'
}
$portOpen = $false
$probe = [System.Net.Sockets.TcpClient]::new()
try { $probe.Connect('127.0.0.1', 55432); $portOpen = $true } catch { } finally { $probe.Dispose() }
if (!$portOpen) {
    Start-Process -FilePath $postgresPath -ArgumentList @('-D', "`"$dataPath`"", '-h', '127.0.0.1', '-p', '55432') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $sessionRoot 'work\postgres-local.stdout.log') -RedirectStandardError (Join-Path $sessionRoot 'work\postgres-local.stderr.log')
    Start-Sleep -Seconds 2
}
Write-Output "Local CRM: http://127.0.0.1:$Port"
Write-Output 'Demo credentials are in LOCAL_ACCESS.md. Verification emails appear in this terminal.'
Push-Location $sessionRoot
try { & $pythonPath $launcherPath runserver "127.0.0.1:$Port" --noreload } finally { Pop-Location }
