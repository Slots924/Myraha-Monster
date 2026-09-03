# Myraha Monster

Яскрава панель керування коментарями Facebook Pages. Вона синхронізує доступні
сторінки через System User token, підписує вибрані сторінки на поле webhook
`feed`, зберігає коментарі в SQLite і може автоматично приховувати нові
коментарі з можливістю повернути їх назад.

## Запуск

```powershell
python index.py
```

Панель відкриється на `http://localhost:80`. Якщо порт зайнятий, зміни `PORT`
у `.env` і перезапусти ngrok на тому самому порту.

Зовнішніх Python або JavaScript-залежностей немає. База створюється автоматично
у `myraha.db`; файл виключений з Git.

## Налаштування `.env`

```dotenv
FACEBOOK_APP_SECRET=...
FACEBOOK_VERIFY_TOKEN=...
SYSTEM_USER_TOKEN=...
GRAPH_API_VERSION=v24.0
PORT=80
NGROK_PUBLIC_URL=https://your-tunnel.ngrok-free.dev
WEBHOOK_CALLBACK_URL=https://your-tunnel.ngrok-free.dev/webhook
DASHBOARD_PASSWORD=use-a-long-random-password
NGROK_PATH=C:\Users\YourName\Programs\ngrok.exe
NGROK_TOKEN=your-ngrok-authtoken
```

Старе локальне ім'я `SUSTEM_USER_TOKEN` теж підтримується, але для нових
конфігурацій використовуй правильне `SYSTEM_USER_TOKEN`.

## Швидкий запуск

Запустити застосунок і ngrok разом:

```powershell
.\start-all.ps1
```

Запустити тільки ngrok:

```powershell
.\start-ngrok.ps1
```

Скрипти читають `NGROK_PATH` і `NGROK_TOKEN` з `.env`; токен у консоль не
виводиться. Якщо PowerShell одноразово блокує локальні скрипти, запускай так:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-all.ps1
```

Якщо `GET /me/accounts` не повертає всі сторінки, додай один із параметрів:

```dotenv
FACEBOOK_BUSINESS_ID=123456789
FACEBOOK_PAGE_IDS=111111111,222222222
```

System User має бути призначений на потрібні Page assets. Токен/застосунок
повинні мати щонайменше `pages_read_engagement`, `pages_manage_engagement`,
`pages_manage_metadata` і бажано `pages_read_user_content`.

## Meta Webhooks

У Meta Developers відкрий **Webhooks → Page** та вкажи:

- Callback URL: значення `WEBHOOK_CALLBACK_URL`;
- Verify token: значення `FACEBOOK_VERIFY_TOKEN`;
- поле підписки: `feed`.

Після загальної конфігурації натисни рубильник біля конкретної сторінки в
панелі. Сервер виконає `POST /PAGE_ID/subscribed_apps` і підпише її на `feed`.

## Як працює модерація

Коли приходить нова подія `feed/comment`, запис одразу потрапляє в SQLite.
Якщо глобальний **Monster Mode** активний, сервер у фоновому потоці виконує
`POST /COMMENT_ID` з `is_hidden=true`. Відповіді, написані від імені самої
сторінки, за замовчуванням не ховаються. У журналі можна вручну сховати або
повернути будь-який отриманий коментар.

Page access tokens, отримані від Meta під час синхронізації, зберігаються тільки
локально у SQLite та ніколи не повертаються frontend-коду.

## Безпека

Не коміть `.env` і `myraha.db`: обидва файли містять токени. Локально панель
відкривається без пароля. Через ngrok вона захищена HTTP Basic Auth: логін
`monster`, пароль — значення `DASHBOARD_PASSWORD`. Endpoint `/webhook` не
вимагає Basic Auth, але перевіряє `X-Hub-Signature-256` через App Secret.
