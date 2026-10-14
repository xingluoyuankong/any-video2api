# any-video2api 启动脚本
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "[any-video2api] 创建虚拟环境 ..."
    python -m venv .venv
    & ".venv\Scripts\python.exe" -m pip install -U pip
    & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
}

# 渠道描述符可热改；此处不覆盖，交给平台自身
$env:PYTHONPATH = $PSScriptRoot
& ".venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8800
