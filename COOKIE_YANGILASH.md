# Instagram Cookie Янгилаш Йўриқномаси

## Қачон янгилаш керак?
- Бот сизга Telegram да хабар юборганда:
  "⚠️ Instagram cookie eskirdi!"
- Instagram Reels таҳлили ишламай қолганда
- Ҳар 1-2 ойда профилактика учун

## 1-Босқич — Браузерда кириш
1. Chrome да instagram.com очинг
2. reelsbot2026 akkauntiga kiring
3. Лентангиз кўринса — тайёр

## 2-Босқич — Cookie экспорт
1. Chrome юқори ўнгда пазл белгиси 🧩 босинг
2. "Get cookies.txt LOCALLY" босинг
3. Export Format: Netscape
4. "Export" тугмасини босинг
5. Файл Downloads папкасига тушади

## 3-Босқич — Файлни кўчириш
PowerShell да:
```powershell
copy "C:\Users\LEGiON\Downloads\www.instagram.com_cookies (N).txt" D:\Projects\ReelsTahlilBot\cookies.txt
```
(N — энг катта рақамли файлни танланг)

## 4-Босқич — Railway га юклаш
PowerShell да кетма-кет бажаринг:
```powershell
cd D:\Projects\ReelsTahlilBot
$cookie = [Convert]::ToBase64String([IO.File]::ReadAllBytes("D:\Projects\ReelsTahlilBot\cookies.txt"))
railway variable set INSTAGRAM_COOKIES="$cookie" --skip-deploys
railway redeploy
```
"y" bosing → Enter

## 5-Босқич — Текшириш
2 дақиқа кутинг, кейин ботга юборинг:

- Telegram'да @ReelsAnalyzerBot'га битта Instagram Reels ҳаволасини юборинг.
- Транскрипция ва таҳлил натижаси келса — тайёр, cookie муваффақиятли ишлади.
