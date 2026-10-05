---
applyTo: "src/rent_monitor/**,deploy/home-route/**"
---

# Рабочие знания домашнего маршрута

Текущее устройство: README.md и docs/home-route-status.md. Backend home-pow, VPS → SSH loopback 18782 → Windows CONNECT loopback 18781 → Авито. Внешний Avito-Parser закреплён на 2d09315ead7ef1b3498415474f690eea512cf571 и хранится вне Git.

HTML HTTP 439 может не содержать JSON challenge. Проверяйте cookie pow_challenge именно домена Авито и повторяйте URL в той же сессии после подтверждённого PoW. Успешная выдача не подтверждает доступ к карточкам: проверяйте выдачу, подробности и доставку отдельно.

HomeRouteUnavailable означает повтор через 60 секунд; не применяйте часовой backoff к отключённому домашнему ПК. Worker должен иметь deadline и завершаться при зависании. Не сбрасывайте baseline или outbox при развёртывании.

Правильный запуск модуля: python -m rent_monitor.main. У пакета нет __main__.py, поэтому python -m rent_monitor не работает. ProtectHome=true запрещает runtime из /home/deploy: зависимости worker должны находиться в /opt/rent-monitor/home-route.

Не коммитьте внешние исходники, базы, живые listing JSON, токены или challenge diagnostics. Не заявляйте живую проверку QRATOR/GeeTest на основании unit-тестов. Фото и видео сейчас не отправляются.
