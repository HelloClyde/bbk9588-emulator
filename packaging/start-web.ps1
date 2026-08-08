param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8000,
    [ValidateSet("nand", "c200", "uboot")]
    [string]$BootMode = "nand",
    [ValidateSet("jz4740", "jz4730")]
    [string]$Chip = "jz4730",
    [string]$Nand = "",
    [switch]$NoOpenBrowser,
    [switch]$NoNandPicker,
    [switch]$RebuildImages,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"

if ($Chip -eq "jz4730") {
    $BootMode = "c200"
}

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

function Resolve-NandSource([string]$Source) {
    $sourcePath = [System.IO.Path]::GetFullPath($Source)
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "NAND source does not exist: $sourcePath"
    }
    $extension = [System.IO.Path]::GetExtension($sourcePath)
    if ($extension -ine ".bin" -and $extension -ine ".zip") {
        throw "NAND source must be a .bin or .zip file: $sourcePath"
    }
    return $sourcePath
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
$Jz4730NandImage = Join-Path $RuntimeDir "bbk9588_jz4730_nand.bin"
$Jz4740NandImage = Join-Path $RuntimeDir "bbk9588_nand.bin"
$NandFileName = if ($Chip -eq "jz4730") { "bbk9588_jz4730_nand.bin" } else { "bbk9588_nand.bin" }
$NandArchivePattern = if ($Chip -eq "jz4730") { "bbk9588_jz4730_nand*.zip" } else { "bbk9588_nand*.zip" }
$NandImage = if ($Chip -eq "jz4730") { $Jz4730NandImage } else { $Jz4740NandImage }
$LocalNandImage = Join-Path $Root $NandFileName
$LocalNandArchives = @(Get-ChildItem -LiteralPath $Root -Filter $NandArchivePattern -File -ErrorAction SilentlyContinue)
$NandImportSource = ""
if ($Nand) {
    $NandImportSource = Resolve-NandSource $Nand
} elseif (-not (Test-Path -LiteralPath $NandImage) -and (Test-Path -LiteralPath $LocalNandImage)) {
    $NandImportSource = Resolve-NandSource $LocalNandImage
} elseif (-not (Test-Path -LiteralPath $NandImage) -and $LocalNandArchives.Count -eq 1) {
    $NandImportSource = Resolve-NandSource $LocalNandArchives[0].FullName
}
$FallbackNandImages = @()
if ($Chip -eq "jz4740") {
    $FallbackNandImages = @(
        (Join-Path $Root "build\bbk9588_nand_loader0_uboot40_fat_page1c40_root512_ftloob.bin"),
        (Join-Path $Root "build\bbk9588_nand_loader0_uboot40_fat_page1c40_root256_ftloob.bin"),
        (Join-Path $Root "build\bbk9588_nand_fat_page1c40_root512_ftloob.bin"),
        (Join-Path $Root "build\bbk9588_nand_fat_page1c40_root256_ftloob.bin"),
        (Join-Path $Root "build\bbk9588_nand_uboot40_fat_page1c40_root512_ftloob.bin"),
        (Join-Path $Root "build\bbk9588_nand_uboot40_fat_page1c40_root256_ftloob.bin")
    )
}
if (-not $RebuildImages -and -not (Test-Path -LiteralPath $NandImage) -and -not $NandImportSource) {
    foreach ($candidate in $FallbackNandImages) {
        if (Test-Path -LiteralPath $candidate) {
            $NandImportSource = Resolve-NandSource $candidate
            break
        }
    }
}

