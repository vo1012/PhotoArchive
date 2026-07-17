# Что положить в `bin/` перед сборкой

Эта папка пуста в репозитории — перед запуском `build\build.bat` положите сюда 6 файлов и
одну папку (все — официальные Windows-сборки, скачиваются с сайтов проектов вручную; версии
не фиксирую намеренно — берите актуальные на момент сборки):

| Файл           | Откуда взять (официальный сайт проекта) | Примечание |
|----------------|------------------------------------------|------------|
| `exiftool.exe` + `exiftool_files\` | exiftool.org, раздел Windows Executable  | Скачивается как `exiftool(-k).exe` — переименуйте в `exiftool.exe`. Начиная с версий ~12.90 архив содержит ещё папку `exiftool_files\` (встроенный `perl.exe` и библиотеки) — она **обязательна**, `exiftool.exe` без неё не запустится. Perl отдельно ставить не нужно, но саму папку нужно положить рядом с `exiftool.exe` |
| `ffmpeg.exe`   | **LGPL-сборка** (см. предупреждение о лицензии ниже), например тег `lgpl` у [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases) | Static build, папка `bin\` внутри архива |
| `ffprobe.exe`  | тот же архив, что и `ffmpeg.exe`          | Идёт в комплекте с ffmpeg |
| `7z.exe`       | 7-zip.org → Download → 7-Zip Extra (консольная версия `7za`/`7zr`) или полный установщик, `7z.exe`+`7z.dll` из папки установки | Нужна именно консольная `7z.exe`, не только GUI |
| `7z.dll`       | тот же источник, что `7z.exe`             | `7z.exe` без неё не работает |
| `UnRAR.exe`    | rarlab.com → Downloads → UnRAR (freeware console version) | Официальная бесплатная консольная утилита распаковки RAR |

После того как всё на месте:

```
bin\
  exiftool.exe
  exiftool_files\   (вся папка целиком, как из архива)
  ffmpeg.exe
  ffprobe.exe
  7z.exe
  7z.dll
  UnRAR.exe
```

можно запускать `build\build.bat` — он бандлит их в `PhotoArchive.exe` через
`--add-binary`/`--add-data`, конечному пользователю ставить/скачивать их отдельно не
придётся.

Если запускаете `photosort_win.py` напрямую через `python3` (не собранный exe) для
разработки/теста — можно обойтись без этой папки, если `exiftool`/`ffmpeg`/`ffprobe`/`7z`/
`unrar` уже стоят в системе и есть на PATH.

## Лицензии — обязательно перед публикацией `.exe`

Ни один из этих пяти бинарников не лицензирован так же, как сам `photosort_win.py`
(Apache License 2.0) — при публикации собранного `.exe` как GitHub Release нужно приложить
их лицензии. Подробности и требования по каждому компоненту —
[`../THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md).
Коротко:

- **ffmpeg — берите именно LGPL-сборку**, не "full"/"essentials" (те обычно собраны с
  `--enable-gpl` и требуют source-offer при распространении). Например тег `lgpl` (не
  `gpl`) у [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases).
- В отличие от `bin/` выше (который пуст в репозитории и наполняется перед каждой сборкой
  заново), лицензионные тексты этих бинарников уже лежат закоммиченными в
  [`../licenses/`](../licenses/) на верхнем уровне репозитория (`licenses/exiftool/`,
  `licenses/ffmpeg/`, `licenses/7zip/`, `licenses/unrar/`) — обновлять их нужно только если
  меняется сама лицензия версии бинарника, которую вы положили в `bin/`, не при каждой
  сборке. `build\build.bat` копирует `../licenses/` целиком в `build\dist\licenses\`.
