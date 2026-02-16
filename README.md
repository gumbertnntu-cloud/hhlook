# hhlook (MVP v3)

Локальное desktop-приложение для macOS (Apple Silicon) для мониторинга и парсинга вакансий hh.ru без официального API.

## Что умеет MVP
- GUI на `PySide6` (`./app`) для настройки и запуска.
- Интерактивная авторизация:
  - приложение открывает headful Chromium,
  - вы логинитесь вручную,
  - сессия сохраняется в `state/state.json`.
- Запуск сбора в режимах:
  - `fast`: только выдача,
  - `deep`: выполняет deep-dive только по отмеченным вакансиям из очереди.
- Ограничения:
  - `max_pages` (например 10),
  - `max_age_days` (по умолчанию 30 дней = 1 месяц).
- Фильтры:
  - include/exclude keywords,
  - морфо-нормализация ключевых слов (RU+EN) для учета склонений/форм слов,
  - optional min salary,
  - поля: `title + company + snippet + description (deep)`.
- SQLite:
  - `vacancies`, `runs`, `run_items`, `changes`.
- Дельты: `new / updated / removed`.
- Отчеты:
  - таблица в GUI,
  - HTML preview: `reports/latest.html`,
  - Excel: `exports/hh_report_*.xlsx` (2 листа: `Общий поиск` и `Deep-dive`).
- Внутренний service mode для `launchd` (`./service-run`).

## Требования
- macOS (Apple Silicon)
- Python `3.11+`
- Chromium for Playwright

## Быстрый старт
```bash
cd /path/to/hhlook
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
chmod +x app service-run
```

Запуск GUI:
```bash
./app
```

Запуск как обычное приложение из Finder:
- Откройте папку проекта
- Дважды кликните `HH Monitor.app`

Если macOS блокирует первый запуск:
```bash
xattr -dr com.apple.quarantine "/path/to/HH Monitor.app"
```
Или откройте через контекстное меню: `Open`.

## Сценарий работы в GUI
1. Нажмите `Авторизоваться`.
2. В открывшемся браузере выполните вход в hh.ru.
3. Дождитесь сообщения в GUI, что сессия сохранена.
4. Задайте настройки:
   - `Целевые позиции (/)`: многострочное поле (до 4+ строк), по одной фразе на строку.
   - `Блокеры (/)`: многострочное поле (до 4+ строк), по одному стоп-слову на строку.
   - `Max pages`
   - `Max age (days)`
   - min salary (опционально)
5. Нажмите `Запустить поиск` для fast-обновления выдачи.
6. В таблице `Найденные вакансии (fast)` отметьте нужные строки тумблерами в колонке `DD`.
7. Нажмите кнопку `↓ deep-dive` под таблицей, чтобы перенести отмеченные строки в `Результат deep-dive`.
8. Deep-dive запускается сразу по кнопке `↓ deep-dive`.
9. После завершения:
   - `Preview HTML` откроет `reports/latest.html`
   - `Export XLSX` сохранит файл в `exports/`.
10. Справа:
   - верхний блок показывает описание вакансии только после клика по строке;
   - нижний блок `Подробности вакансии` показывает структурированный текст с hh.ru.
11. Кнопка `Инструкция` в шапке открывает встроенную памятку по работе с приложением.
12. Очередь deep-dive очищается при каждом новом `Запустить поиск` (fast), чтобы deep-dive всегда соответствовал текущему запуску.

## Где что хранится
- Настройки GUI: `config/settings.json`
- Сессия браузера: `state/state.json`
- База: `data/hh_monitor.db`
- Логи: `logs/app.log` (+ `launchd.out/err.log` при планировщике)
- HTML отчет: `reports/latest.html`
- Excel: `exports/*.xlsx`

## Внутренний service mode (для launchd)
Запуск из терминала:
```bash
./service-run --settings config/settings.json --mode fast --max-pages 10 --max-age-days 30 --export
```

Пример plist: `launchd/com.user.hhmonitor.plist.example`  
Поставьте абсолютные пути и загрузите:
```bash
launchctl unload ~/Library/LaunchAgents/com.user.hhmonitor.plist 2>/dev/null || true
cp launchd/com.user.hhmonitor.plist.example ~/Library/LaunchAgents/com.user.hhmonitor.plist
# отредактируйте абсолютные пути в plist
launchctl load ~/Library/LaunchAgents/com.user.hhmonitor.plist
```

## Обновление app bundle
Если нужно пересобрать launcher:
```bash
./scripts/rebuild_macos_app.sh
```

Если нужно пересобрать с другой PNG-иконкой:
```bash
./scripts/rebuild_macos_app.sh /absolute/path/to/icon.png
```

## Windows: one-file EXE через GitHub Releases
В репозитории настроен workflow:
- `.github/workflows/windows-exe.yml`
- сборочный скрипт: `scripts/build_windows_exe.ps1`

Как получить `HHLook.exe`:
1. Запушьте код в GitHub-репозиторий `hhlook`.
2. Создайте тег вида `v0.1.0` и пушните его:
```bash
git tag v0.1.0
git push origin v0.1.0
```
3. GitHub Actions соберет `dist/HHLook.exe` (one-file) на `windows-latest`.
4. Файл появится в `Actions artifacts` и будет прикреплен к `Release`.

Локальная сборка на Windows:
```powershell
./scripts/build_windows_exe.ps1
```

## Качество
```bash
ruff check src tests
black --check src tests
pytest -q
```

Опционально:
```bash
mypy src
```

## Примечания по устойчивости
- Пагинация идет по `page=` (0-based).
- Параметры `hhtmFrom*` и `searchSessionId` не обязательны.
- Для запросов реализованы retry/backoff на `429/5xx`.
- Между страницами/карточками используется задержка `1..3` сек + jitter.
- Если сессия протухла, нужно заново нажать `Авторизоваться`.