if (-not (Test-Path -LiteralPath $NandImage) -and -not $NandImportSource -and -not $NoNandPicker) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = "Select your BBK 9588 NAND image"
        $dialog.Filter = "BBK 9588 NAND (*.bin;*.zip)|*.bin;*.zip|All files (*.*)|*.*"
        $dialog.CheckFileExists = $true
        if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            $NandImportSource = Resolve-NandSource $dialog.FileName
        }
    } catch {
        Write-Warning "Could not open NAND file picker: $($_.Exception.Message)"
    }
}
$SystemDir = Join-Path $Root $SystemDirName
$AppsDir = Join-Path $Root $AppsDirName
$DataDir = Join-Path $SystemDir $DataDirName
$Jz4730C200Image = Join-Path $DataDir "C200knl.bin"
$C200FileName = if ($Chip -eq "jz4730") { "C200knl.bin" } else { "C200.bin" }
$C200Image = Join-Path $DataDir $C200FileName
$LoaderImage = Join-Path $DataDir "loader_9588_4740.bin"
$Kj409588Image = Join-Path $DataDir "kj409588.bin"
$UBootImage = Join-Path $DataDir "u_boot_9588_4740.bin"
if ($Chip -eq "jz4740" -and ($RebuildImages -or (-not (Test-Path -LiteralPath $NandImage) -and -not $NandImportSource))) {
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
            $Python,
            "-RuntimeDir",
            "build\nand-rebuild"
        )
        $buildRuntimeArgs += @("-Loader", (Join-Path (Join-Path $SystemDirName $DataDirName) "loader_9588_4740.bin"))
        $buildRuntimeArgs += @("-UBoot", (Join-Path (Join-Path $SystemDirName $DataDirName) "u_boot_9588_4740.bin"))
        powershell @buildRuntimeArgs
        $NandImportSource = Join-Path $Root "build\nand-rebuild\bbk9588_nand.bin"
    }
}

if (-not (Test-Path -LiteralPath $NandImage) -and -not $NandImportSource) {
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

For JZ4730 direct boot:
  系统\数据\C200knl.bin
  $NandFileName (a matching JZ4730 raw NAND dump)

Or pass a matching raw image/archive explicitly:
  .\start-web.cmd -Chip $Chip -Nand C:\path\to\$NandFileName

You may also place $NandFileName or $NandArchivePattern next to start-web.cmd.
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
$NandImportArgs = @()
if ($NandImportSource) {
    $NandImportArgs = @("--nand-import-source", $NandImportSource)
}
$BrowserHost = $HostName
if ($BrowserHost -in @("0.0.0.0", "::", "[::]")) {
    $BrowserHost = "127.0.0.1"
}
$url = "http://${BrowserHost}:${Port}/"
Write-Host "Starting BBK 9588 emulator at $url"
$BrowserReadyJob = $null
if (-not $NoOpenBrowser) {
    Write-Host "The browser will open when the web frontend is ready."
    $StatusUrl = "${url}api/status"
    $BrowserReadyJob = Start-Job -ScriptBlock {
        param(
            [string]$StatusUrl,
            [string]$BrowserUrl
        )

        while ($true) {
            try {
                $response = Invoke-WebRequest `
                    -Uri $StatusUrl `
                    -UseBasicParsing `
                    -TimeoutSec 2 `
                    -ErrorAction Stop
                if ([int]$response.StatusCode -eq 200) {
                    Start-Process $BrowserUrl
                    return
                }
            } catch {
                Start-Sleep -Milliseconds 250
            }
        }
    } -ArgumentList $StatusUrl, $url
}

$env:PYTHONNOUSERSITE = "1"
$FrontendExitCode = 1
try {
    & $Python -m emu.web.frontend `
        --boot-mode $BootMode `
        --chip $Chip `
        --qemu $Qemu `
        --nand-image $NandImage `
        --jz4730-nand-image $Jz4730NandImage `
        --jz4740-nand-image $Jz4740NandImage `
        --jz4730-image $Jz4730C200Image `
        --host $HostName `
        --port $Port `
        @NandImportArgs `
        @ImageArgs `
        @ExtraArgs
    $FrontendExitCode = $LASTEXITCODE
} finally {
    if ($null -ne $BrowserReadyJob) {
        Stop-Job -Job $BrowserReadyJob -ErrorAction SilentlyContinue
        Remove-Job -Job $BrowserReadyJob -Force -ErrorAction SilentlyContinue
    }
}
exit $FrontendExitCode
