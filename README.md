# ORTO API

ЖРТ/ОРТга даярдануу платформасынын backend'и (FastAPI + PostgreSQL). ТЗ: `docs/ТЗ_ORT_V3.pdf`.

## Иштетүү

```bash
docker compose up -d                        # PostgreSQL (5433) жана Redis (6380)
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m alembic upgrade head  # таблицалар + ролдор
.venv\Scripts\python main.py                  # http://127.0.0.1:8000/docs
```

Биринчи администратор:

```bash
.venv\Scripts\python -m mysite.cli create-admin admin@example.com
```

Демо маалымат (математика, «demo» форматы, 8 суроолуу пробный; чыныгы ОРТ эмес):

```bash
.venv\Scripts\python -m mysite.cli seed-demo
```

Убактысы бүткөн сессияларды жабуу (cron үчүн; кирген учурда баары бир автоматтык жабылат):

```bash
.venv\Scripts\python -m mysite.cli expire-sessions
```

Тесттер (өзүнчө `orto_test` базасында, чыныгы миграциялар менен):

```bash
.venv\Scripts\python -m pytest
```

## Түзүлүшү

| Папка | Милдети |
|---|---|
| `mysite/app.py` | FastAPI колдонмосу, роутерлер, middleware |
| `mysite/config.py` | Жөндөөлөр (`.env`) |
| `mysite/core/` | Коопсуздук (Argon2id, JWT), каталар, rate limit, dependencies |
| `mysite/db/models/` | SQLAlchemy моделдери |
| `mysite/db/schema/` | Pydantic схемалары (request/response) |
| `mysite/services/` | Домендик логика (роуттардан бөлөк, Telegram-бот да колдонот) |
| `mysite/api/` | Окуучунун endpoint'тери (`/api/v1/...`) |
| `mysite/admin/` | Админ/CMS endpoint'тери (`/api/v1/admin/...`) |
| `migrations/` | Alembic миграциялары |

Модель өзгөргөндө: `alembic revision --autogenerate -m "..."`, файлды карап чыгып, `alembic upgrade head`.

## Суроонун жолу

`draft → editorial_review → expert_review → approved → published → retired`

Редактор жазат жана текшерүүгө жөнөтөт, эксперт бекитет (өз суроосун беките албайт), админ жарыялайт.
Жарыяланган суроо өзгөрбөйт: оңдоо үчүн `new-version` менен жаңы версия түзүлөт.

## Пробныйдын жолу

1. Админ форматты (бөлүмдөр, суроо саны, убакыт) жана шкаланы (raw → балл) киргизип, жарыялайт.
2. Редактор жарыяланган суроолордон тест түзөт, админ жарыялайт (формат менен дал келиши текшерилет).
3. Окуучу: `POST /tests/{id}/start` → `PUT /sessions/{id}/answers/{question_id}` →
   `POST /sessions/{id}/sections/{code}/close` → `POST /sessions/{id}/submit` → натыйжа жана checkpoint.

## Окуу цикли (90 → 200)

1. Пробный бүткөндө ар бир жооп `attempts` тарыхына жазылат, ката жооптор каталар журналына түшөт.
2. `PUT /goals/current`: максаттуу балл, экзамен датасы, күнүнө канча мүнөт.
3. `GET /skills/roi`: кайсы навыктан бир саатта эң көп балл алса болот.
4. `GET /plan/today`: бүгүнкү план (кайталоо + эң пайдалуу навыктар + ар эки жумада пробный).
5. `POST /practice/sessions`: практика, ар бир жооптон кийин чечими менен.
6. `GET /review/queue` → `POST /review/{card_id}/answer`: каталарды 1-3-7-14-30 күндө кайталоо.
7. `GET /prediction`, `GET /goals/current/progress`: божомол жана «план боюнча барамбы».

Формулалар `mysite/services/planning.py` жана `mysite/services/mastery.py` файлдарында, алар базага
көз каранды эмес жана `tests/test_learning_math.py` менен кол менен эсептелген сандарга текшерилген.

## AI-мугалим

