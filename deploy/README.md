# Установка на VPS

Монитор устанавливается в `/opt/rent-monitor`, его SQLite-база находится в `/var/lib/rent-monitor`. Для службы создаётся отдельная системная учётная запись `rent-monitor`. Скрипт установки откажется заменять существующий каталог или systemd unit, если они не помечены как принадлежащие этому проекту.

## Установка релиза

Сначала отправьте исходники в приватный репозиторий GitHub. На сервер переносите архив именно опубликованного commit. Распакуйте его в отдельный временный каталог и запустите установщик с SHA этого commit:

```sh
mkdir -p /tmp/rent-monitor-release
tar -xzf /tmp/rent-monitor-<sha>.tar.gz -C /tmp/rent-monitor-release
sudo /tmp/rent-monitor-release/deploy/install.sh /tmp/rent-monitor-release <sha>
```

Установщик создаёт отдельный release-каталог, строго синхронизирует зависимости по `uv.lock`, устанавливает Chromium Playwright, Xvfb, x11vnc, noVNC/websockify и два systemd unit. Если Tailscale ещё не установлен, используется официальный установщик Tailscale. При отсутствии Telegram-токена основной сервис останется выключенным, а локальный CAPTCHA-стек будет готов к настройке.

При обновлении уже работающей установки скрипт сохраняет системный Telegram credential и базу в `/var/lib/rent-monitor`; повторная привязка чата не нужна. Не запускайте второй экземпляр long polling с тем же токеном.

## Настройка отдельного Telegram-бота

Создайте отдельного бота у BotFather. Сохраните выданный токен через интерактивный скрипт. Ввод скрыт и токен не передаётся в аргументах команды:

```sh
sudo /opt/rent-monitor/current/deploy/set-token.sh
sudo systemctl enable --now rent-monitor
```

Выпустите одноразовый код привязки от имени сервисного пользователя и отправьте его боту в личном чате:

```sh
sudo -u rent-monitor env RENT_MONITOR_DATABASE=/var/lib/rent-monitor/rent-monitor.sqlite3 \
  /opt/rent-monitor/current/.venv/bin/rent-monitor pair-code \
  --config /opt/rent-monitor/current/config/search.toml
```

После привязки бот принимает команды `/status`, `/pause` и `/resume`. Pairing code имеет короткий срок действия и может быть использован один раз.

## Приватный доступ к CAPTCHA с iPhone

На VPS выполните вход в тот же tailnet, который используется приложением Tailscale на iPhone:

```sh
sudo tailscale up
sudo tailscale serve --bg http://127.0.0.1:6080
sudo tailscale serve status --json
```

Команда `serve`, в отличие от `funnel`, оставляет адрес доступным только внутри tailnet. Funnel не включайте. Скопируйте выданный HTTPS-адрес без завершающего `/` в root-owned файл:

```sh
sudo sh -c 'umask 077; printf "%s\n" \
  "RENT_MONITOR_CAPTCHA_BASE_URL=https://ИМЯ-УЗЛА.ВАШ-TAILNET.ts.net" \
  > /etc/rent-monitor/environment'
sudo systemctl restart rent-monitor
```

На iPhone установите Tailscale, войдите в тот же tailnet и держите VPN подключённым при открытии ссылки из Telegram. При CAPTCHA бот отправит сообщение с кнопками. Нажмите «Открыть CAPTCHA», откройте одноразовую ссылку, выполните проверку в окне Avito, вернитесь в Telegram и нажмите «Проверить». Ссылка живёт 15 минут и повторно не публикуется в тексте сообщения.

Порты VNC и noVNC не должны быть доступны извне: `x11vnc` слушает только `127.0.0.1:5900`, websockify — только `127.0.0.1:6080`, а HTTPS предоставляет Tailscale Serve с tailnet ACL.

## Проверка CAPTCHA-стека

До запуска `rent-monitor-captcha.service` следующие проверки ожидаемо завершаются ошибкой. После установки проверьте namespace службы и loopback-порты:

```sh
captcha_pid="$(systemctl show -p MainPID --value rent-monitor-captcha.service)"
sudo nsenter -t "$captcha_pid" -m test -S /tmp/.X11-unix/X99
test -d /run/rent-monitor-captcha/tokens
test "$(stat -c %a /run/rent-monitor-captcha/tokens)" = 700
ss -ltn | grep -F '127.0.0.1:5900'
ss -ltn | grep -F '127.0.0.1:6080'
```

Основная служба присоединяется к приватному `/tmp` CAPTCHA-службы через `JoinsNamespaceOf`, поэтому Chromium видит X11 socket, хотя он скрыт из host `/tmp`.

Если источник остановлен не из-за CAPTCHA, а из-за изменившейся разметки, сначала проверьте публичную страницу и обновите код. После установки исправления можно снять сохранённый статус при остановленной службе:

```sh
sudo systemctl stop rent-monitor
sudo -u rent-monitor env RENT_MONITOR_DATABASE=/var/lib/rent-monitor/rent-monitor.sqlite3 \
  /opt/rent-monitor/current/.venv/bin/rent-monitor resume-source yandex \
  --config /opt/rent-monitor/current/config/search.toml
sudo systemctl start rent-monitor
```

Не запускайте эту команду для возобновления сбора, если доступ к публичной странице по-прежнему ограничен.

## Состояние службы

```sh
systemctl status rent-monitor rent-monitor-captcha
journalctl -u rent-monitor -u rent-monitor-captcha -f
systemd-analyze verify /etc/systemd/system/rent-monitor.service
systemd-analyze verify /etc/systemd/system/rent-monitor-captcha.service
ss -ltn
```

Основной сервис ограничен двумя CPU, 4 GiB памяти и 512 задачами. CAPTCHA-стек ограничен одним CPU, 1 GiB и 128 задачами. Оба локальных порта привязаны только к loopback; публичного VNC/noVNC listener нет. Каталоги и unit других проектов установщик не меняет.

Перед обновлением сначала опубликуйте commit в GitHub, затем перенесите его архив на VPS и выполните установку из нового каталога. Старые release-каталоги установщик не удаляет, чтобы сохранить возможность ручного отката. Для полного удаления службы и данных сначала остановите её и удаляйте только перечисленные пути после проверки владельца.
