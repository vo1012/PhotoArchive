# Собирает dist\PhotoArchive.zip -- папка PhotoArchive\ внутри: exe + документация PDF +
# лицензии + пример конфига, тот же комплект, что раньше планировался для ручного
# распространения на флешке (см. build.bat). Отдельный файл, не inline-команда в build.bat --
# многострочный `powershell -Command "..."` через `^`-продолжение cmd.exe ненадёжен (живая
# находка 2026-07-21: символы `^` внутри кавычек попадали в саму команду буквально вместо
# того, чтобы быть съеденными построчным продолжением, PowerShell пытался выполнить "^" как
# команду).
$ErrorActionPreference = "Stop"

$staging = "dist\zip-staging\PhotoArchive"
if (Test-Path "dist\zip-staging") { Remove-Item "dist\zip-staging" -Recurse -Force }
New-Item -ItemType Directory -Path $staging -Force | Out-Null

Get-ChildItem dist -Exclude zip-staging, PhotoArchive.zip |
    Copy-Item -Destination $staging -Recurse

Compress-Archive -Path $staging -DestinationPath dist\PhotoArchive.zip -Force
Remove-Item "dist\zip-staging" -Recurse -Force
