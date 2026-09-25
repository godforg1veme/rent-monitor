# Установка на VPS

Монитор устанавливается в `/opt/rent-monitor`, его SQLite-база находится в `/var/lib/rent-monitor`. Для службы создаётся отдельная системная учётная запись `rent-monitor`. Скрипт установки откажется заменять существующий каталог или systemd unit, если они не помечены как принадлежащие этому проекту.

## Установка релиза

Сначала отправьте исходники в приватный репозиторий GitHub. На сервер переносите архив именно опубликованного commit. Распакуйте его в отдельный временный каталог и запустите установщик с SHA этого commit:

```sh
mkdir -p /tmp/rent-monitor-release
tar -xzf /tmp/rent-monitor-<sha>.tar.gz -C /tmp/rent-monitor-release
sudo /tmp/rent-monitor-release/deploy/install.sh /tmp/rent-monitor-release <sha>
```

Установщик создаёт отдельный release-каталог, виртуальное окружение и systemd unit. При отсутствии токена он подготовит файлы и оставит службу выключенной.

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

Если сайт приостановил источник из-за CAPTCHA или отказа в доступе, сначала проверьте обычную публичную страницу и обновите код источника. После публикации и установки исправления снимите сохранённый статус при остановленной службе, затем запустите её снова:

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
systemctl status rent-monitor
journalctl -u rent-monitor -f
```

Сервис не открывает входящие порты. Systemd ограничивает его до 50% одного CPU, 1 GiB памяти, 64 задач и IOWeight 10. Каталоги и unit других проектов установщик не меняет.

Перед обновлением сначала опубликуйте commit в GitHub, затем перенесите его архив на VPS и выполните установку из нового каталога. Старые release-каталоги установщик не удаляет, чтобы сохранить возможность ручного отката. Для полного удаления службы и данных сначала остановите её и удаляйте только перечисленные пути после проверки владельца.
