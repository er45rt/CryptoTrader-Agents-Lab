"use strict";
/* Arabic / English display (owner decision D33). The UI is written once, in Arabic; in English mode every Arabic
   phrase on the page is replaced from i18n_en.json (longest phrase first, whole Arabic words only) and the page
   switches to LTR. Display only: the language never reaches the server or the engine, so results cannot differ.
   Engine values that are English already (codes, engine messages, log lines) are shown in their original form. */

let LANG = "ar";
try { LANG = localStorage.getItem("ct-lang") === "en" ? "en" : "ar"; } catch { /* default Arabic */ }
let EN = {};
let EN_RE = null;
const PUNCT = { "،": ",", "؛": ";", "؟": "?", "«": "\"", "»": "\"" };

function buildRe() {
  const keys = Object.keys(EN).sort((a, b) => b.length - a.length).map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  EN_RE = keys.length ? new RegExp("(?<![\\u0600-\\u06FF])(" + keys.join("|") + ")(?![\\u0600-\\u06FF])", "g") : null;
}
/** Arabic text -> English (no-op in Arabic mode). Used for text the DOM pass cannot see (canvas). */
function tr(s) {
  if (LANG !== "en" || s == null) return s;
  let out = String(s);
  if (EN_RE) out = out.replace(EN_RE, (m) => EN[m] ?? m);
  return out.replace(/[،؛؟«»]/g, (c) => PUNCT[c]);
}
/** Translate one text node in place. Writes only when the text actually changes: an unchanged write would be
    observed again by the MutationObserver below and loop forever (e.g. an Arabic strategy name with no entry). */
function trText(n) {
  const v = n.nodeValue;
  if (!/[؀-ۿ]/.test(v)) return;
  const t = tr(v);
  if (t !== v) n.nodeValue = t;
}
const SKIP = new Set(["SCRIPT", "STYLE", "CODE"]);
function translateNode(root) {
  if (LANG !== "en" || !root) return;
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement && !SKIP.has(n.parentElement.tagName) && /[؀-ۿ]/.test(n.nodeValue)
      ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT),
  });
  const nodes = [];
  while (walk.nextNode()) nodes.push(walk.currentNode);
  nodes.forEach(trText);
  const els = root.querySelectorAll ? root.querySelectorAll("[title],[placeholder],option") : [];
  [root, ...els].forEach((el) => {
    if (!el.getAttribute) return;
    ["title", "placeholder"].forEach((a) => { const v = el.getAttribute(a); if (v && /[؀-ۿ]/.test(v) && tr(v) !== v) el.setAttribute(a, tr(v)); });
  });
}
function applyLangChrome() {
  const html = document.documentElement;
  html.lang = LANG;
  html.dir = LANG === "en" ? "ltr" : "rtl";
  document.title = LANG === "en" ? "CryptoTrader Engine" : "محرك CryptoTrader";
  const b = document.getElementById("lang-name");
  if (b) b.textContent = LANG === "en" ? "English" : "العربية";
}
async function initLang() {
  try {
    const r = await fetch("/i18n_en.json", { cache: "no-store" });
    EN = await r.json();
  } catch { EN = {}; }
  buildRe();
  applyLangChrome();
  if (LANG === "en") translateNode(document.body);
  new MutationObserver((muts) => {
    if (LANG !== "en") return;
    for (const m of muts) {
      if (m.type === "characterData") trText(m.target);
      else m.addedNodes.forEach((n) => (n.nodeType === 3 ? trText(n) : translateNode(n)));
    }
  }).observe(document.body, { childList: true, subtree: true, characterData: true });
}
function setLang(l) {
  LANG = l === "en" ? "en" : "ar";
  try { localStorage.setItem("ct-lang", LANG); } catch { /* not persisted */ }
  location.reload();   // re-render everything from the Arabic source in the chosen language
}
