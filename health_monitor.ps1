$ErrorActionPreference = 'SilentlyContinue'
$root = 'C:\Users\danil\Downloads\Telegram Desktop\orbit'
$hb = Join-Path $root 'scanner_heartbeat.txt'
$slog = Join-Path $root 'logs\scanner.stdout.log'
$status = Join-Path $root 'monitor_status.log'
$flag = Join-Path $root 'monitor_STOP.flag'
$maxIter = 120
$hbStale = 150
$scannerMissStreak = 0
$restarts = @()           # timestamps of detected scanner PID changes
$lastScannerPid = $null
$lastLogLen = (Get-Item $slog).Length
if (-not $lastLogLen) { $lastLogLen = 0 }

function Get-OrbitPid($needle) {
  $p = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='py.exe'" |
       Where-Object { $_.CommandLine -like '*\orbit\*' -and $_.CommandLine -like "*$needle*" } |
       Select-Object -First 1
  if ($p) { return $p.ProcessId } else { return $null }
}

function Stop-Bot($reason) {
  $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
  "STOP @ $ts :: $reason" | Out-File -FilePath $flag -Encoding utf8
  foreach ($needle in @('watchdog','ws_scanner','dashboard')) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='py.exe'" |
      Where-Object { $_.CommandLine -like '*\orbit\*' -and $_.CommandLine -like "*$needle*" } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  }
  "[$ts] *** BOT STOPPED *** $reason" | Out-File -FilePath $status -Append -Encoding utf8
  Write-Output "BOT STOPPED: $reason"
}

for ($i = 0; $i -lt $maxIter; $i++) {
  $now = Get-Date
  $ts = $now.ToString('HH:mm:ss')
  $wpid = Get-OrbitPid 'watchdog'
  $spid = Get-OrbitPid 'ws_scanner'
  $dpid = Get-OrbitPid 'dashboard'

  # heartbeat age
  $hbAge = 99999
  if (Test-Path $hb) { $hbAge = [int]((Get-Date) - (Get-Item $hb).LastWriteTime).TotalSeconds }

  # restart tracking
  if ($spid -and $lastScannerPid -and ($spid -ne $lastScannerPid)) { $restarts += $now }
  if ($spid) { $lastScannerPid = $spid }
  $restarts = $restarts | Where-Object { ($now - $_).TotalSeconds -le 600 }

  # scan newly appended log for hedge failures
  $hedgeFail = $null
  try {
    $len = (Get-Item $slog).Length
    if ($len -gt $lastLogLen) {
      $fs = [System.IO.File]::Open($slog,'Open','Read','ReadWrite')
      $fs.Seek($lastLogLen,'Begin') | Out-Null
      $sr = New-Object System.IO.StreamReader($fs)
      $chunk = $sr.ReadToEnd(); $sr.Close(); $fs.Close()
      $lastLogLen = $len
      if ($chunk -match 'HEDGE LIVE\].*FAILED' -or $chunk -match 'UNDER-HEDGED' -or $chunk -match 'short still OPEN') {
        $hedgeFail = ($chunk -split "`n" | Where-Object { $_ -match 'FAILED|UNDER-HEDGED|short still OPEN' } | Select-Object -Last 1)
      }
    } elseif ($len -lt $lastLogLen) { $lastLogLen = $len }
  } catch {}

  "[$ts] wd=$wpid sc=$spid db=$dpid hbAge=${hbAge}s restarts10m=$($restarts.Count)" |
    Out-File -FilePath $status -Append -Encoding utf8

  # ---- anomaly checks ----
  if (-not $wpid) { Stop-Bot "watchdog process gone (cannot self-heal)"; break }
  if ($hedgeFail) { Stop-Bot "hedge failure in log: $hedgeFail"; break }
  if (-not $spid) {
    $scannerMissStreak++
    if ($scannerMissStreak -ge 2 -and $hbAge -gt $hbStale) { Stop-Bot "scanner down ${scannerMissStreak}x + heartbeat stale ${hbAge}s"; break }
  } else { $scannerMissStreak = 0 }
  if ($spid -and $hbAge -gt $hbStale) { Stop-Bot "scanner hung: heartbeat stale ${hbAge}s"; break }
  if ($restarts.Count -ge 4) { Stop-Bot "crash loop: $($restarts.Count) restarts in 10min"; break }

  Start-Sleep -Seconds 60
}
"[$((Get-Date).ToString('HH:mm:ss'))] monitor loop ended (iter done or stopped)" |
  Out-File -FilePath $status -Append -Encoding utf8
Write-Output "monitor finished"
