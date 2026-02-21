# Telethon Relay — Luna Gifts

Этот сервис запускается на **Render.com** (бесплатно) и выполняет все Telethon/MTProto операции.
PA (PythonAnywhere) общается с ним через HTTPS.

## Деплой на Render.com

### 1. Создайте аккаунт
- Зайдите на https://render.com и зарегистрируйтесь

### 2. Создайте Web Service
- Dashboard → **New** → **Web Service**
- Выберите **Build and deploy from a Git repository** или **Upload**
- Если Git: подключите GitHub репо и укажите **Root Directory**: `telethon_relay`
- Если Upload: загрузите содержимое папки `telethon_relay/`

### 3. Настройки сервиса
- **Name**: `lunagifts-relay` (или любое)
- **Region**: Frankfurt (EU) — ближе к Telegram DC
- **Runtime**: Python 3
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120`
- **Instance Type**: Free

### 4. Переменные окружения
- Добавьте: `RELAY_SECRET` = любой длинный секретный ключ (например: `my-super-secret-key-12345`)

### 5. Деплой
- Нажмите **Create Web Service** → дождитесь деплоя
- URL будет вида: `https://lunagifts-relay.onrender.com`

### 6. Настройка в админке Luna Gifts
- Откройте админку → Автовывод
- Введите **Relay URL**: `https://lunagifts-relay.onrender.com`
- Введите **Relay Secret**: тот же ключ что указали в Render
- Нажмите «Сохранить relay»
- Далее вводите API ID, API Hash, телефон → код → готово!

## Важно
- На бесплатном Render сервис «засыпает» через 15 мин без запросов
- Первый запрос после «сна» может занять ~30-60 секунд
- Для стабильной работы можно добавить UptimeRobot (бесплатно) для пинга `/health`
