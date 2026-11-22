# Vale — автопублікація на YouTube

Репозиторій сам завантажує серії на YouTube за розкладом. Працює в GitHub Actions, тобто в хмарі: телефон може бути вимкнений.

**Що всередині**
- `publish/metadata.json` — назви, описи й теги англійською (19 серій + повний фільм)
- `publish/schedule.json` — коли виходить кожна серія (час UTC)
- `publish/settings.json` — вимикачі: `approved`, `paused`
- `publish/ledger.json` — журнал: що вже завантажено, videoId, статус (оновлюється сам)
- `media/checksums.sha256` — контрольні суми відеофайлів
- `thumbnails/` — сюди кладуться прев'ю (`ep01.jpg` … `ep19.jpg`, `movie.jpg`)
- `reports/REPORT.md` — щоденний звіт (оновлюється сам)
- Самі відео лежать не в коді, а в релізі `media-v1` цього репозиторію (вкладка Releases).

Як воно працює: щодня о 06:17 UTC робот бере серії, у яких вихід протягом найближчих 3 днів, і завантажує їх як **private із запланованою датою публікації**. Опубліковує їх уже сам YouTube, точно в слот, навіть якщо GitHub запуститься із запізненням. Повторний запуск не створює копій: журнал і прихована мітка в тегах (`vale-ep01`…) не дають залити серію двічі.

---

## Крок 1. Google Cloud (один раз, у браузері телефона)

1. Відкрий **console.cloud.google.com** під тим Google-акаунтом, якому належить канал. Створи проєкт, наприклад `vale-publisher`.
2. Меню → **APIs & Services → Library** → знайди **YouTube Data API v3** → **Enable**.
3. Меню → **Google Auth Platform** (або «OAuth consent screen») → **Get started**:
   - App name: `vale-publisher`, support email: твій;
   - Audience: **External**;
   - Contact email: твій → **Create**.
4. **Audience → Publish app** (перевести в *In production*).
   Це важливо: у режимі *Testing* ключ доступу помирає через 7 днів, і публікація зупиниться. Застосунок буде «неперевіреним». Для власного каналу це нормально, просто на кроці 2 буде попередження.
5. **Clients → Create client**:
   - Application type: **Web application**;
   - Authorized redirect URIs → Add URI: `https://developers.google.com/oauthplayground`;
   - **Create** → одразу скопіюй **Client ID** і **Client secret** у нотатки (secret потім не покажуть).

## Крок 2. Отримати ключ доступу (refresh token)

1. Відкрий **developers.google.com/oauthplayground**.
2. Шестерня вгорі праворуч → увімкни **Use your own OAuth credentials** → встав Client ID і Client secret → закрий.
3. Ліворуч, у полі **Input your own scopes**, встав рядок:
   `https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly`
   → **Authorize APIs**.
4. Обери свій акаунт. Якщо спитає, обери **саме той канал**, куди публікуємо. На попередженні «Google hasn't verified this app» → **Advanced → Go to vale-publisher** → **Continue / Allow**.
5. **Exchange authorization code for tokens** → скопіюй **Refresh token**.

Ключ нікому не пересилай, і в чат теж. Він потрібен тільки на кроці 3.

## Крок 3. Секрети в GitHub

Репозиторій → **Settings → Secrets and variables → Actions**:
- вкладка **Secrets → New repository secret**, три штуки:
  `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`;
- вкладка **Variables → New repository variable**:
  `YT_CHANNEL_ID` = ID каналу. Його видно в YouTube → Settings → Advanced settings, або візьми з кроку 4.

## Крок 4. Перевірки й пілот

**Actions → publish → Run workflow**, обери mode:

1. `verify`: у лозі має бути `Authorized channel: <назва каналу> (UC…)`. Якщо там чужий канал, робот зупиниться й нічого не завантажить.
2. `dry-run`: перевіряє файли й показує план, нічого не завантажує.
3. `pilot`: заливає **1-шу серію як private без дати**. У лозі буде посилання.
   Відкрий YouTube Studio на телефоні → **Content** і перевір: серія визначилась як Short, субтитри не перекриває інтерфейс, звук нормальний.

## Крок 5. Запуск

Якщо пілот ок: відкрий `publish/settings.json` → олівець ✏️ → заміни `"approved": false` на `"approved": true` → **Commit**.

Далі все автоматично. Пілотна серія не перезаливається: робот просто ставить їй дату виходу.

**Розклад** (`publish/schedule.json`): з 24.11 по одній серії на день, по черзі о 18:00 і 22:00 UTC (19:00 / 23:00 за Варшавою, 13:00 / 17:00 за Нью-Йорком). Повний фільм виходить 13.12. Дати можна міняти в цьому файлі, поки серію ще не завантажено (тобто більш ніж за 3 дні до виходу).

## Керування

- **Пауза:** у `publish/settings.json` постав `"paused": true`. Нові завантаження зупиняться.
- **Скасувати вже заплановану серію:** YouTube Studio → Content → серія → Visibility → **Private** (дата знімається). Потім у `publish/ledger.json` у цієї серії заміни `"state": "scheduled"` на `"state": "missed"`, щоб робот її не чіпав.
- **Пропущений слот** (`missed` у журналі): постав нову дату в `schedule.json` і видали запис цієї серії з `ledger.json`.
- **Ключ помер** (`invalid_grant` у лозі): повтори крок 2 і заміни `YT_REFRESH_TOKEN`. Нічого не зламається, робот просто чекає.
- **Прев'ю:** поклади `epNN.jpg` у папку `thumbnails/` (JPG до 2 МБ, Shorts 1080×1920, фільм 1280×720). Робот поставить прев'ю, коли YouTube закінчить обробку відео.

## Звіт

`reports/REPORT.md` оновлюється після кожного щоденного запуску: статус кожної серії, перегляди, лайки, коментарі, підписники. Утримання й «engaged views» є тільки в YouTube Studio → Analytics.

## Налаштування за замовчуванням

- категорія Entertainment, мова English;
- **not made for kids**;
- позначка **altered or synthetic content = так** (AI-відео).

Якщо щось із цього не так, скажи Claude, і він виправить `publish/metadata.json`.
