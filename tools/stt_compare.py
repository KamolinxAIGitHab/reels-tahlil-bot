"""STT (нутқни матнга айлантириш) моделларини битта аудио файлда солиштириш.

Ишга тушириш (PowerShell, лойиҳа папкасида):
    python tools\\stt_compare.py                      # downloads\\sayohat.mp3
    python tools\\stt_compare.py downloads\\boshqa.mp3

Калитлар .env дан ўқилади (чатга ёки кодга ёзилмайди):
    OPENAI_API_KEY      — мажбурий
    ASSEMBLYAI_API_KEY  — ихтиёрий (AssemblyAI Universal-2, language_code=uz)
    AISHA_API_KEY       — ихтиёрий (Aisha AI, language=uz)

Эталон (ихтиёрий): downloads\\etalon.txt — аудионинг бошидан қўлда ёзилган матн.
Бор бўлса, ҳар бир вариант учун тахминий сўз хатолиги улуши (WER) ҳисобланади.

Натижа: downloads\\stt_natija.txt (папка git'га тушмайди).
"""
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDIO = ROOT / "downloads" / "sayohat.mp3"
ETALON = ROOT / "downloads" / "etalon.txt"
OUT = ROOT / "downloads" / "stt_natija.txt"

# Ботдаги ишлаб чиқариш кўрсатмаси (bot/utils/stt.py) билан бир хил
HINT = "Nutq o'zbek, rus yoki ingliz tilida bo'lishi mumkin. O'zbekcha bo'lsa, o'zbek lotin imlosida yozing: o', g', sh, ch."

# (номи, модель, language, prompt)
UZ_PROMPT_EN = "The audio is in the Uzbek language. Transcribe it in standard Uzbek Latin orthography (o', g', sh, ch, x, q, h), not in Azerbaijani, Turkish or Kazakh spelling."
UZ_PROMPT_UZ = "Bu o'zbek tilidagi nutq. O'zbek lotin alifbosida yozing: o', g', sh, ch, x, q, h."
UZ_PROMPT_CYR = "Бу ўзбек тилидаги нутқ. Ўзбек кирилл алифбосида ёзинг: ў, қ, ғ, ҳ, ш, ч."

UZ_PROMPT_MIXED = "The audio may be in Uzbek, Russian or English. If it is Uzbek, transcribe it in standard Uzbek Latin orthography (o', g', sh, ch, x, q, h), not Azerbaijani or Turkish spelling. If it is Russian or English, transcribe it normally."

OPENAI_VARIANTS = [
    ("B gpt-4o-transcribe | ҳозирги бот", "gpt-4o-transcribe", None, HINT),
    ("J gpt-4o-mini-transcribe | «Uzbek language» мажбурий", "gpt-4o-mini-transcribe", None, UZ_PROMPT_EN),
    ("K gpt-4o-mini-transcribe | аралаш тил кўрсатмаси (ўзбек/рус/инглиз)", "gpt-4o-mini-transcribe", None, UZ_PROMPT_MIXED),
    ("L gpt-4o-mini-transcribe | кўрсатмасиз", "gpt-4o-mini-transcribe", None, None),
]

# ---------- солиштириш учун матнни бир кўринишга келтириш ----------
_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "ts",
    "ч": "ch", "ш": "sh", "ъ": "", "ь": "", "ы": "i", "э": "e", "ю": "yu", "я": "ya",
    "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h",
}
_AZ = {"ə": "a", "ı": "i", "ş": "sh", "ç": "ch", "ü": "u", "ö": "o", "ğ": "g"}


def normalize(text: str) -> list:
    """Кирилл/лотин ва озарбайжон ҳарфларини ягона содда лотин кўринишига келтириб,
    сўзлар рўйхатини қайтаради (қўшимча белгилар ва апострофсиз)."""
    t = re.sub(r"[ʼʻ’‘`']", "", text.lower())  # апострофлар: o'zbek → ozbek
    t = "".join(_CYR.get(ch, ch) for ch in t)
    t = "".join(_AZ.get(ch, ch) for ch in t)
    t = re.sub(r"[^a-z\s]", " ", t)  # апостроф ва тиниш белгилари тушиб қолади
    return t.split()


def wer(ref: list, hyp: list) -> float:
    """Сўз даражасидаги Левенштейн масофаси / эталон узунлиги."""
    if not ref:
        return float("nan")
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / len(ref)


