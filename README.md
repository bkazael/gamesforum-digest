# Ben's Weekly Digest — Gamesforum Digest

פודקאסט שבועי אוטומטי על תעשיית המשחקים לנייד, בעברית, בהנחיית שני
מנחים (דנה ויוני). כל שבוע: איסוף כתבות → דירוג לפי טעם אישי → תסריט →
סינתזת קול → פרסום ל-RSS. הכל דרך Gemini בלבד (טקסט + TTS).

לתיעוד מפורט של כל שינוי — מה, למה, ואיך בודקים — ראה `CHANGELOG.md`.

## איך זה בנוי

```
sources.py  ──▶  discovery.py  ──▶  gamesforum_pipeline.py  ──▶  feed.xml
(scrape)         (סינון + דירוג)     (תסריט + TTS + פרסום)
```

### 1. `sources.py` — איסוף
מתאמים ל-RSS (`PocketGamer.biz/.com`, `MobileGamer.biz`) ול-HTML
(`Gamesforum`, שאין לו feed). גם "כורה" roundups שבועיים של כל אתר —
הכתבות שהעורכים שם עצמם הדגישו מקבלות בוסט בציון.

### 2. `discovery.py` — הצינור, מהזול ליקר
ארבעה שלבים, כל אחד יקר יותר מקודמו, כך שכסף (טוקנים) מוציאים רק על מי
ששרד את השלבים הקודמים:

| שלב | מה קורה | עלות |
|---|---|---|
| LIST | scrape דפי listing | 0 |
| BLOCK | סינון כותרות זבל/שיווקיות | 0 |
| SIGNAL | הורדת גוף הכתבה, מדידת צפיפות נתונים | 0 (בקשת רשת) |
| SCORE | קריאה אחת ל-Gemini לכל ~20 כתבות, לפי `profile.toml` | טוקנים |

אחרי הדירוג: דה-דופליקציה של סיפורים חופפים, תקרה למקור בודד (כדי
שמקור פורה לא ישתלט על הפרק), וחלוקת "זמן אוויר" לפי דירוג. כל החלטה —
מה נכנס, מה נזרק, ולמה — נכתבת ל-`ledger/<date>.md`.

הרצה עצמאית לכוונון בלי לייצר אודיו:
```
python discovery.py --dry-run     # ללא קריאת LLM, ללא עלות
python discovery.py               # דירוג מלא, כותב ledger
```

### 3. `gamesforum_pipeline.py` — תסריט, קול, פרסום
- קריאה אחת ל-Gemini מייצרת את כל הפרק: כותרת, תקציר לפי נושא, ותסריט
  דו-שיח בין דנה ליוני (schema-constrained, `PODCAST_SCHEMA`).
- `memory.py` מזין הקשר מהפרקים האחרונים (ראה למטה).
- הטקסט מפוצל ל-chunks (`TTS_CHUNK_CHAR_LIMIT`, כרגע 3800 תווים) ונשלח
  ל-Gemini TTS רב-דובר. ה-audio מעורבב עם ג'ינגל (fade + ducking) דרך
  ffmpeg.
- `feed.xml` נבנה מחדש מכל הפרקים ב-`episodes/`.

## זיכרון פרקים (`memory.py`)

כדי שהפודקאסט יישמע כמו תוכנית מתמשכת ולא שישה פרקים חד-פעמיים:
כל פרק מסתיים בכתיבת entry ל-`memory.json`, נגזר **ישירות** מה-
`digest_summary` שכבר נוצר — בלי שום קריאת Gemini נוספת. בפרק הבא,
עד `[memory].lookback_episodes` (ברירת מחדל 3) מהפרקים האחרונים נכנסים
לפרומפט תחת "PREVIOUS EPISODES", עם הנחיה מפורשת להזכיר רק כשבאמת
רלוונטי. הארכיון המלא נשמר לצמיתות ב-`memory.json`; רק החלון שנכנס
לפרומפט מוגבל, כדי שעלות הטוקנים לא תגדל עם הזמן.

## `profile.toml` — הקובץ היחיד שצריך לערוך

מי הקורא, מה מעניין אותו, המקורות ומשקלם, חסימות, סף כניסה לפרק,
הגדרות הקול, וחלוקת זמן האוויר. אחרי כל ריצה, `ledger/<date>.md` מראה
מה נכנס ומה נזרק ולמה — תכוון לפי זה.

## משתני סביבה

| משתנה | ברירת מחדל | למה |
|---|---|---|
| `GEMINI_API_KEY` | (חובה) | היחיד ש-Gemini צריך — לטקסט וגם ל-TTS |
| `GEMINI_TEXT_MODEL` | `gemini-2.5-flash` | מודל התסריט |
| `TTS_MODEL` | `gemini-2.5-flash-preview-tts` | מודל הקול |
| `PODCAST_BASE_URL` | (ריק) | בסיס ה-URL של הפודקאסט ב-`feed.xml` — **חובה** בפרודקשן, אחרת ה-enclosure יוצא יחסי ולא ייקרא ע"י אף פלייר |
| `DIGEST_LANG` | `he` | שפת הפרק |
| `API_TIMEOUT_SEC` | `300` | timeout לכל קריאת Gemini בודדת |
| `RUN_DEADLINE_SEC` | `2400` | תקרת זמן כוללת לריצה |

## בדיקות — שלוש רמות

| רמה | קובץ | עלות | מתי רץ |
|---|---|---|---|
| 0/1 — לוגיקה + חיווט | `test_episode.py`, `test_memory.py`, `test_discovery.py`, `test_contracts.py` | אפס (הכל מדומה) | כל push (`test.yaml`) |
| 2 — smoke אמיתי | `live_smoke.py` | טוקנים אמיתיים, מינימלי | ידני בלבד (`manual_test.yaml`) |
| 3 — הריצה האמיתית | `gamesforum_pipeline.py` | מלא | שבועי, מתוזמן (`weekly-digest.yml`) |

`test_contracts.py` הוא הבדיקה החשובה ביותר: היא מייבאת את `discovery.py`
ו-`gamesforum_pipeline.py` יחד ומוודאת שכל שם ששני הקבצים מצפים לו באמת
קיים, ושהצורה שה-discovery מחזיר תואמת למה שהתסריט צריך. זו בדיוק הבדיקה
שהייתה תופסת את התקלה שהשביתה את הפרודקשן ב-24 באוגוסט 2026 — ייבוא
שבור של `discovery.select()`.

הרצה מקומית של הכל, בלי מפתח API אמיתי ובלי עלות:
```
python test_episode.py && python test_memory.py && python test_discovery.py && python test_contracts.py
```
