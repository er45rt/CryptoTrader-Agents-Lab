"""D33 Arabic/English: every Arabic phrase the UI can show has an English translation, and language is display-only."""
from __future__ import annotations

import json
import re
from pathlib import Path

STRAT = Path(__file__).resolve().parents[1] / "strategies"
STATIC = STRAT / "ui" / "static"
AR = re.compile(r"[؀-ۿ]")
SEG = re.compile(r"[؀-ۿ][؀-ۿً-ٰٟ\s«»،؛؟\-…:()0-9%.+/×·—–]*[؀-ۿ»)؟.]|[؀-ۿ]")
SOURCES = [STATIC / f for f in ("app.js", "decisions.js", "demo.js", "portfolio.js", "dashboard.js", "i18n.js", "index.html")] + \
          [STRAT / "ui" / f for f in ("store.py", "decisions.py", "demo.py", "server.py", "portfolio.py", "portfolio_csv.py",
                                      "dca.py", "prices.py", "launcher.py", "vault.py")] + [STRAT / "analysis.py"]


def _segments() -> dict[str, str]:
    out: dict[str, str] = {}
    for f in SOURCES:
        if not f.exists():
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if not AR.search(line) or s.startswith(("#", "//", "/*", "*", '"""', "<!--")) or "\u0600" in line:   # regex ranges are code
                continue
            for m in SEG.finditer(line):
                seg = m.group(0).strip(" :·—–-")
                if AR.search(seg):
                    out.setdefault(seg, f"{f.name}:{n}")
    return out


def _translate(text: str, en: dict[str, str]) -> str:
    """The same rule as lang.js tr(): longest phrase first, whole Arabic words only, then punctuation."""
    keys = sorted(en, key=len, reverse=True)
    rx = re.compile(r"(?<![؀-ۿ])(" + "|".join(re.escape(k) for k in keys) + r")(?![؀-ۿ])")
    out = rx.sub(lambda m: en[m.group(0)], text)
    return out.translate(str.maketrans({"،": ",", "؛": ";", "؟": "?", "«": '"', "»": '"'}))


def test_every_arabic_phrase_in_the_ui_has_an_english_translation():
    en = json.loads((STATIC / "i18n_en.json").read_text(encoding="utf-8"))
    assert not [v for v in en.values() if AR.search(v)], "English values must not contain Arabic"
    missing = {seg: where for seg, where in _segments().items() if AR.search(_translate(seg, en))}
    assert not missing, f"{len(missing)} Arabic phrases without English: " + "; ".join(f"{k!r} ({v})" for k, v in list(missing.items())[:25])


def test_language_is_display_only():
    # the language lives in the browser (localStorage) and is never sent to the server
    for f in ("app.js", "decisions.js", "demo.js", "portfolio.js", "lang.js"):
        src = (STATIC / f).read_text(encoding="utf-8")
        assert not re.search(r"api\([^)]*(lang|LANG)", src), f
        assert "ct-lang" not in src or f == "lang.js"
    for f in ("server.py", "store.py", "decisions.py", "demo.py", "portfolio.py", "portfolio_csv.py", "dca.py", "prices.py"):
        # the static file name "lang.js" (served as a file) is not a language parameter
        assert not re.search(r"\blang(uage)?\b(?!\.js)", (STRAT / "ui" / f).read_text(encoding="utf-8")), f


_NODE_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const queue = [];                       // pending MutationRecords, delivered like the browser does
let observer = null, writes = 0;
function textNode(v) {
  let value = v;
  return { nodeType: 3, parentElement: { tagName: "SPAN" },
    get nodeValue() { return value; },
    set nodeValue(x) { value = x; writes++; queue.push({ type: "characterData", target: this }); } };
}
const body = { querySelectorAll: () => [], getAttribute: () => null };
const ctx = {
  localStorage: { getItem: () => "en", setItem() {} },
  fetch: async () => ({ json: async () => JSON.parse(process.argv[2]) }),
  document: { body, documentElement: {}, getElementById: () => null,
              createTreeWalker: () => ({ nextNode: () => false }) },
  NodeFilter: { SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2 },
  MutationObserver: class { constructor(cb) { observer = cb; } observe() {} },
  location: { reload() {} },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8") + "\n;globalThis.__init = initLang;", ctx);
ctx.__init().then(() => {
  const nodes = JSON.parse(process.argv[3]).map(textNode);
  nodes.forEach((n) => queue.push({ type: "characterData", target: n }));
  let rounds = 0;
  while (queue.length && rounds < 50) { const batch = queue.splice(0); observer(batch); rounds++; }
  console.log(JSON.stringify({ writes, pending: queue.length, rounds, values: nodes.map((n) => n.nodeValue) }));
});
"""


def _run_lang_js(texts: list[str], en: dict[str, str]) -> dict:
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", _NODE_HARNESS, "--", str(STATIC / "lang.js"),
                          json.dumps(en, ensure_ascii=False), json.dumps(texts, ensure_ascii=False)],
                         capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_english_mode_does_not_rewrite_untranslatable_arabic_forever():
    # Sol D30-D33 r1 blocker 1: an Arabic strategy name with no English entry was rewritten on every
    # mutation, each write producing another mutation (unbounded loop). It must settle with no write at all.
    res = _run_lang_js(["تجربتي"], {"الرصيد": "Balance"})
    assert res["pending"] == 0 and res["writes"] == 0, res
    assert res["values"] == ["تجربتي"]


def test_english_mode_translates_once_then_settles():
    res = _run_lang_js(["الرصيد: تجربتي"], {"الرصيد": "Balance"})
    assert res["pending"] == 0 and res["writes"] == 1, res
    assert res["values"] == ["Balance: تجربتي"]
