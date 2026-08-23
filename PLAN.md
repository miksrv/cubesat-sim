# PLAN: LoRa-канал на Heltec WiFi LoRa 32 V4 + Meshtastic

Рабочий документ. Цель — заменить непроверенный LoRa-канал на 52Pi IoT Node(A)
(I2C-мост SC16IS752, пункт G10 в `ROADMAP.md`) на Heltec WiFi LoRa 32 V4 со
стоковой прошивкой Meshtastic, подключённый к Raspberry Pi по UART.

**Дата составления:** 23 августа 2026
**Статус:** этапы 1–4 выполнены 23 августа 2026 — канал Pi ↔ Heltec ↔ эфир
поднят и проверен в обе стороны. Дальше — этап 5 (Python-прототип) и этап 6
(правки в репозитории).

Детали этапов 1–2: На плате Meshtastic
`2.7.26.54e0d8d` (`pioEnv: heltec-v4`), регион `US`, радио проверено —
пакет принят на сторонней ноде, `nodedbCount: 3` (приём тоже работает),
Serial-модуль включён в `PROTO` на `GPIO43/44`, `BAUD_115200`.
Дальше — этап 3 (подготовка Raspberry Pi).

---

## Железо

| Что | Значение |
|---|---|
| Плата | Heltec WiFi LoRa 32 V4 (ESP32-S3R2 + SX1262) |
| Прошивка | Meshtastic, **≥ 2.7.20** (более старые сборки V4 не поддерживают) |
| Регион | `US` — 915 МГц (плата в 915-версии) |
| Modem preset | `LONG_FAST` (дефолт) — должен совпадать на приёмных нодах |
| Подключение к Pi | UART через IO Expansion HAT DFR0566 (Gravity-разъём = прямой вывод пинов Pi, 3.3 В) |
| Скорость UART | 115200 (`BAUD_115200`) |

### Распиновка

| HAT (DFR0566) | Pi BCM | Heltec V4 | Пин платы |
|---|---|---|---|
| `T` (TX) | GPIO14, физ. 8 | `U0RXD` / `GPIO44` | **J2, пин 5** |
| `R` (RX) | GPIO15, физ. 10 | `U0TXD` / `GPIO43` | **J2, пин 6** |
| `G` (GND) | GND | GND | **J2, пин 1** (или J3 пин 1) |
| 5V | 5V | 5V (питание) | **J2, пин 2** |

TX↔RX **перекрёстно**. Земля обязательно общая. Согласователь уровней не нужен —
обе стороны на 3.3 В.

Нумерация J2: **пин 1 — ближайший к USB-разъёму**, дальше вверх до 18. Паять
надёжнее по шелкографии (`43`, `44`), а не считая отверстия. Пины 3 и 4 (`Ve`) —
выход Vext, не вход питания; на `3V3` питание не подавать.

### Распайка (фактическая, 23 августа 2026)

| Цвет | Heltec | Пин J2 | → HAT DFR0566 | Pi |
|---|---|---|---|---|
| зелёный | `U0TXD` / `GPIO43` | 6 | `R` | GPIO15, физ. 10 |
| синий | `U0RXD` / `GPIO44` | 5 | `T` | GPIO14, физ. 8 |
| чёрный | `GND` | 1 | `-` | GND |
| красный | `5V` | 2 | отдельный пин `5V` | 5V |

**`+` на UART-разъёме HAT — это 3.3 В, а не 5 В** (по вики DFR0566: «3.3V
Positive»). Красный провод только на отдельный `5V`-пин, иначе Heltec получит
3.3 В на вход своего регулятора.

На время отладки красный лучше не подключать вовсе и питать Heltec по USB:
исключает просадки 5 В при передаче и оставляет доступной USB-консоль. 5 В от Pi
и USB одновременно не подавать — пин `5V` связан с USB VBUS.

**Занято на Heltec, не трогать:** `GPIO8–14` — SX1262 (NSS 8, SCK 9, MOSI 10,
MISO 11, RST 12, BUSY 13, DIO1 14); `GPIO17/18/21` — OLED; `GPIO38–42` + `GPIO34` —
разъём GNSS; `GPIO19/20` — USB; `GPIO2`, `GPIO7`, `GPIO46` — управление FEM
(усилитель); `GPIO36` — Vext_Ctrl; `GPIO37` — ADC_Ctrl; `GPIO1` — замер VBAT.
Свободная альтернатива для второго UART: `GPIO47`/`GPIO48` (J2, пины 13/14).

### Антенна

Прикручивать **до первой подачи питания**: что лежит на плате с завода —
неизвестно, а после `--set lora.region US` нода начинает передавать сама
(рассылает node info), без отдельной команды. Антенна должна быть на 915 МГц.
Затягивать от руки; если разъём U.FL — жать строго вертикально.

