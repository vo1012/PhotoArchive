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
echo THIRD_PARTY_LICENSES) в PDF -- см. RELEASING.md. Требует "pip install markdown pypdf"
echo (см. requirements.txt) и Microsoft Edge, установленный на этой машине (GitHub-hosted
echo windows-latest runner несёт Edge предустановленным). Нужна только для отдельного
echo ZIP-ассета релиза (dist\PhotoArchive\) -- .md источники на GitHub эта конвертация не
echo затрагивает, там документация по-прежнему рендерится самим GitHub как есть.
python md_to_pdf.py .. dist
if errorlevel 1 (
  echo [ERROR] Конвертация markdown -^> PDF не удалась -- см. вывод выше. dist\ НЕ содержит
  echo пользовательскую документацию, ZIP-ассет релиза нельзя считать готовым.
  exit /b 1
)

copy /Y ..\photoarchive_config.yaml.example dist\ >nul
copy /Y ..\LICENSE dist\ >nul
copy /Y ..\NOTICE dist\ >nul
copy /Y ..\PhotoArchive_buklet.pdf dist\ >nul
echo photoarchive_config.yaml.example/LICENSE/NOTICE/PhotoArchive_buklet.pdf скопированы в dist\.

REM Живая находка 2026-07-15 (dev-репозиторий): dist\photoarchive_config.yaml (БЕЗ .example)
REM появлялся между пересборками -- сам auto-create в photosort_win.py срабатывает только на
REM голом запуске/интерактивном вводе (--help/--version/--formats гарантированно не доходят
REM до этого кода, см. _main()), так что источник -- ручной прогон exe из dist\ во время
REM отладки, а не сама сборка. На всякий случай подчищаем перед тем как считать dist\ готовым.
if exist dist\photoarchive_config.yaml del /Q dist\photoarchive_config.yaml

REM ZIP-ассет релиза (dist\PhotoArchive.zip) -- тот же комплект, что раньше планировался для
REM ручного распространения на флешке (exe + пример конфига + вся документация в PDF +
REM лицензии), теперь дополнительно скачивается отдельной кнопкой с сайта (см. index.html) --
REM одно другому не мешает, флешка остаётся отдельным каналом распространения того же файла.
if exist dist\PhotoArchive.zip del /Q dist\PhotoArchive.zip
powershell -NoProfile -ExecutionPolicy Bypass -File package_zip.ps1
if errorlevel 1 (
  echo [ERROR] Упаковка dist\PhotoArchive.zip не удалась -- см. вывод выше.
  exit /b 1
)
echo dist\PhotoArchive.zip собран ^(папка PhotoArchive\ внутри: exe + документация PDF +
echo лицензии + пример конфига^).

echo.
echo Готово: dist\PhotoArchive.exe и dist\PhotoArchive.zip (если сборка прошла без ошибок выше).
echo Перед ПУБЛИКАЦИЕЙ релиза -- см. ..\THIRD_PARTY_LICENSES.md.

endlocal