`.env` файлына `ANTHROPIC_API_KEY=...` кошулса, `POST /tutor/explain` текшерилген чечимди окуучуга
түшүндүрүп берет (`TUTOR_MODEL`, демейки `claude-opus-5-5`). Ачкыч жок болсо же модель жооп бербесе,
текшерилген чечимдин өзү кайтат. AI туура жоопту аныктабайт жана окуучу жооп бере элек суроону ачпайт.

## Telegram-бот

`.env`: `TELEGRAM_BOT_TOKEN=...` (@BotFather'ден) жана `TELEGRAM_BOT_USERNAME=...`.

```bash
.venv\Scripts\python -m mysite.bot
```

Окуучу сайттан `POST /telegram/link-token` аркылуу бир жолку токен алып, ботко `/start <token>` жөнөтөт.
Буйруктар: `/today /review /stats /streak /weak /goal /language /help`.

## Суроолорду импорттоо жана сүрөттөр

- `POST /api/v1/admin/media` (multipart `file`): PNG/JPEG/GIF/WebP, 2 МБ чейин. Жоопто келген
  `markdown` (`![](/api/v1/media/<id>)`) суроонун текстине коюлат.
- `POST /api/v1/admin/questions/import?dry_run=true` (multipart `file`, `.csv` же `.json`): адегенде
  текшерүү, анан `dry_run=false`. Бир сап ката болсо эч нерсе жазылбайт; ошол эле файлды кайра
  жиберсеңиз, мурда кошулгандар өткөрүлүп жиберилет. Суроолор **черновик** болуп кирет.
  Мамычалар: `external_id, skill_code, language, stem, option_a..option_d, correct, solution,
  difficulty, source, license` (толугу `mysite/services/importer.py` башында).

## Аккаунт, эскертмелер, ЖОЖдор, стратегия

- `GET /account/export` — окуучунун бардык маалыматы JSON файл катары.
- `DELETE /account` (`{"password": ..., "confirm": "DELETE"}`) — аккаунт жана бардык жеке маалымат өчөт.
- `PATCH /me/notifications` — Telegram эскертмелери (демейки өчүк), саат, тынч сааттар, жумалык отчет.
- `GET /universities/programs?q=...` — ЖОЖ/адистиктин өткөн жылдагы гранттык баллы (булагы менен).
  `PUT /goals/current` ичинде `program_id` берсе, ошол балл максат болот.
- `GET /sessions/{id}/strategy` — тест бүткөндөн кийин убакыт боюнча кеңештер.

Админ: `PUT /admin/universities`, `POST /admin/universities/{id}/programs`, `PATCH /admin/university-programs/{id}`.

## Фондук worker

```bash
.venv\Scripts\python -m mysite.worker
```

Ар мүнөт сайын убактысы бүткөн тесттерди жабат жана Telegram эскертмелерин жөнөтөт
(бир күнгө бир жолу, тынч сааттарда жок, ботту бөгөттөгөндөргө токтойт).

## Серверге жүктөө (staging же production)

Серверде: Docker, ачык 80/443 порттору, `API_DOMAIN` үчүн DNS A-жазуусу сервердин IP'сине.

```bash
git clone <репозиторий> orto && cd orto
cp .env.example .env.prod      # SECRET_KEY, POSTGRES_PASSWORD, API_DOMAIN, ENVIRONMENT=staging,
                               # FRONTEND_URL, CORS_ORIGINS, SMTP_* ...
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm api python -m mysite.cli seed-staging --domain <домен>
```

Акыркы буйрук демо маалыматты жана ар бир ролго бирден тесттик аккаунт түзөт, сырсөздөрдү бир жолу
гана көрсөтөт (production'до иштебейт). Фронтенд үчүн колдонмо: `docs/FRONTEND_API.md`.

Курамы: `caddy` (HTTPS сертификатты өзү алат), `postgres`, `redis`, `migrate` (бир жолу
`alembic upgrade head`), `api`, `worker`, `bot` (`--profile telegram` менен), `backup` (күн сайын
`pg_dump` → `./backups`, акыркы 14ү). `ENVIRONMENT=staging` болсо `/docs` ачык; `production` болсо
жабык, жана API начар `SECRET_KEY` же `localhost` FRONTEND_URL менен иштебей коёт.

Жаңыртуу: `git pull` жана ошол эле `up -d --build` (миграциялар өзү өтөт).