---

## Этап 1 — Прошивка (MacBook, USB)

- [x] Открыть web-флешер Meshtastic в **Chrome/Edge** (Safari не поддерживает
      Web Serial — плата не появится в списке)
- [x] Прошить сборку ≥ 2.7.20 для target `heltec-v4`. **Обязательно
      «Full erase and install», а не «Update»** — см. «Известные грабли»
- [x] Порт на macOS: `ls /dev/cu.usbmodem*` → `/dev/cu.usbmodemNNN`
      (нативный USB, не `usbserial` — CP2102 на V4 нет). Имя порта меняется
      между перепрошивками: было `usbmodem101`, стало `usbmodem1101`

Вариант таргета выбирать строго `heltec-v4`: в сборке есть ещё `heltec-v4-r8-oled`,
`heltec-v4-r8-tft` и `heltec-v4-tft`. `-r8` — это ESP32-S3**R8** с 8 МБ octal PSRAM,
а на плате S3**R2** (флешер сам печатает `Embedded PSRAM 2MB (AP_3v3)`).

## Этап 2 — Настройка по USB (MacBook)

```bash
brew install pipx && pipx ensurepath && exec $SHELL
pipx install "meshtastic[cli]"

PORT=/dev/cu.usbmodem101   # подставить свой

meshtastic --port $PORT --info
meshtastic --port $PORT \
  --set position.gps_mode NOT_PRESENT \
  --set lora.region US
```

`position.gps_mode NOT_PRESENT` — для голой платы без GNSS-модуля: `DISABLED`
означает «модуль есть, но выключен», а `NOT_PRESENT` — «железа нет», и прошивка
перестаёт его опрашивать. `position.gps_enabled` — легаси-флаг, не использовать.

**Экран отключать нечем и не нужно:** в `display` есть только `screen_on_secs`,
`oled`, `displaymode`, `flip_screen` — переключателя вкл/выкл нет. Прошивка сканирует
I2C при старте, не находит дисплей и работает headless; строка про отсутствие
display в логе — норма, а не ошибка.

**Сначала проверить радио, потом провод** — иначе при неудаче непонятно, что
виновато: радиолинк или UART.

```bash
meshtastic --port $PORT --sendtext "test from macbook"
```

- [x] Пакет принят на другой Meshtastic-ноде

Приёмные ноды должны совпадать по региону (`US`), modem preset (`LONG_FAST`) и
каналу с его PSK. Дефолтный канал `LongFast` с дефолтным ключом сойдётся сам;
если канал/ключ когда-то меняли — привести к одному виду.

Затем включить Serial-модуль (одной пачкой — каждый `--set` вызывает сохранение
и перезагрузку):

```bash
meshtastic --port $PORT \
  --set device.role CLIENT \
  --set network.wifi_enabled false \
  --set serial.enabled true \
  --set serial.mode PROTO \
  --set serial.rxd 44 \
  --set serial.txd 43 \
  --set serial.baud BAUD_115200
```

- [x] Serial-модуль включён (проверено: `overrideConsoleSerialPort: false`,
      `bluetooth.enabled: true` — USB-API и путь назад целы)

**Bluetooth пока НЕ выключать** — это путь назад. USB-API от включения
Serial-модуля не ломается (`override_console_serial_port` остаётся `false`),
откатиться можно всегда.

Почему именно так: у V4 нативный USB, protobuf-API живёт на USB CDC, а пины
`GPIO43/44` в стоковой конфигурации молчат. API на них поднимает только
Serial-модуль в режиме `PROTO` — в `src/modules/SerialModule.cpp` это
`Serial2.begin(baud, SERIAL_8N1, rxd, txd)` без проверки диапазона пинов
(документное «rxd 1–39, txd 1–33» — легаси классического ESP32), а сам класс —
`SerialModule : StreamAPI(&Serial2)`. Флаг `override_console_serial_port` здесь
не помощник: он работает только с режимами NMEA и CALTOPO.

`BAUD_115200` не опционально: дефолт модуля 38400, а Python-библиотека
Meshtastic открывает порт жёстко на 115200
(`serial.Serial(dev_path, 115200, ...)` в `serial_interface.py`).

## Этап 3 — Подготовка Raspberry Pi

```bash
sudo raspi-config     # Interface Options → Serial Port: login shell = No, hardware = Yes
# /boot/firmware/config.txt:  enable_uart=1  и  dtoverlay=disable-bt
sudo systemctl disable hciuart
sudo apt install -y pipx && pipx ensurepath
pipx install "meshtastic[cli]"
sudo reboot
ls -l /dev/serial0    # должен указывать на ttyAMA0
```

