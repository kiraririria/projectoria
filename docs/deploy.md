# Публикация на Vercel

## Первый деплой

1. Выполните `python scripts/build_index.py --check`. Проверьте, что в публикуемой ветке есть `knowledge/index.json` и `knowledge/vectors.json`. Пересоберите векторы, если изменили документы или модель эмбеддингов.
2. В Vercel откройте **Add New… → Project**, импортируйте `kiraririria/projectoria` и оставьте корень репозитория как **Root Directory**. Выберите **FastAPI** и оставьте команды сборки по умолчанию. Vercel загрузит приложение из `app/server.py`, а `.python-version` задаст Python 3.13. [Vercel Dashboard](https://vercel.com/dashboard), [репозиторий](https://github.com/kiraririria/projectoria), [FastAPI на Vercel](https://vercel.com/docs/frameworks/backend/fastapi)
3. Запустите первый деплой с пустым `OPENROUTER_API_KEY`. Проверьте, что открываются главная страница и библиотека документов. В `GET /api/health` ожидаются `status: "ok"`, `semantic_index: true` и `generation_configured: false`. Для анонимного Production и закрытых Preview используйте **Settings → Deployment Protection → Standard Protection** с **Vercel Authentication**. Для проверки извне берите постоянный Production-домен проекта. [Deployment Protection](https://vercel.com/docs/deployment-protection)

На Hobby Git-деплой ограничен правами владельца команды и типом репозитория. Например, приватный репозиторий организации GitHub нельзя импортировать в Hobby. Сверьте доступ и план перед импортом. [Ограничения Git-деплоя](https://vercel.com/docs/git#using-hobby-teams)

Укажите `main` в качестве Production Branch. После подключения GitHub коммиты в `main` обновляют Production, а коммиты в других ветках и pull request получают Preview. Ключ Production доступен только деплоям Production. [Интеграция Vercel с GitHub](https://vercel.com/docs/git/vercel-for-github)

### Что проверено локально

Сборщик `@vercel/python@33.0.0` обнаружил `app.server:app` и собрал функцию для Linux x86_64 с Python 3.13, лимитом 60 секунд и поддержкой потоковых ответов. Пакет около 15 МБ в распакованном виде содержит зависимости из `requirements.txt`, статику и оба индекса. Работа на публичном домене проверяется после деплоя.

Статика объёмом около 111 КБ обслуживается через FastAPI. Верхнеуровневый middleware отключает автоматический перенос таких файлов в CDN. В отдельной локальной пробе настройка CDN через минимальный `pyproject.toml` переключила сборщик на новый манифест зависимостей и пакет потерял зависимости из `requirements.txt`. Текущая конфигурация включает статику в функцию и сохраняет проверенный набор зависимостей. [Правила статики Vercel](https://vercel.com/docs/frameworks/backend/fastapi#middleware)

## Ограничение запросов

1. Откройте проект → **Firewall → Configure → New Rule**. Укажите условия **Request Path Equals** `/api/chat` и **Method Equals** `POST`, объединённые через **AND**.
2. Выберите **Rate Limit → Fixed Window**, ключ **IP Address**, **10 запросов за 60 секунд**, ответ **429 Too Many Requests**. Это начальная настройка для демо. Проверьте цену в диалоге Vercel при создании первого правила. [WAF Rate Limiting](https://vercel.com/docs/vercel-firewall/vercel-waf/rate-limiting)
3. Нажмите **Save Rule → Review Changes → Publish**. До добавления ключа проверьте, что Firewall показывает совпадения по правилу. Можно отправить с одного IP до 11 последовательных POST с телом `{}` в пределах окна. Пустой запрос не вызывает OpenRouter. После порога ожидается 429. [Условия WAF](https://vercel.com/docs/vercel-firewall/vercel-waf/rule-configuration)

На Hobby доступно одно правило Rate Limit на проект. Счётчики Vercel ведутся по регионам, а один IP может принадлежать нескольким студентам за общей сетью. Ограничение защищает вход в функцию. Общий расход контролирует выделенный ключ OpenRouter. [Ограничения WAF](https://vercel.com/docs/vercel-firewall/vercel-waf/rate-limiting#limits)

## Подключение OpenRouter

В [OpenRouter → API Keys](https://openrouter.ai/settings/keys) создайте отдельный ключ для Production и задайте лимит расходов в долларах. Для одноразовой демонстрации можно выбрать лимит без автоматического сброса. [Лимиты ключей OpenRouter](https://openrouter.ai/docs/api/api-reference/api-keys/create-keys)

В настройках проекта откройте **Environment Variables**, добавьте `OPENROUTER_API_KEY` с типом **Secret** (прежнее название **Sensitive**) только для **Production**. Для остальных переменных используйте **Config**, если меняете значения из `.env.example`. Добавление или изменение переменных вступает в силу после **Deployments → Redeploy**. [Типы переменных Vercel](https://vercel.com/docs/environment-variables/sensitive-environment-variables), [обновление значений](https://vercel.com/docs/environment-variables/managing-environment-variables)

| Переменная | Значение |
|---|---|
| `OPENROUTER_API_KEY` | Выделенный ограниченный ключ. Нужен для генерации. |
| `OPENROUTER_CHAT_MODEL` | По умолчанию `deepseek/deepseek-v4.1-flash`. Нужна поддержка JSON-ответа. |
| `OPENROUTER_EMBEDDING_MODEL` | По умолчанию `baai/bge-m3`, модель должна совпадать с векторным индексом. |
| `OPENROUTER_RERANK_MODEL` | По умолчанию пусто. Задаётся при включении внешнего реранжирования. |

## Проверка Production

1. Откройте Production в приватной вкладке. Проверьте, что интерфейс загружается без входа, стили и шрифты отображаются, библиотека открывает фрагменты.
2. Откройте `/api/health`, проверьте значения `generation_configured`, `semantic_index` и модель в `chat_model`. Работоспособность подключения проверяется запросом чата.
3. Задайте вопрос «Чем отличаются Product Goal и Sprint Goal?», проверьте ссылки и текст источников. Повторите проверку из репозитория одной командой, заменив домен на свой. Запрос использует лимит ключа.

   ```powershell
   python -m evaluation.run --url "https://ВАШ-PRODUCTION-ДОМЕН" --case scrum-goals
   ```

4. Посмотрите результаты правила в **Firewall**, длительность функции и ошибки в **Logs**, расход по ключу в OpenRouter. При необходимости экстренно прекратить платные запросы отзовите ключ в OpenRouter. Смена переменной в Vercel действует на новые деплои. [Логи Vercel](https://vercel.com/docs/logs/runtime)

## Учёт токенов в журнале

По одному `request_id` можно найти вызовы OpenRouter, сделанные для ответа в чате. Этот ID также возвращается в заголовке `X-Request-ID` потокового ответа. События `chat_stage` описывают этапы, `chat_provider_call` — обращения к провайдеру, `chat_total` — итог запроса. Поле `operation` различает основной ответ (`answer`), повтор после сбоя (`retry`), запрос с дополнительными источниками (`expanded_search`) и исправление черновика (`repair`). Отдельного редакторского вызова нет.

`estimated_input_tokens` — приблизительная оценка до отправки. Она берётся из размера всех сообщений в UTF-8 (около четырёх байт на токен), поэтому включает системный промпт, вопрос, контекст и источники, если они вошли в запрос. Для контроля расхода используйте фактические `prompt_tokens`, `completion_tokens` и `total_tokens` в событии `chat_provider_call`: они берутся из `usage` OpenRouter. Если счётчики не пришли, эти поля отсутствуют; нули в итоговом событии не подтверждают нулевой расход. `requested_model` и `model` различают запрошенную и фактическую модель. Семантический поиск записывается отдельно на этапе `embedding`. [Учёт токенов OpenRouter](https://openrouter.ai/docs/cookbook/administration/usage-accounting)

`OPENROUTER_MAX_INPUT_TOKENS` ограничивает локальную оценку входа для каждого вызова чат-модели. Значение по умолчанию — 6 000, отдельно от ограничения на генерацию. Если запрос не помещается, сначала убираются нижние по рейтингу необязательные источники целиком, затем более раннее из двух уточнений. Если этого мало, сервер пробует убрать прошлый ответ и последнее уточнение. Текущий вопрос, начало темы, первый источник и источники уже проверенных блоков сохраняются. Если даже они не вмещаются, вызов не отправляется; ошибка получает код `input_token_limit`. Этап `input_budget` в журнале показывает число удалённых реплик и фрагментов. Локальная оценка может отличаться от счётчика модели. Это ограничение одного вызова, а не лимит расходов ключа OpenRouter.
