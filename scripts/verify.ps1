# 一键验证：测试 → 故障注入 → （可选）评测
#
#   .\scripts\verify.ps1              # 单测 + 故障注入
#   .\scripts\verify.ps1 -WithEval    # 再跑 20 条评测（约 12 分钟）
param(
    [switch]$WithEval,
    [int]$EvalLimit = 20
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "`n=== 1/2 单元测试 ===" -ForegroundColor Cyan
& $py -m pytest -p no:warnings -q
if ($LASTEXITCODE -ne 0) { Write-Host "测试失败" -ForegroundColor Red; exit 1 }

Write-Host "`n=== 2/2 故障注入 ===" -ForegroundColor Cyan
$env:LEASE_SEC = "5"; $env:RECLAIM_SEC = "1"; $env:POLL_SEC = "0.2"
& $py scripts\chaos.py --all
if ($LASTEXITCODE -ne 0) { Write-Host "故障注入未全部通过" -ForegroundColor Red; exit 1 }

if ($WithEval) {
    Write-Host "`n=== 附加：20 条评测 ===" -ForegroundColor Cyan
    & $py eval\run_eval.py --limit $EvalLimit
}

Write-Host "`n全部通过" -ForegroundColor Green
