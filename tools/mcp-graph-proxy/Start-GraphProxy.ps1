# Запуск токен-прокси графа УБД. Docker, WSL и уже открытые порты не трогает.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $pair = $line.Split("=", 2)
        if ($pair.Count -eq 2) {
            Set-Item -Path "Env:$($pair[0].Trim())" -Value $pair[1].Trim()
        }
    }
}

python (Join-Path $PSScriptRoot "proxy.py")
