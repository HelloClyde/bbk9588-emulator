param(
    [string]$Version = "",
    [string]$PackageName = "bbk9588-emulator",
    [string]$OutputDir = "",
    [string]$StagingDir = "",
    [string]$QemuRuntimeDir = "",
    [string]$PythonRoot = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))

function Copy-DirectoryContents([string]$Source, [string]$Destination) {
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $Destination $_.Name) -Recurse -Force
    }
}

function Add-AllowedBinaryRoot([System.Collections.Generic.List[string]]$Roots, [string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path).TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    $Roots.Add($full) | Out-Null
}

function Test-IsUnderAnyRoot([string]$Path, [System.Collections.Generic.List[string]]$Roots) {
    $full = [System.IO.Path]::GetFullPath($Path)
    foreach ($root in $Roots) {
        if ($full.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

if (-not $OutputDir) {
    $OutputDir = Join-Path $RepoRoot "build\dist"
}
if (-not $StagingDir) {
    $StagingDir = Join-Path $RepoRoot "build\package"
}

if (-not $Version) {
    $Version = $env:GITHUB_REF_NAME
}
if (-not $Version) {
    try {
        $Version = (git -C $RepoRoot rev-parse --short HEAD).Trim()
    } catch {
        $Version = "local"
    }
}

$SafeVersion = ($Version -replace '[^A-Za-z0-9._-]', '-').Trim("-")
if (-not $SafeVersion) {
    $SafeVersion = "local"
}

$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
$StagingDir = [System.IO.Path]::GetFullPath($StagingDir)
$BuildRoot = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot "build"))
foreach ($path in @($OutputDir, $StagingDir)) {
    if (-not $path.StartsWith($BuildRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to write package output outside build/: $path"
    }
}

$PackageRoot = Join-Path $StagingDir "$PackageName-$SafeVersion"
$ZipPath = Join-Path $OutputDir "$PackageName-$SafeVersion.zip"
$ShaPath = "$ZipPath.sha256"

if (Test-Path -LiteralPath $PackageRoot) {
    Remove-Item -LiteralPath $PackageRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$releaseReadme = Join-Path $RepoRoot "packaging\RELEASE_README.md"
foreach ($file in @(
    "README.md",
    "CONTRIBUTING.md",
    "DATA_NOTICE.md",
    "requirements.txt",
    "COPYING",
    "COPYING.LIB"
)) {
    $source = Join-Path $RepoRoot $file
    if (Test-Path -LiteralPath $source) {
        $destinationName = $file
        if ($file -eq "README.md" -and (Test-Path -LiteralPath $releaseReadme)) {
            $destinationName = "PROJECT_README.md"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $PackageRoot $destinationName)
    }
}
if (Test-Path -LiteralPath $releaseReadme) {
    Copy-Item -LiteralPath $releaseReadme -Destination (Join-Path $PackageRoot "README.md") -Force
}

Copy-Item -LiteralPath (Join-Path $RepoRoot "emu") -Destination (Join-Path $PackageRoot "emu") -Recurse

$runtimeTools = @(
    "__init__.py",
    "build_runtime_images.ps1",
    "make_combined_nand.py",
    "stamp_nand_ecc.py",
    "audit_ftl_nand.py",
    "make_fat16_image.py",
    "stamp_ftl_oob.py"
)
$runtimeToolsDir = Join-Path $PackageRoot "tools"
New-Item -ItemType Directory -Force -Path $runtimeToolsDir | Out-Null
foreach ($file in $runtimeTools) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot "tools\$file") -Destination $runtimeToolsDir -Force
}

foreach ($file in @("start-web.ps1", "start-web.cmd")) {
    $source = Join-Path $RepoRoot ("packaging\" + $file)
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $PackageRoot $file) -Force
    }
}

$allowedBinaryRoots = New-Object System.Collections.Generic.List[string]
if ($QemuRuntimeDir) {
    foreach ($relative in @(
        "packaging",
        "qemu\README.md",
        "emu\qemu\check_source_tree.py",
        "qemu\scripts",
        "qemu\overlay",
        "tests",
        "tools\collect_qemu_runtime.ps1",
        "tools\package_emulator.ps1"
    )) {
        $path = Join-Path $PackageRoot $relative
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force
        }
    }

    $runtimeSource = [System.IO.Path]::GetFullPath($QemuRuntimeDir)
    $runtimeBin = Join-Path $runtimeSource "bin"
    $runtimeExe = Join-Path $runtimeBin "bbk9588-qemu-system-mipsel.exe"
    if (-not (Test-Path -LiteralPath $runtimeExe)) {
        throw "Packaged QEMU runtime is missing bbk9588-qemu-system-mipsel.exe: $runtimeExe"
    }
    Copy-DirectoryContents $runtimeSource $PackageRoot
    Add-AllowedBinaryRoot $allowedBinaryRoots (Join-Path $PackageRoot "bin")
}

