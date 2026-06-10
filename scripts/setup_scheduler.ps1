# setup_scheduler.ps1
# Registra todas las tareas automaticas SGT Trading en Windows Task Scheduler.
# Ejecutar UNA VEZ como administrador:
#   Clic derecho PowerShell -> Ejecutar como administrador
#   cd C:\Users\alejandro.fernandez\sgt_trading
#   .\scripts\setup_scheduler.ps1

$ErrorActionPreference = "Stop"

# ── Configuracion ─────────────────────────────────────────────────────────────
$ProjectRoot = "C:\Users\alejandro.fernandez\sgt_trading"
$PythonExe   = (Get-Command py -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    $PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $PythonExe) {
    Write-Error "Python no encontrado. Instala Python o ajusta la ruta manualmente."
    exit 1
}

Write-Host "Python: $PythonExe"
Write-Host "Proyecto: $ProjectRoot"
Write-Host ""

# Helper: crear o reemplazar tarea
function Register-SGTTask {
    param(
        [string]$Name,
        [string]$Script,
        [string]$TriggerDesc,
        [object]$Trigger,
        [string]$Description
    )
    $action = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument "scripts\$Script" `
        -WorkingDirectory $ProjectRoot

    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -StartWhenAvailable `
        -RunOnlyIfNetworkAvailable

    # Eliminar si ya existe
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "  [reemplazado]" -ForegroundColor Yellow
    }

    Register-ScheduledTask `
        -TaskName    $Name `
        -Action      $action `
        -Trigger     $Trigger `
        -Settings    $settings `
        -Description $Description `
        -RunLevel    Limited | Out-Null

    Write-Host "OK  $Name  ($TriggerDesc)" -ForegroundColor Green
}

# ── 1. Morning Pipeline — L-V 07:00 ──────────────────────────────────────────
Write-Host "[1/5] Morning Pipeline (run_morning.py)..."
$t1 = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "07:00"
Register-SGTTask `
    -Name        "SGT_Morning_Pipeline" `
    -Script      "run_morning.py" `
    -TriggerDesc "L-V 07:00" `
    -Trigger     $t1 `
    -Description "SGT: pipeline diario + score_today (precios, CEPEA, Santos, ERA5...)"

# ── 2. Intraday Loop — L-V 09:25 ─────────────────────────────────────────────
Write-Host "[2/5] Intraday Loop (run_intraday.py)..."
$t2 = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "09:25"
Register-SGTTask `
    -Name        "SGT_Intraday_Loop" `
    -Script      "run_intraday.py" `
    -TriggerDesc "L-V 09:25 (loop auto hasta 19:15)" `
    -Trigger     $t2 `
    -Description "SGT: refresh barras 1m/5m/30m cada 30s durante sesion NY Sugar"

# ── 3. Weekly COT — viernes 21:35 ─────────────────────────────────────────────
Write-Host "[3/5] Weekly COT (run_weekly_cot.py)..."
$t3 = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Friday `
    -At "21:35"
Register-SGTTask `
    -Name        "SGT_Weekly_COT" `
    -Script      "run_weekly_cot.py" `
    -TriggerDesc "viernes 21:35" `
    -Trigger     $t3 `
    -Description "SGT: ingesta COT CFTC (pub. 21:30) + resumen señal nivel x velocidad"

# ── 4. Weekly NDVI — lunes 06:30 ─────────────────────────────────────────────
Write-Host "[4/5] Weekly NDVI/GEE (run_weekly.py)..."
$t4 = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday `
    -At "06:30"
Register-SGTTask `
    -Name        "SGT_Weekly_NDVI" `
    -Script      "run_weekly.py" `
    -TriggerDesc "lunes 06:30" `
    -Trigger     $t4 `
    -Description "SGT: NDVI Sentinel-2 GEE + crop metrics"

# ── 5. Monthly — días 1 y 10, 07:00 ──────────────────────────────────────────
Write-Host "[5/5] Monthly Pipeline (run_monthly.py)..."
# Dos triggers: día 1 y día 10
$t5a = New-ScheduledTaskTrigger -Monthly -DaysOfMonth 1  -At "07:00"
$t5b = New-ScheduledTaskTrigger -Monthly -DaysOfMonth 10 -At "07:00"

$action5 = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "scripts\run_monthly.py" `
    -WorkingDirectory $ProjectRoot
$settings5 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

if (Get-ScheduledTask -TaskName "SGT_Monthly_Pipeline" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "SGT_Monthly_Pipeline" -Confirm:$false
}
Register-ScheduledTask `
    -TaskName    "SGT_Monthly_Pipeline" `
    -Action      $action5 `
    -Trigger     @($t5a, $t5b) `
    -Settings    $settings5 `
    -Description "SGT: ONI/Comex/CONAB (dia 1) + USDA/MAPA (dia 10)" `
    -RunLevel    Limited | Out-Null
Write-Host "OK  SGT_Monthly_Pipeline  (dia 1 + dia 10, 07:00)" -ForegroundColor Green

# ── Resumen ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=" * 60
Write-Host "TAREAS REGISTRADAS:" -ForegroundColor Cyan
Get-ScheduledTask | Where-Object { $_.TaskName -like "SGT_*" } | `
    Format-Table TaskName, State -AutoSize
Write-Host ""
Write-Host "Logs en: $ProjectRoot\logs\" -ForegroundColor Cyan
Write-Host "Para verificar: Get-ScheduledTask | Where-Object { `$_.TaskName -like 'SGT_*' }"
Write-Host ""
Write-Host "NOTA: el PC debe estar encendido en los horarios programados."
Write-Host "      'StartWhenAvailable' lanza la tarea al arrancar si se perdio la hora."
