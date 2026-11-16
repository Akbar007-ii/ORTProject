# ORTO API: руководство для фронтенда

Полный список эндпоинтов, схемы запросов и ответов и кнопка «Try it out» находятся в Swagger:
`https://<API_DOMAIN>/docs`. Этот документ объясняет то, чего в Swagger не видно: правила
работы с сессией, таймером, автосохранением и ошибками.

- Базовый путь: `https://<API_DOMAIN>/api/v1`
- Контракт: `https://<API_DOMAIN>/openapi.json`. Типы для TypeScript:
  `npx openapi-typescript https://<API_DOMAIN>/openapi.json -o src/api/schema.d.ts`
- Все даты и время в ISO 8601, UTC (`2026-11-16T13:00:00Z`). Показывать в часовом поясе
  пользователя (`timezone` из `/me`, по умолчанию `Asia/Bishkek`).
- Тестовые аккаунты (по одному на роль) выдаёт команда бэкенда.

## 1. Авторизация

| Шаг | Запрос |
|---|---|
| Регистрация | `POST /auth/register` `{email, password, accept_terms: true, accept_privacy: true, locale?, timezone?, display_name?}` |
| Вход | `POST /auth/login` `{email, password}` |
| Ответ обоих | `{user, tokens: {access_token, expires_in, refresh_token}}` |
| Запросы | заголовок `Authorization: Bearer <access_token>` |
| Обновление | `POST /auth/refresh` `{refresh_token}` → новый `access_token` (refresh_token тот же) |
| Выход | `POST /auth/logout` `{refresh_token}`; со всех устройств: `POST /auth/logout-all` |
| Сброс пароля | `POST /auth/password-reset/request` `{email}` → письмо со ссылкой `FRONTEND_URL/reset-password?token=...` → страница вызывает `POST /auth/password-reset/confirm` `{token, new_password}` |

- `access_token` живёт 15 минут. На `401` с кодом `invalid_token` один раз вызвать `/auth/refresh`
  и повторить запрос; если и refresh вернул `401`, отправить на экран входа.
- `refresh_token` хранить надёжно (например, httpOnly-cookie на своём BFF или secure storage); живёт 30 дней.
- Пароль: от 8 символов. Без `accept_terms` и `accept_privacy` = `true` регистрация невозможна.
- `/auth/password-reset/request` всегда отвечает `202`, даже если email не найден: показывать
  «если такой адрес есть, мы отправили письмо».

## 2. Ошибки

Любая ошибка приходит в одном формате:

```json
{"error": {"code": "email_taken", "message": "...", "details": null, "request_id": "..."}}
```

- Текст для пользователя выбирать по `code` (на ky/ru), а не показывать `message`.
- `400 validation_error`: `details` = список `{field, message}` для подсветки полей формы.
- `429 rate_limited`: заголовок `Retry-After` (секунды) — показать, через сколько можно повторить.
- `request_id` показывать в сообщениях «что-то пошло не так»: по нему находится запись в логах.
- Чужой объект и несуществующий одинаково отвечают `404`.

Частые коды: `invalid_credentials`, `email_taken`, `invalid_token`, `invalid_reset_token`,
`session_finished`, `section_closed`, `session_in_progress`, `card_not_due`, `baseline_required`,
`target_too_high`, `target_too_low`, `no_questions`, `test_unavailable`.

## 3. Экраны и эндпоинты

| Экран (макеты ORTO) | Эндпоинты |
|---|---|
| Кабинет | `GET /plan/today`, `GET /goals/current/progress`, `GET /stats/overview`, `GET /prediction`, `GET /sessions?limit=1`, `GET /skills/roi` (слабая тема = первый элемент) |
| Тесты | `GET /tests?kind=mock|section|mini|diagnostic|subject`, `GET /sessions` («Мои сессии») |
| Экзамен | `POST /tests/{id}/start`, `GET /sessions/{id}`, `PUT /sessions/{id}/answers/{question_id}`, `POST /sessions/{id}/sections/{code}/close`, `POST /sessions/{id}/submit` |
| Результат | `GET /sessions/{id}/result`, `GET /sessions/{id}/strategy` |
| Практика | `GET /subjects`, `GET /subjects/{id}/topics`, `POST /practice/sessions`, `POST /practice/sessions/{id}/answers/{question_id}`, `POST /practice/sessions/{id}/finish` |
| Журнал ошибок | `GET /mistakes?status=&skill_id=&source=`, `PATCH /mistakes/{id}` (`reason`, `archived`) |
| Smart Review | `GET /review/queue`, `POST /review/{card_id}/answer`, `POST /review/{card_id}/snooze` |
| AI-учитель | `POST /tutor/explain` `{question_version_id, mode: short|detailed|example}`, `POST /question-reports` |
| Аналитика | `GET /stats/overview?period_days=7|30|90`, `GET /stats/skills`, `GET /stats/score-trajectory`, `GET /achievements`, `GET /reports/weekly` |
| Цель | `GET /universities/programs?q=`, `PUT /goals/current`, `GET /goals/current`, `GET /plan`, `POST /plan/rebuild`, `PATCH /plan/items/{id}` |
| Настройки | `GET/PATCH /me`, `GET/PATCH /me/notifications`, `POST /telegram/link-token`, `DELETE /telegram/link`, `GET /account/export`, `DELETE /account` |
| Админ-панель | всё под `/admin/...` (видно в Swagger); пункт меню показывать только при `admin`/`editor`/`expert` в `user.roles` |