def wer_prefix(ref_text: str, hyp_text: str) -> float:
    """Эталон аудионинг бош қисмидан бўлгани учун гипотезанинг бошидан
    эталондан ~15% узунроқ қисми олинади (тахминий баҳо)."""
    ref = normalize(ref_text)
    hyp = normalize(hyp_text)[: int(len(ref) * 1.15) + 2]
    return wer(ref, hyp)


# ---------- хизматлар ----------
def run_openai(client, audio_path, model, language, prompt):
    kwargs = {"model": model}
    if language:
        kwargs["language"] = language
    if prompt:
        kwargs["prompt"] = prompt
    with open(audio_path, "rb") as f:
        r = client.audio.transcriptions.create(file=f, **kwargs)
    return r.text


def run_assemblyai(audio_path, key):
    import httpx
    h = {"authorization": key}
    with httpx.Client(timeout=120.0) as c:
        with open(audio_path, "rb") as f:
            up = c.post("https://api.assemblyai.com/v2/upload", headers=h, content=f.read())
        up.raise_for_status()
        body = {
            "audio_url": up.json()["upload_url"],
            "language_code": "uz",
            "speech_models": ["universal-2"],
        }
        t = c.post("https://api.assemblyai.com/v2/transcript", headers=h, json=body)
        t.raise_for_status()
        tid = t.json()["id"]
        for _ in range(120):
            time.sleep(3)
            p = c.get(f"https://api.assemblyai.com/v2/transcript/{tid}", headers=h)
            p.raise_for_status()
            d = p.json()
            if d["status"] == "completed":
                return d.get("text") or ""
            if d["status"] == "error":
                raise RuntimeError(d.get("error", "AssemblyAI хатоси"))
    raise TimeoutError("AssemblyAI жавоб бермади")


def run_aisha(audio_path, key):
    import httpx
    with httpx.Client(timeout=180.0) as c, open(audio_path, "rb") as f:
        r = c.post(
            "https://back.aisha.group/api/v1/stt/post/",
            headers={"X-Api-Key": key},
            files={"audio": (Path(audio_path).name, f, "audio/mpeg")},
            data={"language": "uz", "has_diarization": "false", "has_offset": "false", "is_summary": "false", "title": "stt_compare"},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("transcript", "")


def collect_audio(args):
    import glob
    files = []
    for a in (args or [str(DEFAULT_AUDIO)]):
        pa = a if os.path.isabs(a) else str(ROOT / a)
        hits = sorted(glob.glob(pa)) or ([pa] if os.path.exists(pa) else [])
        files += [Path(h) for h in hits if Path(h).is_file()]
    return files


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    audios = collect_audio(sys.argv[1:])
    if not audios:
        sys.exit("Аудио файл топилмади")

    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        sys.exit(".env да OPENAI_API_KEY топилмади")
    from openai import OpenAI
    client = OpenAI(api_key=openai_key, timeout=120.0, max_retries=1)

    etalon = ETALON.read_text(encoding="utf-8").strip() if ETALON.exists() else ""
    lines = [f"Файллар: {len(audios)}", f"Эталон: {'бор' if etalon else 'йўқ'}", ""]
    for audio in audios:
        label = f"{audio.parent.name}/{audio.name}"
        lines += ["#" * 70, f"ФАЙЛ: {label}", ""]
        print(f"\n== {label}")
        jobs = [(n, (lambda m=m, l=l, p=p: run_openai(client, audio, m, l, p))) for n, m, l, p in OPENAI_VARIANTS]
        if os.getenv("ASSEMBLYAI_API_KEY"):
            k = os.getenv("ASSEMBLYAI_API_KEY")
            jobs.append(("G AssemblyAI Universal-2 | uz", lambda: run_assemblyai(audio, k)))
        if os.getenv("AISHA_API_KEY"):
            k = os.getenv("AISHA_API_KEY")
            jobs.append(("H Aisha AI | uz", lambda: run_aisha(audio, k)))
        for name, fn in jobs:
            t0 = time.time()
            try:
                text, err = fn(), None
            except Exception as e:  # хато матнида калит бўлмайди
                text, err = "", f"{type(e).__name__}: {str(e)[:300]}"
            dt = time.time() - t0
            score = ""
            if etalon and text and len(audios) == 1:
                score = f"WER≈{wer_prefix(etalon, text) * 100:.0f}%"
            lines += [f"=== {name} ({dt:.1f} с) {score}", err or text, ""]
            print(f"  {name}: {'ХАТО' if err else (score or 'тайёр')} ({dt:.1f} с)")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nТўлиқ натижа: {OUT}")


if __name__ == "__main__":
    main()
