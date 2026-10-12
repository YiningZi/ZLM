param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
& $Python (Join-Path $projectRoot "tools/build.py")
if ($LASTEXITCODE -ne 0) { throw "ZLM extension build failed" }
