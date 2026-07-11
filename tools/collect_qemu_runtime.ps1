param(
    [Parameter(Mandatory = $true)]
    [string]$QemuExe,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeDir,
    [string]$OutputExeName = "bbk9588-qemu-system-mipsel.exe",
    [string]$MsysBash = "C:\msys64\usr\bin\bash.exe"
)

$ErrorActionPreference = "Stop"

$qemuExePath = [System.IO.Path]::GetFullPath($QemuExe)
$runtimeRoot = [System.IO.Path]::GetFullPath($RuntimeDir)
$binDir = Join-Path $runtimeRoot "bin"

if (-not (Test-Path -LiteralPath $qemuExePath)) {
    throw "QEMU executable not found: $qemuExePath"
}
if (-not (Test-Path -LiteralPath $MsysBash)) {
    throw "MSYS2 bash not found: $MsysBash"
}

if (Test-Path -LiteralPath $runtimeRoot) {
    Remove-Item -LiteralPath $runtimeRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $binDir | Out-Null

$destExe = Join-Path $binDir $OutputExeName
Copy-Item -LiteralPath $qemuExePath -Destination $destExe -Force

function Convert-ToMsysPath([string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path).Replace("\", "/")
    if ($full -match "^([A-Za-z]):/(.*)$") {
        return "/" + $Matches[1].ToLowerInvariant() + "/" + $Matches[2]
    }
    return $full
}

$msysRoot = [System.IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $MsysBash) "..\.."))

function Convert-FromMsysPath([string]$Path) {
    $text = $Path.Trim()
    if ($text -match "^/([A-Za-z])/(.*)$") {
        return ($Matches[1].ToUpperInvariant() + ":\" + $Matches[2].Replace("/", "\"))
    }
    if ($text.StartsWith("/")) {
        return (Join-Path $msysRoot $text.TrimStart("/").Replace("/", "\"))
    }
    return $text.Replace("/", "\")
}

$exePosix = Convert-ToMsysPath $destExe
$lddScript = @"
set -euo pipefail
export MSYSTEM=UCRT64
export PATH=/ucrt64/bin:/usr/bin:`$PATH
ldd "$exePosix" | sed -n \
  -e 's/.*=> \([^ ]*\.dll\).*/\1/p' \
  -e 's/^[[:space:]]*\([^ ]*\.dll\).*/\1/p' | sort -u
"@

$depLines = & $MsysBash -lc $lddScript
if ($LASTEXITCODE -ne 0) {
    throw "ldd failed for $destExe"
}

$copied = New-Object System.Collections.Generic.List[string]
$copied.Add((Split-Path -Leaf $destExe)) | Out-Null

foreach ($line in $depLines) {
    $dep = $line.Trim()
    if (-not $dep -or -not $dep.ToLowerInvariant().EndsWith(".dll")) {
        continue
    }
    $depLower = $dep.Replace("\", "/").ToLowerInvariant()
    if ($depLower.StartsWith("/c/windows/") -or $depLower.StartsWith("c:/windows/")) {
        continue
    }
    $source = Convert-FromMsysPath $dep
    if (-not (Test-Path -LiteralPath $source)) {
        throw "QEMU dependency reported by ldd was not found: $dep -> $source"
    }
    $name = Split-Path -Leaf $source
    Copy-Item -LiteralPath $source -Destination (Join-Path $binDir $name) -Force
    $copied.Add($name) | Out-Null
}

$depManifest = Join-Path $binDir "qemu-runtime-deps.txt"
$copied | Sort-Object -Unique | Set-Content -LiteralPath $depManifest -Encoding ascii

$result = [ordered]@{
    runtime_dir = $runtimeRoot
    qemu_exe = $destExe
    file_count = (Get-ChildItem -LiteralPath $binDir -File | Measure-Object).Count
}

if ($env:GITHUB_OUTPUT) {
    foreach ($key in $result.Keys) {
        "$key=$($result[$key])" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    }
}

$result | ConvertTo-Json -Depth 3
