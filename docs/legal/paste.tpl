<title>Правовые тексты YooMarket</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&family=PT+Serif:ital,wght@0,400;0,700;1,400&display=swap">
<style>
:root{
  --ground:#EDF0F4; --paper:#FFFFFF; --ink:#171A1F; --muted:#5C6673;
  --line:#D3DAE3; --edge:#C2CBD6; --accent:#2E7D6B; --accent-ink:#FFFFFF;
  --warn:#7A5C15; --warn-bg:#FBF2DC; --shadow:0 1px 2px rgba(23,26,31,.07);
  --ui:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --serif:"PT Serif",Georgia,"Times New Roman",serif;
  --mono:"IBM Plex Mono",ui-monospace,"SFMono-Regular",Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ground:#0E1013; --paper:#1A1D23; --ink:#E6E9EE; --muted:#98A1AE;
    --line:#2A2F38; --edge:#39404B; --accent:#4FA891; --accent-ink:#0E1013;
    --warn:#D6A83C; --warn-bg:#2A2413; --shadow:0 1px 2px rgba(0,0,0,.4);
  }
}
:root[data-theme="dark"]{
  --ground:#0E1013; --paper:#1A1D23; --ink:#E6E9EE; --muted:#98A1AE;
  --line:#2A2F38; --edge:#39404B; --accent:#4FA891; --accent-ink:#0E1013;
  --warn:#D6A83C; --warn-bg:#2A2413; --shadow:0 1px 2px rgba(0,0,0,.4);
}
*{box-sizing:border-box}
body{background:var(--ground);color:var(--ink);font-family:var(--ui);
     line-height:1.55;margin:0;padding:24px 16px 64px}
.wrap{max-width:44rem;margin:0 auto;display:flex;flex-direction:column;gap:28px}

header h1{font-family:var(--serif);font-size:1.75rem;line-height:1.2;margin:0;
          text-wrap:balance;letter-spacing:-.01em}
header p{color:var(--muted);margin:8px 0 0;max-width:34rem}

.steps{display:flex;flex-direction:column;gap:0;border:1px solid var(--line);
       border-radius:10px;background:var(--paper);box-shadow:var(--shadow);
       overflow:hidden}
.step{display:grid;grid-template-columns:2rem 1fr;gap:12px;padding:14px 16px;
      align-items:baseline}
.step + .step{border-top:1px solid var(--line)}
.step b{font-family:var(--mono);font-size:.8rem;font-weight:500;
        color:var(--accent);letter-spacing:.02em}
.step .what{font-weight:500}
.step .how{color:var(--muted);font-size:.92rem;display:block;margin-top:2px}

.byline{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:6px}
.byline code{font-family:var(--mono);font-size:.95rem;background:var(--ground);
             border:1px solid var(--edge);border-radius:6px;padding:3px 8px;
             color:var(--ink)}

section{border:1px solid var(--line);border-radius:10px;background:var(--paper);
        box-shadow:var(--shadow);overflow:hidden}
.bar{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center;
     justify-content:space-between;padding:14px 16px;
     border-bottom:1px solid var(--line)}
.bar h2{font-family:var(--serif);font-size:1.12rem;margin:0;line-height:1.3}
.meta{font-family:var(--mono);font-size:.76rem;color:var(--muted);
      font-variant-numeric:tabular-nums;display:block;margin-top:3px}
button.copy{font-family:var(--ui);font-size:.9rem;font-weight:500;
            background:var(--accent);color:var(--accent-ink);border:0;
            border-radius:7px;padding:9px 15px;cursor:pointer;
            white-space:nowrap;transition:opacity .15s}
button.copy:hover{opacity:.88}
button.copy:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
button.copy[data-state="done"]{background:transparent;color:var(--accent);
            box-shadow:inset 0 0 0 1px var(--accent)}
button.copy[data-state="fail"]{background:transparent;color:var(--warn);
            box-shadow:inset 0 0 0 1px var(--warn)}

.note{display:flex;gap:8px;padding:10px 16px;background:var(--warn-bg);
      color:var(--warn);font-size:.88rem;border-bottom:1px solid var(--line)}

