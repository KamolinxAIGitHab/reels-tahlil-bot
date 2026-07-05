from typing import TypedDict, List
from langgraph.graph import StateGraph, END
from openai import OpenAI
from bot.config import settings

client = OpenAI(api_key=settings.openai_api_key)

class AnalysisState(TypedDict):
    text: str
    lang: str
    claims: List[str]
    verified_claims: List[dict]
    final_report: str


EXTRACT_CLAIMS_PROMPT = """
Қуйидаги матнда келтирилган барча аниқ даъволарни, айниқса рақам,
фоиз, даромад, тежам ёки фойда ҳақидаги маълумотларни рўйхат
кўринишида ажратиб бер.

Ҳар бир даъвони алоҳида қатор сифатида ёз. Агар даъвода рақам
мавжуд бўлса, уни аниқ кўрсат.

Матн: {text}

Жавобни фақат рўйхат кўринишида, изоҳсиз бер:
1. [даъво матни]
2. [даъво матни]
"""

VERIFY_CLAIM_PROMPT = """
Қуйидаги даъвони танқидий баҳола.

Даъво: {claim}

Топшириқ:
1. Агар даъвода рақам (даромад, тежам, фойда ва ҳоказо) мавжуд
   бўлса — "Бу рақам қандай ҳисобланган?" деган савол бўйича
   текшир.
2. Агар видеода ёки матнда бу рақамнинг асоси (формула, манба,
   ҳисоб-китоб усули) кўрсатилмаган бўлса, буни асоссиз даъво
   сифатида белгила.
3. Агар даъвода рақам йўқ бўлса, унинг мантиқий тўғрилигини
   баҳола.

Натижани қуйидаги форматда қайтар:
БЕЛГИ: [✅ ТЎҒРИ / ⚠️ ОҒИРИЛГАН / ❌ НОТЎҒРИ-АЛДАМЧИ]
САБАБ: [қисқа изоҳ]
"""

REPORT_PROMPT = """
Қуйидаги текширилган даъволар асосида якуний ҳисобот тайёрла.
Ҳисобот {lang} тилида, кирилл ёзувида бўлсин.

Текширилган даъволар: {verified}

Форматни қатъий сақла:
✅ ТЎҒРИ:
[рўйхат]

⚠️ ОҒИРИЛГАН:
[рўйхат]

❌ НОТЎҒРИ/АЛДАМЧИ:
[рўйхат]

💡 АМАЛИЙ ҚИЙМАТ:
[қисқа хулоса]
"""


def extract_claims_node(state: AnalysisState) -> AnalysisState:
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": EXTRACT_CLAIMS_PROMPT.format(text=state["text"])}]
    )
    claims_text = response.choices[0].message.content
    state["claims"] = [line.strip() for line in claims_text.split("\n") if line.strip()]
    return state


def verify_claim_node(state: AnalysisState) -> AnalysisState:
    verified = []
    for claim in state["claims"]:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": VERIFY_CLAIM_PROMPT.format(claim=claim)}]
        )
        verified.append({"claim": claim, "result": response.choices[0].message.content})
    state["verified_claims"] = verified
    return state


def generate_report_node(state: AnalysisState) -> AnalysisState:
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": REPORT_PROMPT.format(
            lang=state["lang"], verified=state["verified_claims"]
        )}]
    )
    state["final_report"] = response.choices[0].message.content
    return state


graph = StateGraph(AnalysisState)
graph.add_node("extract_claims", extract_claims_node)
graph.add_node("verify_claim", verify_claim_node)
graph.add_node("generate_report", generate_report_node)

graph.set_entry_point("extract_claims")
graph.add_edge("extract_claims", "verify_claim")
graph.add_edge("verify_claim", "generate_report")
graph.add_edge("generate_report", END)

analyzer_graph = graph.compile()


def analyze_with_graph(text: str, lang: str = "lang_kirill") -> str:
    result = analyzer_graph.invoke({"text": text, "lang": lang})
    return result["final_report"]