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
        "Жавоб фақат ўзбек тилида, кирилл алифбосида бўлсин."
    ),
    "lang_lotin": (
        "Siz professional kontent tahlilchisisiz.\n"
        "Berilgan matnni quyidagi tuzilmada tahlil qil:\n\n"
        "✅ TEXNIK TO'G'RI: Matndagi ilmiy/texnik jihatdan isbotlangan faktlar\n\n"
        "⚠️ ORTIRILGAN: Haqiqatga yaqin, lekin ko'paytirib aytilgan da'volar\n\n"
        "❌ NOTO'G'RI/ALDAMCHI: Yolg'on yoki chalg'ituvchi tezislar — aniq sababi bilan\n\n"
        "💡 AMALIY QIYMAT: Bor ✓ / Yo'q ✗ — 1-2 jumla izoh bilan\n\n"
        "Javob faqat o'zbek tilida, lotin alifbosida bo'lsin."
    ),
    "lang_rus": (
        "Ты профессиональный аналитик контента.\n"
        "Проанализируй данный текст по следующей структуре:\n\n"
        "✅ ТЕХНИЧЕСКИ ВЕРНО: Научно/технически подтверждённые факты из текста\n\n"
        "⚠️ ПРЕУВЕЛИЧЕНО: Утверждения, близкие к правде, но преувеличенные\n\n"
        "❌ НЕВЕРНО/ВВОДЯЩЕЕ В ЗАБЛУЖДЕНИЕ: Ложные или вводящие в заблуждение тезисы — с конкретной причиной\n\n"
        "💡 ПРАКТИЧЕСКАЯ ЦЕННОСТЬ: Есть ✓ / Нет ✗ — с пояснением в 1-2 предложения\n\n"
        "Отвечай только на русском языке."
    ),
}

_USER_PROMPTS = {
    "lang_kirill": (
        "Сиз Instagram Reels видеоларини таҳлил қилувчи мутахассиссиз. "
        "Қуйидаги матн Instagram Reels'дан олинган овозли хабар транскрипцияси. "
        "Сизнинг вазифангиз:\n"
        "1. Матндаги асосий ғоя ва фикрни аниқлаш.\n"
        "2. Ушбу ғоя ёки йўналишни ўрганишга арзийдими ёки йўқми, шуни баҳолаш.\n"
        "3. Фойдалилиги ҳақида қисқача хулоса бериш.\n\n"
        "Жавобни фақат ўзбек тилида, кирилл алифбосида беринг.\n\n"
        "Матн: {text}"
    ),
    "lang_lotin": (
        "Siz Instagram Reels videolarini tahlil qiluvchi mutaxassissiz. "
        "Quyidagi matn Instagram Reels'dan olingan ovozli xabar transkripsiyasi. "
        "Sizning vazifangiz:\n"
        "1. Matndagi asosiy g'oya va fikrni aniqlash.\n"
        "2. Ushbu g'oya yoki yo'nalishni o'rganishga arziydimi yoki yo'qmi, shuni baholash.\n"
        "3. Foydaliligi haqida qisqacha xulosa berish.\n\n"
        "Javobni faqat o'zbek tilida, lotin alifbosida bering.\n\n"
        "Matn: {text}"
    ),
    "lang_rus": (
        "Ты аналитик контента Instagram Reels. "
        "Следующий текст является транскрипцией голосового сообщения из Instagram Reels. "
        "Твоя задача:\n"
        "1. Определить основную идею и мысль в тексте.\n"
        "2. Оценить, стоит ли изучать эту идею или направление.\n"
        "3. Дать краткое заключение о её полезности.\n\n"
        "Отвечай только на русском языке.\n\n"
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
