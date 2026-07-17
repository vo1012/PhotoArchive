# Собирает bin/ из инструментов, поставленных через choco (см. .github/workflows/ci.yml).
# Только для CI: реальная portable-поставка пользователю по-прежнему собирается вручную
# по bin/README-BIN.md (choco здесь просто самый быстрый источник тех же официальных
# бинарников на чистом windows-latest runner, без интерактивной установки).
$ErrorActionPreference = "Stop"
$chocoLib = "$env:ChocolateyInstall\lib"
New-Item -ItemType Directory -Force -Path bin | Out-Null

function Find-And-Copy($pattern, $destName, $extraSearchRoots = @()) {
    $found = Get-ChildItem -Path $chocoLib -Recurse -Filter $pattern -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $found) {
        foreach ($root in $extraSearchRoots) {
            $found = Get-ChildItem -Path $root -Recurse -Filter $pattern -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($found) { break }
        }
    }
    if ($found) {
        Copy-Item $found.FullName -Destination "bin\$destName" -Force
        Write-Host "OK: $destName <- $($found.FullName)"
    } else {
        Write-Warning "NOT FOUND: $pattern (needed for $destName)"
    }
}

# The "7zip" choco package installs via a real installer into Program Files, not into the
# chocolatey lib tree like the other packages here -- search there too as a fallback.
$sevenZipRoots = @("$env:ProgramFiles\7-Zip", "${env:ProgramFiles(x86)}\7-Zip")

Find-And-Copy "exiftool.exe" "exiftool.exe"
Find-And-Copy "ffmpeg.exe" "ffmpeg.exe"
Find-And-Copy "ffprobe.exe" "ffprobe.exe"
Find-And-Copy "7z.exe" "7z.exe" $sevenZipRoots
Find-And-Copy "7z.dll" "7z.dll" $sevenZipRoots
Find-And-Copy "UnRAR.exe" "UnRAR.exe"
if (-not (Test-Path "bin\UnRAR.exe")) { Find-And-Copy "unrar.exe" "UnRAR.exe" }

$exiftoolFiles = Get-ChildItem -Path $chocoLib -Recurse -Directory -Filter "exiftool_files" -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($exiftoolFiles) {
    Copy-Item $exiftoolFiles.FullName -Destination "bin\exiftool_files" -Recurse -Force
    Write-Host "OK: exiftool_files <- $($exiftoolFiles.FullName)"
} else {
    Write-Warning "NOT FOUND: exiftool_files directory -- exiftool.exe may refuse to launch without it"
}

Write-Host "`nFinal bin/ contents:"
Get-ChildItem bin -Recurse | Select-Object FullName
