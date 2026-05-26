from openai import OpenAI
from bot.config import settings

client = OpenAI(api_key=settings.openai_api_key)

_SYSTEM_PROMPTS = {
    "lang_kirill": (
        "Сиз профессионал контент таҳлилчисисиз.\n"
        "Берилган матнни қуйидаги тузилмада таҳлил қил:\n\n"
        "✅ ТЕХНИК ТЎҒРИ: Матндаги илмий/техник жиҳатдан исботланган фактлар\n\n"
        "⚠️ ОРТИРИЛГАН: Ҳақиқатга яқин, лекин кўпайтириб айтилган даъволар\n\n"
        "❌ НОТЎҒРИ/АЛДАМЧИ: Ёлғон ёки чалғитувчи тезислар — аниқ сабаби билан\n\n"
        "💡 АМАЛИЙ ҚИЙМАТ: Бор ✓ / Йўқ ✗ — 1-2 жумла изоҳ билан\n\n"
        "МУҲИМ: Транскрипцияни ҳам ўзбек кирилл тилига таржима қилиб бер. "
        "Таҳлил ҳам фақат ўзбек кирилл тилида бўлсин.\n"
        "Жавоб формати:\n\n"
        "📝 Транскрипция (ўзбекча):\n"
        "[таржима]\n\n"
        "🔍 Таҳлил:\n"
        "✅ ТЕХНИК ТЎҒРИ: ...\n"
        "⚠️ ОРТИРИЛГАН: ...\n"
        "❌ НОТЎҒРИ/АЛДАМЧИ: ...\n"
        "💡 АМАЛИЙ ҚИЙМАТ: ..."
    ),
    "lang_lotin": (
        "Siz professional kontent tahlilchisisiz.\n"
        "Berilgan matnni quyidagi tuzilmada tahlil qil:\n\n"
        "✅ TEXNIK TO'G'RI: Matndagi ilmiy/texnik jihatdan isbotlangan faktlar\n\n"
        "⚠️ ORTIRILGAN: Haqiqatga yaqin, lekin ko'paytirib aytilgan da'volar\n\n"
        "❌ NOTO'G'RI/ALDAMCHI: Yolg'on yoki chalg'ituvchi tezislar — aniq sababi bilan\n\n"
        "💡 AMALIY QIYMAT: Bor ✓ / Yo'q ✗ — 1-2 jumla izoh bilan\n\n"
        "MUHIM: Transkripsiyani ham o'zbek lotin tiliga tarjima qilib ber. "
        "Tahlil ham lotin tilida bo'lsin.\n"
        "Javob formati:\n\n"
        "📝 Transkripsiya (o'zbekcha):\n"
        "[tarjima]\n\n"
        "🔍 Tahlil:\n"
        "✅ TEXNIK TO'G'RI: ...\n"
        "⚠️ ORTIRILGAN: ...\n"
        "❌ NOTO'G'RI/ALDAMCHI: ...\n"
        "💡 AMALIY QIYMAT: ..."
    ),
    "lang_rus": (
        "Ты профессиональный аналитик контента.\n"
        "Проанализируй данный текст по следующей структуре:\n\n"
        "✅ ТЕХНИЧЕСКИ ВЕРНО: Научно/технически подтверждённые факты из текста\n\n"
        "⚠️ ПРЕУВЕЛИЧЕНО: Утверждения, близкие к правде, но преувеличенные\n\n"
        "❌ НЕВЕРНО/ВВОДЯЩЕЕ В ЗАБЛУЖДЕНИЕ: Ложные или вводящие в заблуждение тезисы — с конкретной причиной\n\n"
        "💡 ПРАКТИЧЕСКАЯ ЦЕННОСТЬ: Есть ✓ / Нет ✗ — с пояснением в 1-2 предложения\n\n"
        "MUHIM: Transkripsiyani ham rus tiliga tarjima qilib ber. "
        "Tahlil ham rus tilida bo'lsin.\n"
        "Format otveta:\n\n"
        "📝 Транскрипция (на русском):\n"
        "[текст]\n\n"
        "🔍 Анализ:\n"
        "✅ ТЕХНИЧЕСКИ ВЕРНО: ...\n"
        "⚠️ ПРЕУВЕЛИЧЕНО: ...\n"
        "❌ НЕВЕРНО/ВВОДЯЩЕЕ В ЗАБЛУЖДЕНИЕ: ...\n"
        "💡 ПРАКТИЧЕСКАЯ ЦЕННОСТЬ: ..."
    ),
}

_USER_PROMPTS = {
    "lang_kirill": (
        "Сиз Instagram Reels видеоларини таҳлил қилувчи мутахассиссиз. "
        "Қуйидаги матн Instagram Reels'дан олинган овозли хабар транскрипцияси. "
        "Сизнинг вазифангиз:\n"
        "1. Транскрипцияни ўзбек кирилл тилига таржима қилиш.\n"
        "2. Матндаги асосий ғоя ва фикрни аниқлаш.\n"
        "3. Ушбу ғоя ёки йўналишни ўрганишга арзийдими ёки йўқми, шуни баҳолаш.\n"
        "4. Фойдалилиги ҳақида қисқача хулоса бериш.\n\n"
        "Жавобни белгиланган форматда, фақат ўзбек тилида, кирилл алифбосида беринг.\n\n"
        "Матн: {text}"
    ),
    "lang_lotin": (
        "Siz Instagram Reels videolarini tahlil qiluvchi mutaxassissiz. "
        "Quyidagi matn Instagram Reels'dan olingan ovozli xabar transkripsiyasi. "
        "Sizning vazifangiz:\n"
        "1. Transkripsiyani o'zbek lotin tiliga tarjima qilish.\n"
        "2. Matndagi asosiy g'oya va fikrni aniqlash.\n"
        "3. Ushbu g'oya yoki yo'nalishni o'rganishga arziydimi yoki yo'qmi, shuni baholash.\n"
        "4. Foydaliligi haqida qisqacha xulosa berish.\n\n"
        "Javobni belgilangan formatda, faqat o'zbek tilida, lotin alifbosida bering.\n\n"
        "Matn: {text}"
    ),
    "lang_rus": (
        "Ты аналитик контента Instagram Reels. "
        "Следующий текст является транскрипцией голосового сообщения из Instagram Reels. "
        "Твоя задача:\n"
        "1. Перевести транскрипцию на русский язык.\n"
        "2. Определить основную идею и мысль в тексте.\n"
        "3. Оценить, стоит ли изучать эту идею или направление.\n"
        "4. Дать краткое заключение о её полезности.\n\n"
        "Отвечай в указанном формате, только на русском языке.\n\n"
        "Текст: {text}"
    ),
}

def analyze_content(text: str, lang: str = "lang_kirill") -> str:
    system_prompt = _SYSTEM_PROMPTS.get(lang, _SYSTEM_PROMPTS["lang_kirill"])
    user_prompt = _USER_PROMPTS.get(lang, _USER_PROMPTS["lang_kirill"]).format(text=text)

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ]
    )

    return response.choices[0].message.content
