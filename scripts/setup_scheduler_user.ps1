# setup_scheduler_user.ps1
# Registra tareas SGT Trading para el usuario actual — SIN necesitar admin.
# Usar cuando setup_scheduler.ps1 falla por permisos UAC.
#
#   cd C:\Users\alejandro.fernandez\sgt_trading
#   .\scripts\setup_scheduler_user.ps1

$ErrorActionPreference = "Stop"

$ProjectRoot = "C:\Users\alejandro.fernandez\sgt_trading"
$PythonExe   = (Get-Command py -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    $PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $PythonExe) {
    Write-Error "Python no encontrado."
    exit 1
}

# Principal = usuario actual, sesion interactiva, sin elevar
$principal = New-ScheduledTaskPrincipal `
    -UserId    "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel  Limited

Write-Host "Usuario : $env:USERDOMAIN\$env:USERNAME"
Write-Host "Python  : $PythonExe"
Write-Host "Proyecto: $ProjectRoot"
Write-Host ""

function Register-SGTTask {
    param(
        [string]$Name,
        [string]$Script,
        [string]$TriggerDesc,
        [object]$Trigger,
        [string]$Description
    )
    $action   = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument "scripts\$Script" `
        -WorkingDirectory $ProjectRoot

    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit    (New-TimeSpan -Hours 2) `
        -StartWhenAvailable `
        -RunOnlyIfNetworkAvailable

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "  [reemplazado]" -ForegroundColor Yellow
    }

    Register-ScheduledTask `
        -TaskName    $Name `
        -Action      $action `
        -Trigger     $Trigger `
        -Settings    $settings `
        -Principal   $principal `
        -Description $Description | Out-Null

    Write-Host "OK  $Name  ($TriggerDesc)" -ForegroundColor Green
}

# ── 1. Morning Pipeline — L-V 07:00 ──────────────────────────────────────────
Write-Host "[1/5] Morning Pipeline..."
$t1 = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "07:00"
Register-SGTTask -Name "SGT_Morning_Pipeline" -Script "run_morning.py" `
    -TriggerDesc "L-V 07:00" -Trigger $t1 `
    -Description "SGT: pipeline diario + score_today"

# ── 2. Intraday Loop — L-V 09:25 ─────────────────────────────────────────────
Write-Host "[2/5] Intraday Loop..."
$t2 = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:25"
Register-SGTTask -Name "SGT_Intraday_Loop" -Script "run_intraday.py" `
    -TriggerDesc "L-V 09:25" -Trigger $t2 `
    -Description "SGT: refresh barras 1m/5m/30m cada 30s sesion NY Sugar"

# ── 3. Weekly COT — viernes 21:35 ────────────────────────────────────────────
Write-Host "[3/5] Weekly COT..."
$t3 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At "21:35"
Register-SGTTask -Name "SGT_Weekly_COT" -Script "run_weekly_cot.py" `
    -TriggerDesc "viernes 21:35" -Trigger $t3 `
    -Description "SGT: ingesta COT CFTC + resumen senal nivel x velocidad"

# ── 4. Weekly NDVI — lunes 06:30 ─────────────────────────────────────────────
Write-Host "[4/5] Weekly NDVI..."
$t4 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "06:30"
Register-SGTTask -Name "SGT_Weekly_NDVI" -Script "run_weekly.py" `
    -TriggerDesc "lunes 06:30" -Trigger $t4 `
    -Description "SGT: NDVI Sentinel-2 GEE + crop metrics"

# ── 5. Monthly — dias 1 y 10, 07:00 ──────────────────────────────────────────
Write-Host "[5/5] Monthly Pipeline..."
$t5a = New-ScheduledTaskTrigger -Monthly -DaysOfMonth 1  -At "07:00"
$t5b = New-ScheduledTaskTrigger -Monthly -DaysOfMonth 10 -At "07:00"

$action5   = New-ScheduledTaskAction `
    -Execute $PythonExe -Argument "scripts\run_monthly.py" `
    -WorkingDirectory $ProjectRoot
$settings5 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable -RunOnlyIfNetworkAvailable

if (Get-ScheduledTask -TaskName "SGT_Monthly_Pipeline" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "SGT_Monthly_Pipeline" -Confirm:$false
}
Register-ScheduledTask `
    -TaskName    "SGT_Monthly_Pipeline" `
    -Action      $action5 `
    -Trigger     @($t5a, $t5b) `
    -Settings    $settings5 `
    -Principal   $principal `
    -Description "SGT: ONI/Comex/CONAB (dia 1) + USDA/MAPA (dia 10)" | Out-Null
Write-Host "OK  SGT_Monthly_Pipeline  (dia 1 + dia 10, 07:00)" -ForegroundColor Green

# ── Resumen ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "TAREAS REGISTRADAS:" -ForegroundColor Cyan
Get-ScheduledTask | Where-Object { $_.TaskName -like "SGT_*" } |
    Format-Table TaskName, State -AutoSize
Write-Host "Logs en: $ProjectRoot\logs\" -ForegroundColor Cyan
Write-Host "NOTA: la sesion debe estar abierta (LogonType Interactive)."
Write-Host "      'StartWhenAvailable' lanza la tarea si se perdio la hora."