.doc{max-height:20rem;overflow-y:auto;padding:4px 16px 16px;
     font-family:var(--serif);font-size:.97rem;line-height:1.62;
     color:var(--ink)}
.doc h3{font-family:var(--ui);font-size:1rem;font-weight:600;
        margin:1.6em 0 .5em;line-height:1.3}
.doc h4{font-family:var(--ui);font-size:.92rem;font-weight:600;
        margin:1.3em 0 .4em;color:var(--muted)}
.doc p{margin:0 0 .85em}
.doc ul,.doc ol{margin:0 0 .9em;padding-left:1.35em}
.doc li{margin-bottom:.35em}
.doc code{font-family:var(--mono);font-size:.86em}
.doc hr{border:0;border-top:1px solid var(--line);margin:1.4em 0}
.fade{padding:8px 16px 14px;font-size:.8rem;color:var(--muted);
      border-top:1px solid var(--line)}
footer{color:var(--muted);font-size:.88rem;max-width:34rem}
footer code{font-family:var(--mono);font-size:.86em}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
<div class="wrap">
<header>
  <h1>Правовые тексты для Telegraph</h1>
  <p>Три документа, разобранные из файлов репозитория тем же кодом, каким их
     выложил бы <code>publish_legal.py</code>. Разметка уже разобрана: копируется
     форматированный текст, а не звёздочки.</p>
</header>

<div class="steps">
  <div class="step"><b>01</b><div><span class="what">Заголовок</span>
    <span class="how">Верхнее поле новой страницы — впиши название документа
    из шапки ниже.</span></div></div>
  <div class="step"><b>02</b><div><span class="what">Автор</span>
    <span class="how">Строка под заголовком. Впиши имя и ничего больше —
    ссылку не добавляй, иначе подпись снова станет кликабельной.</span>
    <span class="byline"><code>YooMarket Manager</code>
    <button class="copy" data-copy="author" data-plain="YooMarket Manager">Скопировать имя</button></span></div></div>
  <div class="step"><b>03</b><div><span class="what">Текст</span>
    <span class="how">Кнопка «Скопировать текст» кладёт документ с разметкой —
    заголовки, жирное и списки Telegraph сохранит при вставке.</span></div></div>
</div>

<!--DOCS-->

<footer>
  <p>Подпись — отдельное поле Telegraph, а не часть текста: страница, созданная
  без него, подписывается ником создателя и вешает на него ссылку на профиль.</p>
  <p>Адреса готовых страниц впиши в бота:
  <b>👑 Админ-панель → 📄 Правовые документы</b>. Бот ссылку не проверяет —
  со старым адресом кнопка молча откроет прежнюю страницу.</p>
</footer>
</div>

<script>
const say = (btn, text, state) => {
  btn.textContent = text;
  btn.dataset.state = state;
  setTimeout(() => { btn.textContent = btn.dataset.idle; btn.dataset.state = ""; }, 2600);
};

async function copyRich(btn, html, plain) {
  try {
    if (navigator.clipboard && window.ClipboardItem) {
      await navigator.clipboard.write([new ClipboardItem({
        "text/html": new Blob([html], {type: "text/html"}),
        "text/plain": new Blob([plain], {type: "text/plain"})
      })]);
      return true;
    }
  } catch (e) { /* ниже — запасной путь */ }
  try {
    await navigator.clipboard.writeText(plain);
    return true;
  } catch (e) { return false; }
}

document.querySelectorAll("button.copy").forEach(btn => {
  btn.dataset.idle = btn.textContent;
  btn.addEventListener("click", async () => {
    const key = btn.dataset.copy;
    let html, plain;
    if (btn.dataset.plain) {
      html = plain = btn.dataset.plain;
    } else {
      const box = document.getElementById("doc-" + key);
      html = box.innerHTML;
      plain = box.innerText;
    }
    const ok = await copyRich(btn, html, plain);
    say(btn, ok ? "Скопировано" : "Не вышло — выдели текст и скопируй сам",
        ok ? "done" : "fail");
  });
});
</script>