## 4. Экзамен: правила, которые важно соблюсти

**Старт.** `POST /tests/{id}/start` с заголовком `Idempotency-Key: <uuid>` (новый uuid на каждое
нажатие «Начать»). Повторный запрос с тем же ключом или при незавершённой попытке вернёт ту же
сессию (`200`), новая сессия — `201`.

**Таймер.** Не доверять часам браузера. В каждом ответе есть `server_time` и `deadline_at`
(дедлайн открытого раздела, а если разделов нет — всего теста):
`осталось = deadline_at − server_time`, затем отсчитывать локально и пересинхронизировать при
каждом ответе сервера. По истечении времени просто запросить `GET /sessions/{id}`: сервер сам
закроет раздел/тест.

**Разделы.** В пробном ОРТ разделы идут по очереди. `questions` в ответе содержит только вопросы
открытого раздела (`current_section_code`). Закрытый раздел изменить и посмотреть нельзя.
Кнопка «Завершить раздел»: `POST /sessions/{id}/sections/{code}/close`.

**Автосохранение.** `PUT /sessions/{id}/answers/{question_id}`
`{option_id | null, marked?, seq, time_spent_sec?}`:
- `seq` — счётчик изменений этого вопроса на клиенте, растёт с каждым изменением (1, 2, 3...).
  Сервер применяет ответ, только если `seq` больше сохранённого, поэтому повтор старого запроса
  из офлайн-очереди не перезапишет новый ответ.
- Ответ: `{applied, option_id, marked, seq}`. Если `applied: false`, показать серверное состояние.
- Офлайн: класть изменения в локальную очередь (последнее на вопрос), отправлять после
  восстановления сети, индикатор `saved / saving / offline / error`.
- `409 section_closed` / `409 session_finished`: время вышло, обновить экран через `GET /sessions/{id}`.

**Завершение.** `POST /sessions/{id}/submit` возвращает результат; повторный вызов безопасен и
вернёт то же самое. Правильные ответы и решения приходят только в результате завершённого теста.

**Балл.** `scaled_score` — балл по шкале; если `score_is_estimated: true`, рядом обязательно
подпись «ориентировочно». Если шкалы нет, `scaled_score: null` — показывать `raw_score/max_score`.

**Перезагрузка страницы.** `GET /sessions/{id}` полностью восстанавливает экран (ответы, отметки, `seq`).

## 5. Практика, ошибки, повторение

- В практике ответ на вопрос даётся один раз: `POST .../answers/{question_id}` сразу возвращает
  `{is_correct, correct_option_id, solution}`. Повтор вернёт тот же результат.
- Каждая ошибка (в тесте, практике) автоматически попадает в журнал ошибок.
- Повторение: `GET /review/queue` (вопросы без ключа) → `POST /review/{card_id}/answer` с
  `Idempotency-Key`. Ответ содержит `next_due_at`, `interval_days`, `status` (`mastered` — карточка освоена).
- Причина ошибки (`reason`): `unknown_topic, calculation, inattention, misread, no_time, guessed, other`.

## 6. Цель, план, прогноз

- Цель можно поставить только после пробного теста (или ввода внешнего результата
  `POST /checkpoints/self-reported`), иначе `422 baseline_required`.
- `PUT /goals/current` `{target_score | program_id, exam_date, minutes_per_day}`. С `program_id`
  целью становится проходной балл программы (показывать год и источник, это ориентир, не гарантия).
- `GET /goals/current/progress`: `current_score`, `gap`, `next_milestone`, `track`
  (`ahead | on_track | behind | unknown`), `feasible`, `max_reachable`, `minutes_needed`.
- `GET /prediction`: если `available: false`, показать причину (`reason`), а не 0.
  Иначе диапазон `low–high` и `confidence` (`low | medium | high`).
- План: элементы `kind = practice | review | mock`. Для `practice` запускать
  `POST /practice/sessions {skill_id, plan_item_id}` — элемент отметится выполненным после `finish`.

## 7. Прочее

- **Язык:** `user.locale` (`ky`/`ru`) из `/me`; менять через `PATCH /me {locale}`. Вопросы в
  практике выдаются на языке пользователя.
- **Картинки и формулы:** текст вопросов (`stem`) — Markdown с LaTeX (`$...$`). Картинки:
  `![](/api/v1/media/<id>)` — путь относительно домена API. Рендерить Markdown безопасно (без HTML).
- **AI-учитель:** доступен только для вопросов, на которые пользователь уже ответил. Поле
  `source`: `ai` — текст от модели, `verified_solution` — показано проверенное решение
  (модель недоступна или исчерпан лимит `remaining_today`). Под объяснением — кнопка «Сообщить
  об ошибке» → `POST /question-reports {question_version_id, kind: "explanation"}`.
- **Telegram:** `POST /telegram/link-token` → показать `deep_link` (или `token` для команды
  `/start <token>` в боте). Токен одноразовый, живёт 10 минут.
- **Уведомления:** по умолчанию выключены; включение и часы — `PATCH /me/notifications`.
- **Удаление аккаунта:** `DELETE /account {password, confirm: "DELETE"}` — пользователь сам
  вводит слово DELETE. Действие необратимо.
