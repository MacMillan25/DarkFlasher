# Dark Flasher 1.0

Тёмная прошивалка `.uf2` для nRF52 (nice!nano и совместимые платы).

![Dark Flasher](preview.png)

## Как пользоваться

1. Скачай [архив](https://github.com/MacMillan25/DarkFlasher/releases/latest) и распакуй.
2. Запусти `DarkFlasher\DarkFlasher.exe`.
3. Перетащи `.uf2` в окно.
4. Плату переведи в загрузчик — дважды нажми Reset.

Программа сама поймает UF2-диск и запишет файл. Если плата уже в загрузчике — начнёт сразу.

Если Windows покажет SmartScreen — «Подробнее» → «Выполнить всё равно».

## Сборка

`build.cmd` — соберёт `dist\DarkFlasher\DarkFlasher.exe` (Python 3.10+, PyInstaller).
Из исходников — `Запуск.bat`.

## Лог

`darkflasher.log` рядом с `DarkFlasher.exe`.
