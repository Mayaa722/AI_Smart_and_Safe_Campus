"""Text normalisation and a math-aware tokenizer with stopword removal (used by BM25)."""
from __future__ import annotations

import re
import unicodedata

STOPWORDS = set("""
a about above after again against all am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have
having he her here hers herself him himself his how i if in into is it its itself just let me more
most my myself no nor not now of off on once only or other our ours ourselves out over own same she
should so some such than that the their theirs them themselves then there these they this those
through to too under until up very was we were what when where which while who whom why will with
would you your yours yourself yourselves also using use used uses explain describe define tell show
give please lecture slide slides according
""".split())

# Hyphens and slashes are left out on purpose: they are mostly word punctuation on slides.
MATH_SYMBOLS = set("+*^=<>≤≥≠≈∑∏∫√π∂∞±×÷→←↔⇒⇔∈∉⊂⊆∪∩∀∃λμσθρΔ∇αβγδεω")
TOKEN = re.compile(r"\w+|[" + re.escape("".join(sorted(MATH_SYMBOLS))) + r"]")


# JSON escapes that swallow the first letter of a LaTeX command: "\text" -> TAB + "ext", "\frac" ->
# FORM FEED + "rac", "\beta" -> BACKSPACE + "eta", "\right" -> CR + "ight", "\vec" -> VT + "ec",
# "\neq" -> NEWLINE + "eq". Keys: control char -> (letter, the rest of the commands it can start).
_LATEX_REST = {
    "\t": ("t", "ext|extbf|extit|extrm|an|anh|au|heta|imes|o|op|riangle|ilde|frac|ag"),
    "\f": ("f", "rac|orall|lat|box"),
    "\b": ("b", "eta|ar|inom|ig|igl|igr|igg|oldsymbol|ot|egin|ullet|ackslash|mathbf"),
    "\r": ("r", "ight|ightarrow|ho|angle|m|mathrm|ceil|floor|Rightarrow|ightleftharpoons"),
    "\v": ("v", "ec|arepsilon|artheta|arphi|ee|dots|ert|Vert"),
    "\n": ("n", "eq|e|abla|u|ot|i|mid|exists|leq|geq|subseteq|supseteq|parallel|ewline|cr|eg"),
}
_LATEX_CONTROL = {c: re.compile(r"\\?" + re.escape(c) + r"(?=(?:" + rest + r")(?![A-Za-z]))")
                  for c, (_, rest) in _LATEX_REST.items()}
_MATH_SPAN = re.compile(r"\$\$.*?\$\$|\$[^$]{1,400}?\$|\\\(.*?\\\)|\\\[.*?\\\]", re.S)


def restore_latex_escapes(text: str) -> str:
    """Undo LaTeX commands that JSON turned into control characters (see _LATEX_REST). Only known
    command names are restored. Tab/form feed/backspace/CR/VT are restored anywhere (they never come
    before such a word in real text); a newline only inside math ($...$, $$...$$, \\(...\\), \\[...\\]),
    because elsewhere a newline before e.g. "e" or "u" is a real line break."""
    if not text or not any(c in text for c in _LATEX_REST):
        return text
    for c, pattern in _LATEX_CONTROL.items():
        if c != "\n" and c in text:
            text = pattern.sub(lambda m, c=c: "\\" + _LATEX_REST[c][0], text)
    if "\n" in text:
        text = _MATH_SPAN.sub(lambda m: _LATEX_CONTROL["\n"].sub(r"\\n", m.group(0)), text)
    return text


def restore_latex_in(obj):
    """restore_latex_escapes applied to every string in a parsed JSON value."""
    if isinstance(obj, str):
        return restore_latex_escapes(obj)
    if isinstance(obj, list):
        return [restore_latex_in(v) for v in obj]
    if isinstance(obj, dict):
        return {k: restore_latex_in(v) for k, v in obj.items()}
    return obj


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def tokens(text: str, remove_stopwords: bool = True) -> list[str]:
    result = TOKEN.findall(normalize(text))
    if remove_stopwords:
        result = [t for t in result if t not in STOPWORDS]
    return result


def squash(text: str) -> str:
    """Whitespace- and case-insensitive form, used to match quotes against slide text."""
    return " ".join(normalize(text).split())


def looks_garbled(text: str) -> bool:
    stripped = "".join(text.split())
    if len(stripped) < 20:
        return False
    odd = sum(1 for c in stripped if not (c.isalnum() or c in ".,;:()[]-'\"%/"))
    private = sum(1 for c in stripped if unicodedata.category(c) in ("Co", "Cn"))
    return private > 0 or odd / len(stripped) > 0.3


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))
