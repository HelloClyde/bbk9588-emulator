param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8000,
    [ValidateSet("nand", "c200", "uboot")]
    [string]$BootMode = "nand",
    [string]$Nand = "",
    [switch]$NoOpenBrowser,
    [switch]$NoNandPicker,
    [switch]$RebuildImages,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Resolve-PythonExe {
    $bundled = Join-Path $Root "python\python.exe"
    if (Test-Path -LiteralPath $bundled) {
        return $bundled
    }
    foreach ($name in @("python", "py")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            return $cmd.Source
        }
    }
    throw "Python was not found. Use the release package with bundled Python, or install Python 3.11+ and retry."
}

function Test-ExtraArgOption([string]$Name) {
    foreach ($arg in $ExtraArgs) {
        if ($arg -eq $Name -or $arg.StartsWith("$Name=")) {
            return $true
        }
    }
    return $false
}

function Join-Codepoints([int[]]$Codepoints) {
    return -join ($Codepoints | ForEach-Object { [char]$_ })
}

function Import-NandSource([string]$Source, [string]$Destination) {
    $sourcePath = [System.IO.Path]::GetFullPath($Source)
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "NAND source does not exist: $sourcePath"
    }

    $temporaryDir = Join-Path (Split-Path -Parent $Destination) ".nand-import"
    $temporaryImage = "$Destination.importing"
    $imageSource = $sourcePath
    try {
        if ([System.IO.Path]::GetExtension($sourcePath) -ieq ".zip") {
            if (Test-Path -LiteralPath $temporaryDir) {
                Remove-Item -LiteralPath $temporaryDir -Recurse -Force
            }
            Expand-Archive -LiteralPath $sourcePath -DestinationPath $temporaryDir -Force
            $images = @(Get-ChildItem -LiteralPath $temporaryDir -Filter "*.bin" -File -Recurse)
            if ($images.Count -ne 1) {
                throw "NAND archive must contain exactly one .bin file; found $($images.Count)"
            }
            $imageSource = $images[0].FullName
        }

        $length = (Get-Item -LiteralPath $imageSource).Length
        $supportedSizes = @(536870912L, 553648128L)
        if ($length -notin $supportedSizes) {
            throw "Unsupported NAND size $length bytes; expected 512 MiB page data or 528 MiB raw data+OOB"
        }
        Copy-Item -LiteralPath $imageSource -Destination $temporaryImage -Force
        Move-Item -LiteralPath $temporaryImage -Destination $Destination -Force
        $runtimeDir = Split-Path -Parent $Destination
        $packageRoot = Split-Path -Parent $runtimeDir
        $checkpointDir = Join-Path $runtimeDir "qemu_nand_persistent"
        $workDir = Join-Path $packageRoot "build\qemu_nand_runs"
        foreach ($derivedPath in @($checkpointDir, $workDir)) {
            if (Test-Path -LiteralPath $derivedPath) {
                Remove-Item -LiteralPath $derivedPath -Recurse -Force
            }
        }
        $stream = [System.IO.File]::OpenRead($Destination)
        try {
            $hasher = [System.Security.Cryptography.SHA256]::Create()
            try {
                $hashBytes = $hasher.ComputeHash($stream)
                $hash = ([System.BitConverter]::ToString($hashBytes) -replace "-", "").ToLowerInvariant()
            } finally {
                $hasher.Dispose()
            }
        } finally {
            $stream.Dispose()
        }
        Write-Host "Imported NAND: $Destination"
        Write-Host "SHA256: $hash"
    } finally {
        Remove-Item -LiteralPath $temporaryImage -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $temporaryDir) {
            Remove-Item -LiteralPath $temporaryDir -Recurse -Force
        }
    }
}

$SystemDirName = Join-Codepoints @(0x7cfb, 0x7edf)
$AppsDirName = Join-Codepoints @(0x5e94, 0x7528)
$DataDirName = Join-Codepoints @(0x6570, 0x636e)

$Python = Resolve-PythonExe
$Qemu = Join-Path $Root "bin\bbk9588-qemu-system-mipsel.exe"
if (-not (Test-Path -LiteralPath $Qemu)) {
    throw "Packaged QEMU executable not found: $Qemu"
}

