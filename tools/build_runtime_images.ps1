param(
  [string]$Workspace = ".",
  [string]$Python = "python",
  [string]$BuildDir = "build",
  [string]$RuntimeDir = "runtime",
  [string]$SystemDir = "",
  [string]$AppsDir = "",
  [string]$C200 = "",
  [string]$Kj409588 = "",
  [string]$Loader = "",
  [string]$UBoot = "",
  [string]$FatImage = "bbk9588_fat_page1c40.img",
  [string]$CombinedNand = "bbk9588_nand_loader0_uboot40_fat_page1c40.bin",
  [string]$StampedNand = "bbk9588_nand.bin",
  [string]$FatPageBase = "0x1c40",
  [string]$OsPageBase = "0x200",
  [string]$LoaderPageBase = "0x0",
  [string]$UBootPageBase = "0x40"
)

$ErrorActionPreference = "Stop"

function Join-Codepoints([int[]]$Codepoints) {
  return -join ($Codepoints | ForEach-Object { [char]$_ })
}

$defaultSystemDir = Join-Codepoints @(0x7cfb, 0x7edf)
$defaultAppsDir = Join-Codepoints @(0x5e94, 0x7528)
$defaultDataDir = Join-Codepoints @(0x6570, 0x636e)
if (-not $SystemDir) { $SystemDir = $defaultSystemDir }
if (-not $AppsDir) { $AppsDir = $defaultAppsDir }
if (-not $Kj409588) { $Kj409588 = Join-Path (Join-Path $SystemDir $defaultDataDir) "kj409588.bin" }
$root = Resolve-Path $Workspace
if (-not $UBoot) {
  $candidateUBoot = Join-Path (Join-Path $SystemDir $defaultDataDir) "u_boot_9588_4740.bin"
  if (Test-Path -LiteralPath (Join-Path $root $candidateUBoot)) {
    $UBoot = $candidateUBoot
  }
}
if (-not $Loader) {
  $candidateLoader = Join-Path (Join-Path $SystemDir $defaultDataDir) "loader_9588_4740.bin"
  if (Test-Path -LiteralPath (Join-Path $root $candidateLoader)) {
    $Loader = $candidateLoader
  }
}

$build = Join-Path $root $BuildDir
$runtime = Join-Path $root $RuntimeDir
New-Item -ItemType Directory -Force -Path $build | Out-Null
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

$systemPath = Join-Path $root $SystemDir
$appsPath = Join-Path $root $AppsDir
$c200Path = if ($C200) { Join-Path $root $C200 } else { "" }
$kj409588Path = Join-Path $root $Kj409588
$loaderPath = if ($Loader) { Join-Path $root $Loader } else { "" }
$ubootPath = if ($UBoot) { Join-Path $root $UBoot } else { "" }
$fatPath = Join-Path $build $FatImage
$combinedPath = Join-Path $build $CombinedNand
$stampedPath = Join-Path $runtime $StampedNand

foreach ($path in @($systemPath, $appsPath, $kj409588Path)) {
  if (-not (Test-Path -LiteralPath $path)) {
    throw "required source path missing: $path"
  }
}
if ($loaderPath -and -not (Test-Path -LiteralPath $loaderPath)) {
  throw "optional loader source path missing: $loaderPath"
}
if ($c200Path -and -not (Test-Path -LiteralPath $c200Path)) {
  throw "optional C200 raw source path missing: $c200Path"
}
if ($ubootPath -and -not (Test-Path -LiteralPath $ubootPath)) {
  throw "optional U-Boot source path missing: $ubootPath"
}

& $Python (Join-Path $root "tools\make_fat16_image.py") `
  --output $fatPath `
  $systemPath $appsPath
if ($LASTEXITCODE -ne 0) { throw "make_fat16_image.py failed" }

$combinedArgs = @(
  (Join-Path $root "tools\make_combined_nand.py"),
  "--fat-image", $fatPath,
  "--output", $combinedPath,
  "--fat-page-base", $FatPageBase
)
if ($ubootPath) {
  $combinedArgs += @("--uboot-image", $ubootPath, "--uboot-page-base", $UBootPageBase)
}
if ($loaderPath) {
  $combinedArgs += @("--loader-image", $loaderPath, "--loader-page-base", $LoaderPageBase)
}
if ($c200Path) {
  $combinedArgs += @("--base-nand", $c200Path, "--os-page-base", $OsPageBase)
}
& $Python @combinedArgs
if ($LASTEXITCODE -ne 0) { throw "make_combined_nand.py failed" }

& $Python (Join-Path $root "tools\stamp_ftl_oob.py") `
  $combinedPath `
  $stampedPath `
  --fat-page-base $FatPageBase
if ($LASTEXITCODE -ne 0) { throw "stamp_ftl_oob.py failed" }

Write-Host "wrote $fatPath"
Write-Host "wrote $combinedPath"
Write-Host "wrote $stampedPath"
