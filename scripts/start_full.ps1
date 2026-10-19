param([switch]$WorkerOnly)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "process_helpers.ps1")
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDependencies = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies'
foreach ($toolPath in @((Join-Path $runtimeDependencies 'node/bin'), (Join-Path $runtimeDependencies 'native/git/cmd'), 'C:/Program Files/Docker/Docker/resources/bin')) {
    if (Test-Path -LiteralPath $toolPath) { $env:PATH = $toolPath + [IO.Path]::PathSeparator + $env:PATH }
}
$backendDirectory = Join-Path $projectRoot "backend"
$frontendDirectory = Join-Path $projectRoot "frontend"
$pythonPath = Join-Path $backendDirectory ".venv\Scripts\python.exe"
$nodePath = if ($WorkerOnly) { $null } else { (Get-Command node -ErrorAction Stop).Source }
$nextPath = Join-Path $frontendDirectory "node_modules\next\dist\bin\next"
$runtimeDirectory = Join-Path $projectRoot ".data\runtime"
$processFile = Join-Path $runtimeDirectory "processes.json"
New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null

if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Missing backend Python environment." }
if (-not $WorkerOnly -and -not (Test-Path -LiteralPath (Join-Path $frontendDirectory ".next\BUILD_ID"))) {
    throw "Frontend build is missing. Run npm run build in frontend first."
}

function Test-Backend {
    try {
        # Windows PowerShell 5.1 guesses a legacy encoding for JSON without a
        # charset. Decode the actual UTF-8 bytes so Chinese app names match.
        $healthResponse = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        $response = [Text.Encoding]::UTF8.GetString($healthResponse.RawContentStream.ToArray()) | ConvertFrom-Json
        return $response.status -eq "ok" -and $response.app -eq "研迹"
    } catch { return $false }
}

function Test-Frontend {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:3001" -TimeoutSec 2
        return $response.StatusCode -eq 200 -and $response.Content.Contains("研迹")
    } catch { return $false }
}

$savedProcesses = @(Read-DevFlowProcessRecords -Path $processFile)
$newProcesses = @()
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:NEXT_TELEMETRY_DISABLED = "1"

Push-Location $projectRoot
try {
    Write-Host "Starting PostgreSQL, Milvus and RabbitMQ services..."
    & docker desktop start --timeout 60
    if ($LASTEXITCODE -ne 0) { throw "Docker Desktop could not start. Check its error dialog." }
    & docker compose -p devflow-ai-local -f docker-compose.yml -f docker-compose.local.yml up -d --wait --wait-timeout 180 postgres etcd minio milvus rabbitmq
    if ($LASTEXITCODE -ne 0) { throw "Infrastructure startup failed. Check Docker Desktop and compose logs." }

    if (-not $WorkerOnly -and -not (Test-Backend)) {
        $process = Start-Process -FilePath $pythonPath -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000") -WorkingDirectory $backendDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDirectory "backend.out.log") -RedirectStandardError (Join-Path $runtimeDirectory "backend.err.log") -PassThru
        $newProcesses += $process
        $savedProcesses = @($savedProcesses | Where-Object Role -ne "backend") + @([PSCustomObject]@{Role="backend"; Pid=$process.Id; StartedAt=$process.StartTime.ToUniversalTime().ToString("o")})
        ConvertTo-Json -InputObject @($savedProcesses) | Set-Content -LiteralPath $processFile -Encoding UTF8
        $ready = $false
        for ($attempt = 0; $attempt -lt 120; $attempt++) {
            if ($process.HasExited) { throw "Backend exited. Check .data/runtime/backend.err.log." }
            if (Test-Backend) { $ready = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $ready) { throw "Backend startup timed out. Check .data/runtime/backend.err.log." }
    }

    $workerRecord = @($savedProcesses | Where-Object Role -eq "worker") | Select-Object -First 1
    $workerAlive = $false
    if ($workerRecord) {
        $workerAlive = Test-DevFlowProcessRecord -Record $workerRecord
    }
    if (-not $workerAlive) {
        $workerInstance = [guid]::NewGuid().ToString()
        $env:DEVFLOW_WORKER_INSTANCE = $workerInstance
        $process = Start-Process -FilePath $pythonPath -ArgumentList @("-m", "app.workers.task_worker") -WorkingDirectory $backendDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDirectory "worker.out.log") -RedirectStandardError (Join-Path $runtimeDirectory "worker.err.log") -PassThru
        $newProcesses += $process
        $savedProcesses = @($savedProcesses | Where-Object Role -ne "worker") + @([PSCustomObject]@{Role="worker"; Pid=$process.Id; StartedAt=$process.StartTime.ToUniversalTime().ToString("o")})
        ConvertTo-Json -InputObject @($savedProcesses) | Set-Content -LiteralPath $processFile -Encoding UTF8
        $ready = $false
        for ($attempt = 0; $attempt -lt 90; $attempt++) {
            if ($process.HasExited) { throw "Worker exited. Check .data/runtime/worker.err.log." }
            $heartbeatPath = Join-Path $runtimeDirectory "worker-heartbeat.json"
            if (Test-Path -LiteralPath $heartbeatPath) {
                try {
                    $heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw | ConvertFrom-Json
                    if ($heartbeat.instance_id -eq $workerInstance -and $heartbeat.status -eq "ready") { $ready = $true; break }
                } catch { }
            }
            Start-Sleep -Milliseconds 500
        }
        if (-not $ready) { throw "Worker startup timed out. Check RabbitMQ and worker.err.log." }
    }

    if (-not $WorkerOnly -and -not (Test-Frontend)) {
        $quotedNextPath = '"' + $nextPath + '"'
        $process = Start-Process -FilePath $nodePath -ArgumentList @($quotedNextPath, "start", "--hostname", "127.0.0.1", "--port", "3001") -WorkingDirectory $frontendDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDirectory "frontend.out.log") -RedirectStandardError (Join-Path $runtimeDirectory "frontend.err.log") -PassThru
        $newProcesses += $process
        $savedProcesses = @($savedProcesses | Where-Object Role -ne "frontend") + @([PSCustomObject]@{Role="frontend"; Pid=$process.Id; StartedAt=$process.StartTime.ToUniversalTime().ToString("o")})
        ConvertTo-Json -InputObject @($savedProcesses) | Set-Content -LiteralPath $processFile -Encoding UTF8
        $ready = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            if ($process.HasExited) { throw "Frontend exited. Check .data/runtime/frontend.err.log." }
            if (Test-Frontend) { $ready = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $ready) { throw "Frontend startup timed out. Check .data/runtime/frontend.err.log." }
    }
    if ($WorkerOnly) { Write-Host "研迹 Worker is ready." } else {
        Write-Host "研迹 is ready."
        Write-Host "Frontend: http://127.0.0.1:3001"
        Write-Host "API docs: http://127.0.0.1:8000/docs"
    }
    Write-Host "RabbitMQ: http://127.0.0.1:15673"
    Write-Host "Logs: .data/runtime"
} catch {
    foreach ($process in $newProcesses) {
        if (-not $process.HasExited) { Stop-DevFlowProcessTree -TaskProcessId $process.Id }
    }
    throw
} finally { Pop-Location }
