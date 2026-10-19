function Read-DevFlowProcessRecords {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        $records = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        foreach ($record in $records) { Write-Output $record }
    }
}

function Test-DevFlowProcessRecord {
    param($Record)
    if (-not $Record -or -not $Record.Pid -or -not $Record.StartedAt) { return $false }
    try {
        $process = Get-Process -Id $Record.Pid -ErrorAction Stop
        # PowerShell 7 parses JSON timestamps as DateTime; Windows PowerShell keeps strings.
        $expectedStart = [DateTimeOffset]$Record.StartedAt
        return $process.StartTime.ToUniversalTime().Ticks -eq $expectedStart.UtcDateTime.Ticks
    } catch { return $false }
}

function Stop-DevFlowProcessTree {
    param([int]$TaskProcessId)
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$TaskProcessId" -ErrorAction SilentlyContinue)
    foreach ($child in $children) {
        Stop-DevFlowProcessTree -TaskProcessId $child.ProcessId
    }
    Stop-Process -Id $TaskProcessId -ErrorAction SilentlyContinue
}
