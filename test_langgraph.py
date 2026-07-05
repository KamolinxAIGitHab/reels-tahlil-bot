from bot.utils.langgraph_analyzer import analyze_with_graph

test_text = """
Ушбу видеода айтилишича, кичик бизнес эгалари сунъий интеллект
асбобларини қўллаб, ойига 5000 доллар қўшимча даромад топишлари
мумкин экан. Бунинг учун фақат ChatGPT'дан фойдаланиш кифоя.
"""

result = analyze_with_graph(test_text, lang="lang_kirill")
print(result)