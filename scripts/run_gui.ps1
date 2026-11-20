param([ValidateSet('alice','bob')][string]$Account='alice')
$ErrorActionPreference='Stop'
Set-Location (Join-Path $PSScriptRoot '..')
& .\.venv\Scripts\python.exe -m secure_transfer.gui --config "runtime/$Account.json"
