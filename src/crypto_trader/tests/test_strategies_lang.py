"""D33 Arabic/English: every Arabic phrase the UI can show has an English translation, and language is display-only."""
from __future__ import annotations

import json
import re
from pathlib import Path

STRAT = Path(__file__).resolve().parents[1] / "strategies"
STATIC = STRAT / "ui" / "static"
AR = re.compile(r"[؀-ۿ]")
SEG = re.compile(r"[؀-ۿ][؀-ۿً-ٰٟ\s«»،؛؟\-…:()0-9%.+/×·—–]*[؀-ۿ»)؟.]|[؀-ۿ]")
SOURCES = [STATIC / f for f in ("app.js", "decisions.js", "demo.js", "i18n.js", "index.html")] + \
          [STRAT / "ui" / f for f in ("store.py", "decisions.py", "demo.py", "server.py")] + [STRAT / "analysis.py"]


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
    for f in ("app.js", "decisions.js", "demo.js", "lang.js"):
        src = (STATIC / f).read_text(encoding="utf-8")
        assert not re.search(r"api\([^)]*(lang|LANG)", src), f
        assert "ct-lang" not in src or f == "lang.js"
    for f in ("server.py", "store.py", "decisions.py", "demo.py"):
        # the static file name "lang.js" (served as a file) is not a language parameter
        assert not re.search(r"\blang(uage)?\b(?!\.js)", (STRAT / "ui" / f).read_text(encoding="utf-8")), f
