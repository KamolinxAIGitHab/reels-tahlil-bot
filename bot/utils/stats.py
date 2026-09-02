import sqlite3
import os
import logging
from datetime import datetime, timezone

_DATA_DIR = os.environ.get("STATS_DB_DIR",
    os.path.join(os.path.dirname(__file__), "..", ".."))
os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, "stats.db")

SOURCE_TYPES = ("instagram_reels", "instagram_post", "instagram_account", "youtube_shorts", "voice")
LANGUAGES = ("lang_kirill", "lang_lotin", "lang_rus")
ERROR_TYPES = ("instagram_limit", "openai_credit", "openai_auth", "moderation", "download_error", "other")


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    """analysis_log jadvalini (mavjud bo'lmasa) yaratadi."""
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                source_type TEXT NOT NULL,
                language TEXT NOT NULL,
                status TEXT NOT NULL,
                error_type TEXT,
                error_detail TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


init_db()


def log_analysis(
    user_id: int,
    source_type: str,
    language: str,
    status: str,
    error_type: str | None = None,
    error_detail: str | None = None,
) -> None:
    """Bitta so'rov natijasini analysis_log jadvaliga yozadi. Statistika
    yozish botning asosiy funksionalligini to'xtatmasligi kerak, shuning
    uchun xato yuz bersa faqat logga yoziladi, exception ko'tarilmaydi."""
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO analysis_log "
                "(user_id, timestamp, source_type, language, status, error_type, error_detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    datetime.now(timezone.utc).isoformat(),
                    source_type,
                    language,
                    status,
                    error_type,
                    (error_detail or "")[:500] or None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logging.error(f"Statistika bazasiga yozishda xato: {e}")


def get_stats() -> dict:
    """/stats buyrug'i uchun barcha kerakli agregatsiyalarni qaytaradi."""
    conn = _connect()
    try:
        cur = conn.cursor()

        def count(where: str = "", params: tuple = ()) -> int:
            cur.execute(f"SELECT COUNT(*) FROM analysis_log{where}", params)
            return cur.fetchone()[0]

        today_total = count(" WHERE date(timestamp) = date('now')")
        today_success = count(" WHERE date(timestamp) = date('now') AND status = 'success'")
        today_error = count(" WHERE date(timestamp) = date('now') AND status = 'error'")

        today_error_types = {}
        tracked_errors = ("instagram_limit", "openai_credit", "moderation")
        for et in tracked_errors:
            today_error_types[et] = count(
                " WHERE date(timestamp) = date('now') AND status = 'error' AND error_type = ?",
                (et,),
            )
        placeholders = ",".join("?" * len(tracked_errors))
        today_error_types["boshqa"] = count(
            f" WHERE date(timestamp) = date('now') AND status = 'error' "
            f"AND (error_type IS NULL OR error_type NOT IN ({placeholders}))",
            tracked_errors,
        )

        week_total = count(" WHERE strftime('%Y-%W', timestamp) = strftime('%Y-%W', 'now')")
        month_total = count(" WHERE strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now')")
        all_total = count()

        by_source = {st: count(" WHERE source_type = ?", (st,)) for st in SOURCE_TYPES}
        by_lang = {lang: count(" WHERE language = ?", (lang,)) for lang in LANGUAGES}

        return {
            "today_total": today_total,
            "today_success": today_success,
            "today_error": today_error,
            "today_error_types": today_error_types,
            "week_total": week_total,
            "month_total": month_total,
            "all_total": all_total,
            "by_source": by_source,
            "by_lang": by_lang,
        }
    finally:
        conn.close()
