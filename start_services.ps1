# 一键启动 travel-agent 微服务三进程（gateway / backend / monitor）
# 用法：在项目根目录（travel-agent/travel-agent）执行  .\start_services.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# 强制 UTF-8 模式，避免 Windows GBK 控制台无法编码 emoji 导致 print 崩溃
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# 优先使用项目内置 venv，否则回退到 PATH 中的 python
$Candidates = @(
    (Join-Path $Root "travel-agent\Scripts\python.exe"),
    (Join-Path $Root ".venv\Scripts\python.exe"),
    (Join-Path $Root "venv\Scripts\python.exe")
)
$Python = $null
foreach ($c in $Candidates) {
    if (Test-Path $c) { $Python = $c; break }
}
if (-not $Python) { $Python = "python" }

Write-Host "使用 Python: $Python`n"

# 1) backend 内部服务 :8001（Windows 需用其内置 SelectorEventLoop 策略，直接运行 server.py）
Start-Process -FilePath $Python -ArgumentList "server.py" -WorkingDirectory (Join-Path $Root "backend") -WindowStyle Normal

# 2) gateway 对外网关 :8000
Start-Process -FilePath $Python -ArgumentList @("-m", "uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000") -WorkingDirectory $Root -WindowStyle Normal

# 3) monitor 监控服务 :8002
Start-Process -FilePath $Python -ArgumentList @("-m", "uvicorn", "monitor.main:app", "--host", "0.0.0.0", "--port", "8002") -WorkingDirectory $Root -WindowStyle Normal

Write-Host "已启动三个服务："
Write-Host "  gateway  http://127.0.0.1:8000  (对外 JWT)"
Write-Host "  backend  http://127.0.0.1:8001  (内部)"
Write-Host "  monitor  http://127.0.0.1:8002  (监控)"