if ($PythonRoot) {
    $pythonSource = [System.IO.Path]::GetFullPath($PythonRoot)
    if (-not (Test-Path -LiteralPath (Join-Path $pythonSource "python.exe"))) {
        throw "Python runtime root does not contain python.exe: $pythonSource"
    }
    $pythonDest = Join-Path $PackageRoot "python"
    New-Item -ItemType Directory -Force -Path $pythonDest | Out-Null
    foreach ($item in @("DLLs", "Lib")) {
        $source = Join-Path $pythonSource $item
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $pythonDest $item) -Recurse -Force
        }
    }
    Get-ChildItem -LiteralPath $pythonSource -File -Force | Where-Object {
        $_.Name -match "^(pythonw?\.exe|python[0-9]+\.dll|vcruntime.*\.dll|LICENSE.*)$"
    } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $pythonDest $_.Name) -Force
    }
    $condaLibraryBin = Join-Path $pythonSource "Library\bin"
    if (Test-Path -LiteralPath $condaLibraryBin) {
        Get-ChildItem -LiteralPath $condaLibraryBin -Filter "ffi*.dll" -File | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $pythonDest $_.Name) -Force
        }
    }
    foreach ($path in @(
        (Join-Path $pythonDest "Lib\test"),
        (Join-Path $pythonDest "Lib\site-packages"),
        (Join-Path $pythonDest "Lib\ensurepip")
    )) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force
        }
    }
    $sitePackages = Join-Path $pythonDest "Lib\site-packages"
    New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
    $pythonExe = Join-Path $pythonSource "python.exe"
    & $pythonExe -m pip install `
        --disable-pip-version-check `
        --no-compile `
        --target $sitePackages `
        -r (Join-Path $RepoRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "failed to install bundled Python dependencies"
    }
    Add-AllowedBinaryRoot $allowedBinaryRoots $pythonDest
}

$excludedDirectoryNames = @(
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist"
)
Get-ChildItem -LiteralPath $PackageRoot -Directory -Recurse -Force | Where-Object {
    $excludedDirectoryNames -contains $_.Name
} | Sort-Object FullName -Descending | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Recurse -Force
}

Get-ChildItem -LiteralPath $PackageRoot -Directory -Recurse -Force -Filter "__pycache__" |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $PackageRoot -File -Recurse -Force | Where-Object {
    $_.Extension.ToLowerInvariant() -in @(".pyc", ".pyo")
} |
    Remove-Item -Force

$forbiddenExtensions = @(
    ".a",
    ".bda",
    ".bin",
    ".dba",
    ".dll",
    ".dlx",
    ".dylib",
    ".elf",
    ".exe",
    ".lib",
    ".map",
    ".o",
    ".obj",
    ".pdb",
    ".so"
)
$forbidden = Get-ChildItem -LiteralPath $PackageRoot -File -Recurse -Force | Where-Object {
    ($forbiddenExtensions -contains $_.Extension.ToLowerInvariant()) -and
        $_.Name -ne "COPYING.LIB" -and
        -not (Test-IsUnderAnyRoot $_.FullName $allowedBinaryRoots)
}
if ($forbidden) {
    $list = ($forbidden | Select-Object -First 20 | ForEach-Object { $_.FullName }) -join "`n"
    throw "Package contains firmware/build binary files:`n$list"
}

foreach ($required in @(
    "README.md",
    "start-web.ps1",
    "start-web.cmd",
    "emu\web\frontend.py",
    "tools\build_runtime_images.ps1"
)) {
    $path = Join-Path $PackageRoot $required
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required runtime package file is missing: $required"
    }
}
if ($QemuRuntimeDir -and -not (Test-Path -LiteralPath (Join-Path $PackageRoot "bin\bbk9588-qemu-system-mipsel.exe"))) {
    throw "Required packaged emulator executable is missing: bin\bbk9588-qemu-system-mipsel.exe"
}

$manifestPath = Join-Path $PackageRoot "MANIFEST.txt"
$files = Get-ChildItem -LiteralPath $PackageRoot -File -Recurse -Force | Sort-Object FullName
$packageRootPrefix = $PackageRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
$manifest = foreach ($file in $files) {
    $fullName = [System.IO.Path]::GetFullPath($file.FullName)
    if (-not $fullName.StartsWith($packageRootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Package manifest path escaped package root: $fullName"
    }
    $relative = $fullName.Substring($packageRootPrefix.Length).Replace("\", "/")
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
    "$hash  $relative"
}
$manifest | Set-Content -LiteralPath $manifestPath -Encoding ascii

if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}
if (Test-Path -LiteralPath $ShaPath) {
    Remove-Item -LiteralPath $ShaPath -Force
}

Compress-Archive -LiteralPath $PackageRoot -DestinationPath $ZipPath -CompressionLevel Optimal
$zipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipPath).Hash.ToLowerInvariant()
"$zipHash  $(Split-Path -Leaf $ZipPath)" | Set-Content -LiteralPath $ShaPath -Encoding ascii

$result = [ordered]@{
    version = $SafeVersion
    package_root = $PackageRoot
    zip_path = $ZipPath
    sha256_path = $ShaPath
    artifact_name = "$PackageName-$SafeVersion"
    sha256 = $zipHash
}

if ($env:GITHUB_OUTPUT) {
    foreach ($key in $result.Keys) {
        "$key=$($result[$key])" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    }
}

$result | ConvertTo-Json -Depth 3
