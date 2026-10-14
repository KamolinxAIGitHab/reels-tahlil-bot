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
    """analysis_log jadvalini (mavjud bo'lmasa) yaratadi va yangi
    ustunlarni (masalan fallback_used) eski bazalarga ham qo'shadi."""
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
                error_detail TEXT,
                fallback_used INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        try:
            conn.execute(
                "ALTER TABLE analysis_log ADD COLUMN fallback_used INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
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
    fallback_used: int = 0,
) -> None:
    """Bitta so'rov natijasini analysis_log jadvaliga yozadi. Statistika
    yozish botning asosiy funksionalligini to'xtatmasligi kerak, shuning
    uchun xato yuz bersa faqat logga yoziladi, exception ko'tarilmaydi."""
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO analysis_log "
                "(user_id, timestamp, source_type, language, status, error_type, error_detail, fallback_used) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    datetime.now(timezone.utc).isoformat(),
                    source_type,
                    language,
                    status,
                    error_type,
                    (error_detail or "")[:500] or None,
                    fallback_used,
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

        # Timestamplar UTC da saqlanadi (log_analysis: datetime.now(timezone.utc)).
        # O'zbekiston doim UTC+5 (DST yo'q), shuning uchun "bugun"/soat hisoblarini
        # to'g'ri chiqarish uchun barcha taqqoslashlarga +5 soat siljish qo'llanadi.
        today_total = count(" WHERE date(timestamp, '+5 hours') = date('now', '+5 hours')")
        today_success = count(" WHERE date(timestamp, '+5 hours') = date('now', '+5 hours') AND status = 'success'")
        today_error = count(" WHERE date(timestamp, '+5 hours') = date('now', '+5 hours') AND status = 'error'")

        today_error_types = {}
        tracked_errors = ("instagram_limit", "cookie_expired", "openai_credit", "moderation", "bad_link")
        for et in tracked_errors:
            today_error_types[et] = count(
                " WHERE date(timestamp, '+5 hours') = date('now', '+5 hours') AND status = 'error' AND error_type = ?",
                (et,),
            )
        placeholders = ",".join("?" * len(tracked_errors))
        today_error_types["boshqa"] = count(
            f" WHERE date(timestamp, '+5 hours') = date('now', '+5 hours') AND status = 'error' "
            f"AND (error_type IS NULL OR error_type NOT IN ({placeholders}))",
            tracked_errors,
        )

        week_total = count(" WHERE strftime('%Y-%W', timestamp, '+5 hours') = strftime('%Y-%W', 'now', '+5 hours')")
        month_total = count(" WHERE strftime('%Y-%m', timestamp, '+5 hours') = strftime('%Y-%m', 'now', '+5 hours')")
        all_total = count()

        by_source = {st: count(" WHERE source_type = ?", (st,)) for st in SOURCE_TYPES}
        by_lang = {lang: count(" WHERE language = ?", (lang,)) for lang in LANGUAGES}
        fallback_used_count = count(" WHERE fallback_used = 1")
        # cookie'siz urinish muvaffaqiyatsiz bo'lib, cookie bilan
        # muvaffaqiyatli bo'lgan hollar — log_analysis()ga error_type=
        # 'cookie_fallback' bilan yoziladi (status='success' saqlanadi,
        # shuning uchun today_error statistikasi buzilmaydi).
        cookie_fallback_count = count(" WHERE error_type = 'cookie_fallback'")

        cur.execute(
            "SELECT strftime('%H', timestamp, '+5 hours') as hour, COUNT(*) as count "
            "FROM analysis_log "
            "WHERE date(timestamp, '+5 hours') = date('now', '+5 hours') "
            "GROUP BY hour ORDER BY hour"
        )
        hourly = {hour: cnt for hour, cnt in cur.fetchall()}

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
            "fallback_used_count": fallback_used_count,
            "cookie_fallback_count": cookie_fallback_count,
            "hourly": hourly,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# OpenAI xarajati (cost_log). Mavjud analysis_log jadvaliga tegmaydi.
# ---------------------------------------------------------------------------

def init_cost_table() -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cost_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                label TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                audio_seconds REAL NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


init_cost_table()


def log_cost(label: str, model: str, input_tokens: int, cached_tokens: int,
             output_tokens: int, audio_seconds: float, cost_usd: float) -> None:
    """Bitta OpenAI chaqiruvi xarajatini yozadi. Xato bo'lsa faqat logga yoziladi."""
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO cost_log "
                "(timestamp, label, model, input_tokens, cached_tokens, output_tokens, audio_seconds, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), label, model,
                 input_tokens, cached_tokens, output_tokens, audio_seconds, cost_usd),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logging.error(f"cost_log'ga yozib bo'lmadi: {e}")


