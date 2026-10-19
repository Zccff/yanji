param([switch]$StopOnly)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'process_helpers.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$processFile = Join-Path $projectRoot '.data/runtime/processes.json'
if (Test-Path -LiteralPath $processFile) {
    $records = @(Read-DevFlowProcessRecords -Path $processFile)
    foreach ($record in @($records | Where-Object Role -eq 'worker')) {
        if (Test-DevFlowProcessRecord -Record $record) {
            Stop-DevFlowProcessTree -TaskProcessId $record.Pid
        }
    }
}
if (-not $StopOnly) { & (Join-Path $PSScriptRoot 'start_full.ps1') -WorkerOnly }