$RuntimeDir = Join-Path $Root "runtime"
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
$NandImage = Join-Path $RuntimeDir "bbk9588_nand.bin"
$LocalNandImage = Join-Path $Root "bbk9588_nand.bin"
$LocalNandArchives = @(Get-ChildItem -LiteralPath $Root -Filter "bbk9588_nand*.zip" -File -ErrorAction SilentlyContinue)
if ($Nand) {
    Import-NandSource $Nand $NandImage
} elseif (-not (Test-Path -LiteralPath $NandImage) -and (Test-Path -LiteralPath $LocalNandImage)) {
    Import-NandSource $LocalNandImage $NandImage
} elseif (-not (Test-Path -LiteralPath $NandImage) -and $LocalNandArchives.Count -eq 1) {
    Import-NandSource $LocalNandArchives[0].FullName $NandImage
}
$FallbackNandImages = @(
    (Join-Path $Root "build\bbk9588_nand_loader0_uboot40_fat_page1c40_root512_ftloob.bin"),
    (Join-Path $Root "build\bbk9588_nand_loader0_uboot40_fat_page1c40_root256_ftloob.bin"),
    (Join-Path $Root "build\bbk9588_nand_fat_page1c40_root512_ftloob.bin"),
    (Join-Path $Root "build\bbk9588_nand_fat_page1c40_root256_ftloob.bin"),
    (Join-Path $Root "build\bbk9588_nand_uboot40_fat_page1c40_root512_ftloob.bin"),
    (Join-Path $Root "build\bbk9588_nand_uboot40_fat_page1c40_root256_ftloob.bin")
)
if (-not $RebuildImages -and -not (Test-Path -LiteralPath $NandImage)) {
    foreach ($candidate in $FallbackNandImages) {
        if (Test-Path -LiteralPath $candidate) {
            Copy-Item -LiteralPath $candidate -Destination $NandImage
            break
        }
    }
}

if (-not (Test-Path -LiteralPath $NandImage) -and -not $NoNandPicker) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = "Select your BBK 9588 NAND image"
        $dialog.Filter = "BBK 9588 NAND (*.bin;*.zip)|*.bin;*.zip|All files (*.*)|*.*"
        $dialog.CheckFileExists = $true
        if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            Import-NandSource $dialog.FileName $NandImage
        }
    } catch {
        Write-Warning "Could not open NAND file picker: $($_.Exception.Message)"
    }
}
$SystemDir = Join-Path $Root $SystemDirName
$AppsDir = Join-Path $Root $AppsDirName
$DataDir = Join-Path $SystemDir $DataDirName
$C200Image = Join-Path $DataDir "C200.bin"
$LoaderImage = Join-Path $DataDir "loader_9588_4740.bin"
$Kj409588Image = Join-Path $DataDir "kj409588.bin"
$UBootImage = Join-Path $DataDir "u_boot_9588_4740.bin"
if ($RebuildImages -or -not (Test-Path -LiteralPath $NandImage)) {
    if (
        (Test-Path -LiteralPath $LoaderImage) -and
        (Test-Path -LiteralPath $Kj409588Image) -and
        (Test-Path -LiteralPath $UBootImage) -and
        (Test-Path -LiteralPath $AppsDir)
    ) {
        $buildRuntimeArgs = @(
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            (Join-Path $Root "tools\build_runtime_images.ps1"),
            "-Workspace",
            $Root,
            "-Python",
            $Python
        )
        $buildRuntimeArgs += @("-Loader", (Join-Path (Join-Path $SystemDirName $DataDirName) "loader_9588_4740.bin"))
        $buildRuntimeArgs += @("-UBoot", (Join-Path (Join-Path $SystemDirName $DataDirName) "u_boot_9588_4740.bin"))
        powershell @buildRuntimeArgs
    }
}

if (-not (Test-Path -LiteralPath $NandImage)) {
    throw @"
Runtime NAND image is missing:
  $NandImage

Place the firmware/resource dump next to this script and run again:
  系统\数据\loader_9588_4740.bin
  系统\数据\u_boot_9588_4740.bin
  系统\数据\kj409588.bin
  系统\...
  应用\...

Optional for C200 direct-boot compatibility mode:
  系统\数据\C200.bin

Or pass a raw image/archive explicitly:
  .\start-web.cmd -Nand C:\path\to\bbk9588_nand.bin
  .\start-web.cmd -Nand C:\path\to\bbk9588_nand.zip

You may also place bbk9588_nand.bin or bbk9588_nand*.zip next to start-web.cmd.
"@
}

$ImageArgs = @()
if (-not (Test-ExtraArgOption "--image")) {
    if ($BootMode -eq "c200") {
        if (-not (Test-Path -LiteralPath $C200Image)) {
            throw "C200 boot mode requires a boot image: $C200Image"
        }
        $ImageArgs = @("--image", $C200Image)
    }
}

$url = "http://${HostName}:${Port}/"
Write-Host "Starting BBK 9588 emulator at $url"
if (-not $NoOpenBrowser) {
    Start-Process $url
}

$env:PYTHONNOUSERSITE = "1"
& $Python -m emu.web.frontend `
    --boot-mode $BootMode `
    --qemu $Qemu `
    --nand-image $NandImage `
    --host $HostName `
    --port $Port `
    @ImageArgs `
    @ExtraArgs
