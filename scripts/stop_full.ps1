$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "process_helpers.ps1")
$projectRoot = Split-Path -Parent $PSScriptRoot
$dockerPath = 'C:/Program Files/Docker/Docker/resources/bin'
if (Test-Path -LiteralPath $dockerPath) { $env:PATH = $dockerPath + [IO.Path]::PathSeparator + $env:PATH }
$processFile = Join-Path $projectRoot ".data\runtime\processes.json"
if (Test-Path -LiteralPath $processFile) {
    $records = @(Read-DevFlowProcessRecords -Path $processFile)
    foreach ($record in $records) {
        if (Test-DevFlowProcessRecord -Record $record) {
            Stop-DevFlowProcessTree -TaskProcessId $record.Pid
            Write-Host "Stopped $($record.Role)."
        }
    }
    Remove-Item -LiteralPath $processFile
}
Push-Location $projectRoot
try {
    & docker compose -p devflow-ai-local -f docker-compose.yml -f docker-compose.local.yml stop postgres etcd minio milvus rabbitmq
    if ($LASTEXITCODE -ne 0) { throw "Could not stop infrastructure services." }
} finally { Pop-Location }
Write-Host "研迹已停止，数据库和上传文件已保留。"