- [x] `/dev/serial0` → `ttyAMA0`

**Фактически на Pi 4 (Bookworm, ядро 6.12) потребовалось ещё два действия,
которых в плане не было:**

1. **Убрать `console=serial0,115200` из `/boot/firmware/cmdline.txt`.**
   Консоль ядра висела на том же UART и сыпала в него текст. Симптом:
   `--info` падает с `device reports readiness to read but returned no data
   (device disconnected or multiple access on port?)`. `console=tty1` оставить —
   это путь к консоли, если Pi не поднимет сеть
2. **Убрать `dtoverlay=sc16is752-i2c`** — остаток от 52Pi IoT Node(A).
   Железа нет, устройств `ttySC*` не создавалось

`systemctl disable hciuart` на этом образе не нужен — юнита нет
(`not-found`), Bluetooth поднимается через `bluetooth.service`, а после
`dtoverlay=disable-bt` HCI-адаптер просто не появляется.

Проверка, что PL011 отдан на GPIO14/15:
```
$ ls -l /dev/serial0            # → ttyAMA0
$ dmesg | grep ttyAMA
fe201000.serial: ttyAMA0 at MMIO 0xfe201000 (irq = 40, base_baud = 0) is a PL011 AXI
```
До правок тот же `fe201000.serial` был занят Bluetooth (`hci_uart_bcm serial0-0`),
а `serial0` указывал на `ttyS0` — mini-UART.

CLI на Pi поставлен без sudo, в venv: `python3 -m venv ~/mesh-venv` +
`~/mesh-venv/bin/pip install "meshtastic[cli]"` (версия 2.7.11 при прошивке
2.7.26 — работает).

`disable-bt` нужен, чтобы `serial0` был полноценным PL011, а не mini-UART,
у которого скорость зависит от частоты ядра. Конфликта с GPS нет: 52Pi выведен
из схемы, новый GNSS TEL0157 идёт по I2C.

## Этап 4 — Переезд на UART

- [x] Отключить USB, подключить питание/GND/TX/RX по таблице выше
- [x] `meshtastic --port /dev/serial0 --info` — вернул конфиг (298 строк)
- [x] `meshtastic --port /dev/serial0 --sendtext "hello from raspberry"` — принято на другой ноде (`implicit ACK` + подтверждение приёма)
- [x] Только теперь: `meshtastic --port /dev/serial0 --set bluetooth.enabled false`
      — заодно подтвердило, что запись конфига через UART работает, а не только
      чтение. В `--info` проверять поле `bluetooth.enabled`, а не
      `Metadata.hasBluetooth`: последнее — признак наличия железа, он остаётся
      `true`

`bluetooth.enabled` — это BT самого Heltec; `dtoverlay=disable-bt` — это BT
Raspberry Pi. Разные вещи.

## Этап 5 — Python-скрипт

- [x] Скрипт на `meshtastic.serial_interface.SerialInterface('/dev/serial0')`:
      отправка + приём через колбэк `onReceive`. Это прототип того, на что ляжет
      переписанный `src/comms/lora.py`.

Проверено 23 августа 2026 в обе стороны: Pi отправила `CLAUDE-TEST-1`, вторая нода
приняла; ответ `"Working, thanks!"` от `!698204b0` пришёл на Pi через колбэк
(SNR 6.0 дБ). Подписка — `pub.subscribe(on_receive, "meshtastic.receive")`,
текстовые пакеты отбираются по `decoded["portnum"] == "TEXT_MESSAGE_APP"`,
сам текст в `decoded["text"]`, отправитель в `fromId`.

Замечания по полям пакета: `rxRssi` может отсутствовать (приходит `None`) —
опираться на `rxSnr`; `hopStart` — это исходный лимит хопов, а не число
пройденных. Сам прототип живёт на Pi в `/tmp/mesh_listen.py` и в репозиторий
пока не перенесён.

## Этап 6 — Изменения в репозитории

- [ ] `src/comms/lora.py` — переписать `LoRaModule` поверх `meshtastic`
      (не `smbus2` и не сырой `pyserial`). Своё кадрирование
      `length + payload + CRC-16-CCITT` больше не нужно: Meshtastic сам делает
      кадрирование, CRC, ретраи и шифрование
- [ ] `src/common/config.py` + `config/config.yaml` — убрать `LORA_I2C_ADDRESS`,
      добавить `LORA_PORT` (`/dev/serial0`), `LORA_BAUDRATE`, таймаут
