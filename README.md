# Dark Flasher 1.1

Тёмная прошивалка `.uf2` для nRF52 (nice!nano и совместимые платы).

![Dark Flasher](preview.png)

## Как пользоваться

1. Скачай [архив](https://github.com/MacMillan25/DarkFlasher/releases/latest) и распакуй.
2. Запусти `DarkFlasher\DarkFlasher.exe`.
3. Выбери **роль** ноды (компаньон, репитер, сенсор и т.д.) и, если нужно, нажми **Настройки…** — имя, частота, полоса, SF/CR, мощность, ёмкость батареи, модуль, коэффициент АЦП. Настройки сохраняются в `darkflasher.json` рядом с exe.
4. Нажми **Прошить роль** (или перетащи `.uf2` в окно).
5. Плату переведи в загрузчик — дважды нажми Reset.

Программа сама поймает UF2-диск, запишет файл и после перезагрузки платы отправит настройки по COM-порту. Если плата уже в загрузчике — начнёт сразу.

Если Windows покажет SmartScreen — «Подробнее» → «Выполнить всё равно».

## Где лежат прошивки

- Папка `fw\` рядом с exe: файлы `<env>.uf2`, например `Darktec_companion_radio_ble.uf2`.
- Либо `fw\manifest.json` с адресами:
  ```json
  { "files": { "Darktec_companion_radio_ble": "https://…/файл.uf2" } }
  ```
  Недостающие файлы программа скачает сама.

## Команды для ноды

После прошивки отправляется (по одной, с ответом `OK`):

```
set name …            # если имя задано
set freq 869.900
set bw 62.5
set sf 8
set cr 8
set tx 22
set adc.multiplier 1.750
set radio.module 0..3
set darktec.capacity 1000..10000
```

`freq/bw/sf/cr/tx` применяются после перезагрузки ноды — она перезагрузится сама при следующем включении; если нужно сразу, отправь `reboot`.

## Сборка

`build.cmd` — соберёт `dist\DarkFlasher\DarkFlasher.exe` (Python 3.10+, PyInstaller, pyserial).
Из исходников — `Запуск.bat`.

## Лог

`darkflasher.log` рядом с `DarkFlasher.exe`.
