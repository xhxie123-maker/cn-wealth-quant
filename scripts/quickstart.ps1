# 一键演示（PowerShell）
# 用法：powershell -ExecutionPolicy Bypass -File scripts/quickstart.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$env:PYTHONPATH = Join-Path $Root "src"

Write-Host "[1/4] 检查环境 ..."
python scripts/check_env.py

Write-Host "[2/4] 生成示例数据 ..."
python -m wealth_quant.cli generate-sample

Write-Host "[3/4] 运行端到端演示 ..."
python -m wealth_quant.cli demo --strategy DUAL_MOMENTUM_V1 --out reports/demo

Write-Host "[4/4] 校验产物与审计链 ..."
python scripts/verify_reports.py reports/demo

Write-Host "完成。启动界面：python -m wealth_quant.cli serve"