- [ ] **Размер пакета.** Serial-модуль Meshtastic — до 240 байт на сообщение,
      а агрегированный пакет COMMS (eps + adcs + payload + system) — сотни байт.
      Сейчас `lora.py:33` молча режет payload до 28 байт, то есть
      `service.py:293` отправляет обрезанный мусор. Решить до включения
      `COMMS_LORA_ENABLED=1`: компактный beacon-набор полей **или** чанкование
- [ ] `requirements.txt` — добавить `meshtastic`; моки в `tests/conftest.py`
- [ ] `tests/test_comms_lora.py` — сейчас мокает `smbus`, переделать
      (образец подхода — `tests/test_common_gps_a9g.py`)
- [ ] `crc16_ccitt()` в `src/common/utils.py` теряет потребителя — решить,
      удалять или оставить
- [x] `docs/hardware-heltec-lora32-v4.md` — новый файл; строка в таблице
      Hardware в `README.md`
- [ ] `ROADMAP.md` — переформулировать G10: проверка регистров SC16IS752
      больше не актуальна
- [ ] Для двустороннего канала на земле тоже нужен SX1262-приёмник,
      подключённый к ground station

---

## Известные грабли

- **Первый `--info` через UART может вернуть пусто.** Наблюдалось на Pi: первый
  запуск завершился с кодом 0 и без единой строки, повторный сразу отдал полный
  конфиг. Похоже на тот же мусор в начале потока (см. ниже) — CLI не успевает
  пересинхронизироваться. В своём коде предусмотреть повтор
- **«Update» вместо «Full erase and install»** — на этом потеряли первый подход.
  Web-флешер в режиме Update пишет только `firmware-heltec-v4-<ver>.bin`
  (2117408 байт) по адресу `0x10000`, то есть один раздел `app0`, а бутлоадер
  (`0x0`) и таблицу разделов (`0x8000`) оставляет заводские, от прошивки Heltec.
  Разметка не совпадает с ожидаемой (`partitionScheme: 16MB`, `nvs` на `0x9000`,
  `spiffs` на `0xc90000`), приложение не находит своих разделов и уходит
  в `esp_restart()`. Симптом — ROM-лог по кругу с `rst:0x3 (RTC_SW_SYS_RST)`
  и ни одной строки от Meshtastic. Лечится полным стиранием и записью
  `.factory.bin` с нуля. Признак успешного старта: ROM-лог проходит **один раз**,
  дальше идут строки Meshtastic
- **Метрики в `--info` устаревшие.** `airUtilTx`/`uptimeSeconds` в `deviceMetrics`
  берутся из записи nodedb и обновляются только когда нода рассылает телеметрию
  (дефолт — раз в 30 минут). Проверять по ним факт передачи бесполезно; факт TX
  подтверждается приёмом на другой ноде либо живым логом (`--listen`, `--noproto`)
- **Регион.** По умолчанию `lora.region = UNSET`, и в этом состоянии плата
  не передаёт ни одного пакета. Самая частая причина «прошил, а тишина»
- **Питание.** Пики тока при передаче на 915 МГц могут просаживать 5 В от Pi
  и ресетить Heltec. Симптом — перезагрузки платы. Лечение: электролит
  100–470 мкФ по питанию Heltec либо отдельное питание по USB на время отладки
- **Мусор в начале.** До инициализации Serial-модуля TX-пин Heltec висит
  в плавающем состоянии, плюс ROM-загрузчик пишет бут-лог на те же `GPIO43/44`.
  CLI пересинхронизируется по заголовку кадра, в своём коде это надо учесть
- **Неверная скорость** — `--info` просто зависает на таймауте, без внятной ошибки
- **Маркировка `T`/`R` на HAT** — сверить по шелкографии или прозвонить на
  физические пины 8/10: цветовую раскладку Gravity-кабеля DFRobot документирует
  непоследовательно

## Ссылки

- [Heltec WiFi LoRa 32 V4 wiki](https://wiki.heltec.org/docs/devices/open-source-hardware/esp32-series/lora-32/wifi-lora-32-v4/)
- [V4 Pin Map](https://resource.heltec.cn/download/WiFi_LoRa_32_V4/Pinmap/V4_pinmap.png)
- [Meshtastic Serial Module](https://meshtastic.org/docs/configuration/module/serial/)
- [Meshtastic LoRa Region config](https://meshtastic.org/docs/configuration/radio/lora/)
- [Meshtastic Python CLI](https://meshtastic.org/docs/software/python/cli/installation/)
- [SerialModule.cpp](https://github.com/meshtastic/firmware/blob/master/src/modules/SerialModule.cpp)
- [DFRobot DFR0566 wiki](https://wiki.dfrobot.com/dfr0566/)
