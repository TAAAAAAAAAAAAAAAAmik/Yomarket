<title>Правовые документы YooMarket</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bitter:wght@500;600;700&family=Manrope:wght@400;500;600;700&display=swap">
<style>
  :root{
    --ground:#EDF1F0; --surface:#FFFFFF; --sunk:#E2E9E7;
    --ink:#111A1B; --body:#26383A; --muted:#5F7274; --hair:#CBD8D6;
    --accent:#1B5D58; --accent-soft:#DCEAE7;
    --warn:#8A3527; --warn-soft:#F6E6E2;
    --shadow:0 1px 2px rgba(17,26,27,.06),0 8px 24px -12px rgba(17,26,27,.18);
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      --ground:#0C1213; --surface:#141D1E; --sunk:#0A0F10;
      --ink:#E7EFED; --body:#C2D2D0; --muted:#8497 96; --muted:#849796;
      --hair:#26383A; --accent:#79C4BA; --accent-soft:#14312E;
      --warn:#E39C89; --warn-soft:#33201B;
      --shadow:0 1px 2px rgba(0,0,0,.5),0 8px 24px -12px rgba(0,0,0,.7);
    }
  }
  :root[data-theme="dark"]{
    --ground:#0C1213; --surface:#141D1E; --sunk:#0A0F10;
    --ink:#E7EFED; --body:#C2D2D0; --muted:#849796;
    --hair:#26383A; --accent:#79C4BA; --accent-soft:#14312E;
    --warn:#E39C89; --warn-soft:#33201B;
    --shadow:0 1px 2px rgba(0,0,0,.5),0 8px 24px -12px rgba(0,0,0,.7);
  }

  *{box-sizing:border-box}
  body{
    background:var(--ground); color:var(--body);
    font-family:Manrope,"Segoe UI",system-ui,sans-serif;
    font-size:16px; line-height:1.65;
    -webkit-text-size-adjust:100%;
  }
  .wrap{max-width:44rem;margin:0 auto;padding:0 1.1rem 5rem}

  /* --- шапка ------------------------------------------------------- */
  .top{
    position:sticky; top:0; z-index:5;
    background:color-mix(in srgb,var(--ground) 88%,transparent);
    backdrop-filter:blur(10px);
    border-bottom:1px solid var(--hair);
  }
  .topin{max-width:44rem;margin:0 auto;padding:.55rem 1.1rem}
  .brand{
    display:flex;align-items:baseline;gap:.55rem;
    font-family:Bitter,Georgia,serif;font-weight:700;
    color:var(--ink);font-size:.98rem;letter-spacing:-.01em;
  }
  .brand small{
    font-family:Manrope,sans-serif;font-weight:600;font-size:.66rem;
    letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
  }
  nav{display:flex;gap:.3rem;margin-top:.5rem;overflow-x:auto;
      scrollbar-width:none}
  nav::-webkit-scrollbar{display:none}
  .tab{
    flex:0 0 auto;font:inherit;font-size:.8rem;font-weight:600;
    padding:.4rem .72rem;border-radius:999px;cursor:pointer;
    border:1px solid var(--hair);background:var(--surface);color:var(--muted);
    transition:background .15s,color .15s,border-color .15s;
  }
  .tab:hover{color:var(--ink)}
  .tab[aria-selected="true"]{
    background:var(--accent);border-color:var(--accent);
    color:var(--surface);
  }
  :root[data-theme="dark"] .tab[aria-selected="true"],
  @media (prefers-color-scheme: dark){}
  .tab:focus-visible,.copy:focus-visible{outline:2px solid var(--accent);
    outline-offset:2px}

  /* --- памятка владельцу ------------------------------------------- */
  .note{
    margin:1.4rem 0 0;padding:1rem 1.1rem;background:var(--surface);
    border:1px solid var(--hair);border-radius:.6rem;box-shadow:var(--shadow);
  }
  .note h2{
    font-family:Bitter,Georgia,serif;font-size:.95rem;margin:0 0 .5rem;
    color:var(--ink);
  }
  .note ol{margin:0;padding-left:1.15rem;font-size:.9rem}
  .note li{margin:.3rem 0}
  .note code{font-size:.85em}

  /* --- документ ----------------------------------------------------- */
  .doc{margin-top:1.6rem}
  .dochead{
    padding-bottom:1.1rem;margin-bottom:1.4rem;
    border-bottom:2px solid var(--ink);
  }
  h1{
    font-family:Bitter,Georgia,serif;font-weight:700;
    font-size:clamp(1.5rem,5.5vw,2rem);line-height:1.15;
    letter-spacing:-.02em;color:var(--ink);margin:0;text-wrap:balance;
  }
  .meta{font-size:.82rem;color:var(--muted);margin:.6rem 0 0}
  .meta strong{color:var(--body);font-weight:600}
  .acts{display:flex;align-items:center;gap:.7rem;margin-top:.95rem;
        flex-wrap:wrap}
  .copy{
    font:inherit;font-size:.85rem;font-weight:600;
    padding:.5rem .95rem;border-radius:.45rem;cursor:pointer;
    background:var(--accent);color:#fff;border:1px solid var(--accent);
  }
  :root[data-theme="dark"] .copy{color:#08110F}
  .copy:active{transform:translateY(1px)}
  .said{font-size:.8rem;color:var(--accent);font-weight:600}

  h2{
    display:flex;gap:.6rem;align-items:baseline;
    font-family:Bitter,Georgia,serif;font-weight:600;
    font-size:1.12rem;line-height:1.3;color:var(--ink);
    margin:2.4rem 0 .8rem;text-wrap:balance;
  }
  h2 .num{
    flex:0 0 auto;font-size:.78rem;font-weight:700;color:var(--accent);
    font-variant-numeric:tabular-nums;padding-top:.18rem;
  }
  .body p{margin:0 0 .85rem}
  .body ul{margin:0 0 .95rem;padding-left:1.15rem}
  .body li{margin:.32rem 0}
  .body strong{color:var(--ink);font-weight:700}
  code{
    font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
    font-size:.86em;background:var(--sunk);padding:.1em .35em;
    border-radius:.25em;color:var(--ink);
  }

  /* Пункты, где ошибка стоит денег или доступа к кошельку. */
  p.money{
    border-left:3px solid var(--warn);padding:.1rem 0 .1rem .85rem;
    background:linear-gradient(90deg,var(--warn-soft),transparent 70%);
    border-radius:0 .3rem .3rem 0;
  }
  p.money strong{color:var(--warn)}
  .pin{
    margin-top:1.8rem;padding:.95rem 1.1rem;background:var(--accent-soft);
    border-radius:.5rem;font-weight:600;color:var(--ink);
  }

  .scroll{overflow-x:auto;margin:0 0 1.1rem;
          border:1px solid var(--hair);border-radius:.5rem}
  table{border-collapse:collapse;width:100%;font-size:.83rem;
        background:var(--surface)}
  th,td{padding:.55rem .7rem;text-align:left;vertical-align:top;
        border-bottom:1px solid var(--hair)}
  th{font-weight:700;color:var(--ink);background:var(--sunk);
     white-space:nowrap}
  tbody tr:last-child td{border-bottom:0}

  .src{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}

  @media (prefers-reduced-motion:reduce){
    *{transition:none!important;animation:none!important}
  }
</style>

<header class="top">
  <div class="topin">
    <div class="brand">YooMarket <small>правовые документы</small></div>
    <nav role="tablist"><!--NAV--></nav>
  </div>
</header>

<main class="wrap">
  <section class="note">
    <h2>Как это опубликовать</h2>
    <ol>
      <li>Нажмите «Скопировать текст» на нужном документе.</li>
      <li>Вставьте в <code>telegra.ph</code> — заголовки и жирный шрифт
          сохранятся. Получите ссылку.</li>
      <li>В боте: <strong>👑 Админ-панель → 📄 Правовые документы</strong> —
          вставьте ссылку. Кнопка появится в <code>/policy</code> только у
          того документа, у которого ссылка задана.</li>
    </ol>
  </section>
  <!--DOCS-->
</main>

<script>
(function () {
  var tabs = [].slice.call(document.querySelectorAll(".tab"));
  var docs = [].slice.call(document.querySelectorAll(".doc"));

  function show(key, remember) {
    tabs.forEach(function (t) {
      t.setAttribute("aria-selected", String(t.dataset.doc === key));
    });
    docs.forEach(function (d) { d.hidden = d.id !== key; });
    if (remember) {
      try { localStorage.setItem("yoomarket-legal-doc", key); } catch (e) {}
    }
  }

  tabs.forEach(function (t) {
    t.setAttribute("role", "tab");
    t.addEventListener("click", function () { show(t.dataset.doc, true); });
  });

  var start = tabs[0].dataset.doc;
  try {
    var saved = localStorage.getItem("yoomarket-legal-doc");
    if (saved && document.getElementById(saved)) { start = saved; }
  } catch (e) {}
  show(start, false);

  // Копирование. В песочнице буфер обмена бывает закрыт, поэтому путей три,
  // и последний — честно сказать, что скопировать придётся вручную, а не
  // молча ничего не сделать.
  function say(key, text) {
    var el = document.querySelector('[data-said="' + key + '"]');
    if (!el) { return; }
    el.textContent = text;
    setTimeout(function () { el.textContent = ""; }, 4000);
  }

  document.querySelectorAll(".copy").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var key = btn.dataset.doc;
      var rich = document.querySelector("#" + key + " .body");
      var area = document.getElementById("src-" + key);

      function selectRich() {
        var sel = window.getSelection();
        var range = document.createRange();
        range.selectNodeContents(rich);
        sel.removeAllRanges();
        sel.addRange(range);
      }

      function legacy() {
        selectRich();
        var ok = false;
        try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
        say(key, ok ? "Скопировано" :
            "Текст выделен — скопируйте его сами");
        return ok;
      }

      if (navigator.clipboard && window.ClipboardItem) {
        var payload = {};
        payload["text/html"] = new Blob([rich.innerHTML], {type: "text/html"});
        payload["text/plain"] = new Blob([area.value], {type: "text/plain"});
        navigator.clipboard.write([new ClipboardItem(payload)]).then(
          function () { say(key, "Скопировано — вставляйте в telegra.ph"); },
          legacy
        );
      } else {
        legacy();
      }
    });
  });
})();
</script>
