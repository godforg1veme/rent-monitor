# Постоянное подключение через домашний ПК

VPS запускает основной монитор; домашний ПК обеспечивает выход в интернет. Публичные proxy-порты не нужны. SSH alias `jarvis-vps` должен заранее работать с ключом и проверкой host key.

## Windows

Нужны Python 3 и Windows OpenSSH. В текущей установке репозиторий находится в `F:\rent-monitor`.

```powershell
& F:\rent-monitor\deploy\home-route\install-windows.ps1 -PythonPath python.exe -SshHost jarvis-vps
Start-ScheduledTask -TaskName RentMonitor-HomeRoute
Get-ScheduledTask -TaskName RentMonitor-HomeRoute
Get-Content "$env:LOCALAPPDATA\RentMonitor\home-route\home-route.log" -Tail 15
```

Задача работает скрыто и запускается при входе текущего пользователя без пароля Windows. Supervisor проверяет relay и SSH каждые 10 секунд, перезапускает завершившиеся процессы, записывает PIDs и логи в `%LOCALAPPDATA%\RentMonitor\home-route`. Автоматический сон блокируется на время работы; экран может выключаться.

При переносе уже работающей задачи сначала остановите старый supervisor и дождитесь его завершения. Проверьте command line дочерних PIDs из прежнего `processes.json` и завершите только соответствующие relay/SSH, затем запускайте новую задачу. Не запускайте два relay на одном порту. При обычном перезапуске по прежнему пути supervisor подхватывает проверенные дочерние процессы.

Forward: `ssh -N -R 127.0.0.1:18782:127.0.0.1:18781 jarvis-vps`. Relay допускает CONNECT на порт 443 и слушает только localhost. При ручном сне, выключении или выходе пользователя маршрут может прекратиться.

## Обновление существующего VPS

Сначала нужны установленная служба `rent-monitor`, её пользователь, credentials и рабочая SQLite. Общая первоначальная установка описана в [deploy/README](../README.md). Этот скрипт обновляет существующую службу, сохраняет базу и не сбрасывает baseline.

Создайте архив конкретного коммита `git archive`, добавьте файл `REVISION` с его SHA, передайте на сервер и распакуйте в новый каталог `/opt/rent-monitor/releases/<дата>-<sha>`. Затем:

```sh
sudo bash /opt/rent-monitor/releases/<релиз>/deploy/home-route/install-vps.sh
sudo systemctl is-active rent-monitor
sudo python3 /opt/rent-monitor/current/deploy/home-route/inspect-health.py
sudo journalctl -u rent-monitor -n 30 --no-pager
sudo ss -ltnp 'sport = :18782'
cat /opt/rent-monitor/current/REVISION
```

Скрипт устанавливает основное окружение из экспортированного `uv.lock`, отдельное окружение worker из проверенных версий и внешний [Avito-Parser](https://github.com/mickberrad659-sketch/Avito-Parser) на `2d09315ead7ef1b3498415474f690eea512cf571`. Внешний код не входит в этот Git-репозиторий. Наши `rent_adapter.py` и `home-pow-worker.py` устанавливаются в `/opt/rent-monitor/home-route`; vendor хранится отдельно.

Systemd override выбирает `home-pow`, включает подробности и запускает `python -m rent_monitor.main`. Не используйте `python -m rent_monitor`: у пакета нет `__main__.py`. Runtime worker находится в `/opt`, потому что служба использует `ProtectHome=true`.

Основная база — `/var/lib/rent-monitor/rent-monitor.sqlite3`, приватная диагностика — `/var/lib/rent-monitor/avito-home-pow`. Перед переключением создаётся закрытый backup SQLite и сохраняется предыдущий путь current. Токен и SSH-ключи не входят в архив.

## Проверка и возврат

После одного прохода ожидаются `avito healthy`, свежий last_success, `database_check ok` и отсутствие роста недоставленной очереди. `/status` показывает следующую проверку. Недоступный домашний маршрут повторяется через 60 секунд. SSH keepalive на обеих сторонах освобождает зависший forward.

Для возврата остановите службу, восстановите ссылку `current` из `previous-release.txt`, верните совместимые runtime/override и запустите службу. Базу не откатывайте автоматически: новые объявления и история доставок должны сохраниться. Для возврата к Firefox дополнительно удалите home-route override и восстановите его прежние настройки.

Отключить домашний маршрут: `Stop-ScheduledTask -TaskName RentMonitor-HomeRoute`, затем `Disable-ScheduledTask -TaskName RentMonitor-HomeRoute`. При необходимости завершите только проверенные дочерние PIDs из state-файла.