def get_cost_summary() -> dict:
    """Xarajat yig'indilari: bugun, hafta, oy, jami, oy bo'yicha turlar,
    bitta so'rovga o'rtacha va oy oxirigacha taxmin. Kunlar UTC+5 bo'yicha."""
    import calendar
    from datetime import timedelta

    conn = _connect()
    try:
        cur = conn.cursor()

        def total(where: str = "") -> tuple[float, int]:
            cur.execute(f"SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM cost_log{where}")
            r = cur.fetchone()
            return float(r[0]), int(r[1])

        today_usd, today_calls = total(" WHERE date(timestamp, '+5 hours') = date('now', '+5 hours')")
        week_usd, _ = total(" WHERE strftime('%Y-%W', timestamp, '+5 hours') = strftime('%Y-%W', 'now', '+5 hours')")
        month_where = " WHERE strftime('%Y-%m', timestamp, '+5 hours') = strftime('%Y-%m', 'now', '+5 hours')"
        month_usd, month_calls = total(month_where)
        all_usd, all_calls = total()

        cur.execute(
            "SELECT label, COUNT(*), COALESCE(SUM(cost_usd), 0) FROM cost_log"
            + month_where + " GROUP BY label ORDER BY SUM(cost_usd) DESC"
        )
        by_label = [(r[0], int(r[1]), float(r[2])) for r in cur.fetchall()]

        cur.execute("SELECT MIN(timestamp) FROM cost_log")
        first_ts = cur.fetchone()[0]

        # O'rtacha narx adolatli chiqishi uchun so'rovlar ham faqat xarajat
        # yozuvi boshlangan paytdan keyingilar bo'yicha sanaladi
        if first_ts:
            cur.execute(
                "SELECT COUNT(*) FROM analysis_log WHERE status = 'success' "
                "AND error_type IS NOT 'cache_hit' AND timestamp >= ? "
                "AND strftime('%Y-%m', timestamp, '+5 hours') = strftime('%Y-%m', 'now', '+5 hours')",
                (first_ts,),
            )
            month_requests = int(cur.fetchone()[0])
        else:
            month_requests = 0

        cur.execute(
            "SELECT MIN(timestamp) FROM cost_log" + month_where
        )
        first_month_ts = cur.fetchone()[0]

        now_uz = datetime.now(timezone.utc) + timedelta(hours=5)
        days_in_month = calendar.monthrange(now_uz.year, now_uz.month)[1]
        forecast = None
        if first_month_ts and month_usd > 0:
            start = datetime.fromisoformat(first_month_ts) + timedelta(hours=5)
            elapsed_days = (now_uz - start).total_seconds() / 86400
            if elapsed_days >= 1.0:
                remaining_days = max(days_in_month - (now_uz.day - 1) - (now_uz.hour / 24), 0)
                forecast = month_usd + month_usd / elapsed_days * remaining_days

        return {
            "today_usd": today_usd, "today_calls": today_calls,
            "week_usd": week_usd,
            "month_usd": month_usd, "month_calls": month_calls,
            "all_usd": all_usd, "all_calls": all_calls,
            "by_label": by_label,
            "month_requests": month_requests,
            "avg_per_request": (month_usd / month_requests) if month_requests else None,
            "forecast_month_usd": forecast,
            "tracking_since": first_ts,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Xarajat limiti ogohlantirishlari (cost_alert). Har bir chegara bir marta yoziladi.
# ---------------------------------------------------------------------------

def init_alert_table() -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cost_alert (
                key TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


init_alert_table()


def cost_totals() -> tuple[float, float]:
    """(bugungi, shu oydagi) xarajat, USD. Kun va oy UTC+5 bo'yicha."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_log "
            "WHERE date(timestamp, '+5 hours') = date('now', '+5 hours')"
        )
        today = float(cur.fetchone()[0])
        cur.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_log "
            "WHERE strftime('%Y-%m', timestamp, '+5 hours') = strftime('%Y-%m', 'now', '+5 hours')"
        )
        month = float(cur.fetchone()[0])
        return today, month
    finally:
        conn.close()


def add_alert(key: str, text: str, sent: int = 0) -> bool:
    """Ogohlantirishni yozadi. Shu kalit avval yozilgan bo'lsa False qaytaradi."""
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO cost_alert (key, text, created_at, sent) VALUES (?, ?, ?, ?)",
            (key, text, datetime.now(timezone.utc).isoformat(), sent),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def pending_alerts() -> list[tuple[str, str]]:
    conn = _connect()
    try:
        cur = conn.execute("SELECT key, text FROM cost_alert WHERE sent = 0 ORDER BY created_at")
        return [(r[0], r[1]) for r in cur.fetchall()]
    finally:
        conn.close()


def mark_alert_sent(key: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE cost_alert SET sent = 1 WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()
