@echo off
REM Собирает PhotoArchive.exe из photosort_win.py. Запускать на Windows из папки build\.
REM Требует: pip install -r ..\requirements.txt (версии закреплены там)
REM Требует: bin\exiftool.exe, ffmpeg.exe, ffprobe.exe, 7z.exe, 7z.dll, UnRAR.exe
REM (см. ..\bin\README-BIN.md) -- без них соберётся, но упадёт в лог при попытке их вызвать.

setlocal

set BIN=..\bin

if not exist "%BIN%\exiftool.exe" echo [WARN] %BIN%\exiftool.exe не найден -- см. bin\README-BIN.md
if not exist "%BIN%\exiftool_files" echo [WARN] %BIN%\exiftool_files не найден -- exiftool.exe без него не запустится, см. bin\README-BIN.md
if not exist "%BIN%\ffmpeg.exe"   echo [WARN] %BIN%\ffmpeg.exe не найден -- см. bin\README-BIN.md
if not exist "%BIN%\ffprobe.exe"  echo [WARN] %BIN%\ffprobe.exe не найден -- см. bin\README-BIN.md
if not exist "%BIN%\7z.exe"       echo [WARN] %BIN%\7z.exe не найден -- см. bin\README-BIN.md
if not exist "%BIN%\7z.dll"       echo [WARN] %BIN%\7z.dll не найден -- см. bin\README-BIN.md
if not exist "%BIN%\UnRAR.exe"    echo [WARN] %BIN%\UnRAR.exe не найден -- см. bin\README-BIN.md

pyinstaller --onefile --name PhotoArchive ^
  --add-binary "%BIN%\exiftool.exe;bin" ^
  --add-data "%BIN%\exiftool_files;bin\exiftool_files" ^
  --add-binary "%BIN%\ffmpeg.exe;bin" ^
  --add-binary "%BIN%\ffprobe.exe;bin" ^
  --add-binary "%BIN%\7z.exe;bin" ^
  --add-binary "%BIN%\7z.dll;bin" ^
  --add-binary "%BIN%\UnRAR.exe;bin" ^
  --collect-data reverse_geocoder ^
  ..\photosort_win.py

REM В этом репозитории (в отличие от dev-репозитория, где bin/licenses/ наполняется вручную
REM перед каждой сборкой) лицензии сторонних бинарников уже закоммичены в ..\licenses\ на
REM верхнем уровне -- см. bin\README-BIN.md.
if exist "..\licenses" (
  xcopy /E /I /Y "..\licenses" "dist\licenses\" >nul
  echo Лицензии сторонних компонентов скопированы в dist\licenses\.
) else (
  echo [WARN] ..\licenses не найден -- лицензии сторонних бинарников не попадут в dist\, см. ..\THIRD_PARTY_LICENSES.md перед публикацией релиза
)

echo Конвертация пользовательской документации (README/QUICKSTART/FAQ/PhotoArchive_ot_avtora/
echo THIRD_PARTY_LICENSES) в PDF отличалась бы форматом от dev-репозитория, где .md остаётся
echo источником истины только на GitHub -- здесь, наоборот, .md рендерится самим GitHub, а
echo релиз распространяется как отдельный .exe-asset, так что документация в dist\ остаётся
echo в исходном .md-формате, конвертация в PDF НЕ выполняется.

copy /Y ..\photoarchive_config.yaml.example dist\ >nul
copy /Y ..\LICENSE dist\ >nul
copy /Y ..\NOTICE dist\ >nul
echo photoarchive_config.yaml.example/LICENSE/NOTICE скопированы в dist\.

REM Живая находка 2026-07-15 (dev-репозиторий): dist\photoarchive_config.yaml (БЕЗ .example)
REM появлялся между пересборками -- сам auto-create в photosort_win.py срабатывает только на
REM голом запуске/интерактивном вводе (--help/--version/--formats гарантированно не доходят
REM до этого кода, см. _main()), так что источник -- ручной прогон exe из dist\ во время
REM отладки, а не сама сборка. На всякий случай подчищаем перед тем как считать dist\ готовым.
if exist dist\photoarchive_config.yaml del /Q dist\photoarchive_config.yaml

echo.
echo Готово: dist\PhotoArchive.exe (если сборка прошла без ошибок выше).
echo Перед ПУБЛИКАЦИЕЙ релиза -- см. ..\THIRD_PARTY_LICENSES.md.

endlocal
