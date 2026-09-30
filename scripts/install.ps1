# Windows installer for web-mcp (global tool via uv). Run from a PowerShell prompt:
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
$ErrorActionPreference = "Stop"
$src = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "==> installing uv"
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
Write-Host "==> installing web-mcp"
uv tool install --force --reinstall "$src"
uv tool update-shell | Out-Null
web-mcp config init | Out-Null
web-mcp doctor
Write-Host "`nRegister with a client:  web-mcp install-client print   (or claude-desktop / cursor / windsurf)"