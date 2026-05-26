from openai import OpenAI
from bot.config import settings

client = OpenAI(api_key=settings.openai_api_key)

def analyze_content(text: str) -> str:
    """
    Analyzes the transcribed text and provides a verdict in Uzbek.
    """
    prompt = (
        "Сиз Instagram Reels видеоларини таҳлил қилувчи мутахассиссиз. "
        "Қуйидаги матн Instagram Reels'дан олинган овозли хабар транскрипцияси. "
        "Сизнинг вазифангиз:\n"
        "1. Матндаги асосий ғоя ва фикрни аниқлаш.\n"
        "2. Ушбу ғоя ёки йўналишни ўрганишга арзийдими ёки йўқми, шуни баҳолаш.\n"
        "3. Фойдалилиги ҳақида қисқача хулоса бериш.\n\n"
        "Жавобни фақат ўзбек тилида, кирилл алифбосида беринг.\n\n"
        f"Матн: {text}"
    )

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": (
                "Сиз профессионал контент таҳлилчисисиз.\n"
                "Берилган матнни қуйидаги тузилмада таҳлил қил:\n\n"
                "✅ ТЕХНИК ТЎҒРИ: Матндаги илмий/техник жиҳатдан исботланган фактлар\n\n"
                "⚠️ ОРТИРИЛГАН: Ҳақиқатга яқин, лекин кўпайтириб айтилган даъволар\n\n"
                "❌ НОТЎҒРИ/АЛДАМЧИ: Ёлғон ёки чалғитувчи тезислар — аниқ сабаби билан\n\n"
                "💡 АМАЛИЙ ҚИЙМАТ: Бор ✓ / Йўқ ✗ — 1-2 жумла изоҳ билан\n\n"
                "Жавоб фақат ўзбек тилида, кирилл алифбосида бўлсин."
            )},
            {"role": "user", "content": prompt}
        ]
    )

    return response.choices[0].message.content
