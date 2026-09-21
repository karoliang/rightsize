"""Bounded task-classification heuristic for the offline stand-in path.

Replaces the regex table inside ``rightsize.heuristic`` when the typed judgment
is unavailable. Returns the same keys and default scales as the original:

    tier, tier_confidence, size, second_opinion, spec_complete, destructive

The only behavioural narrowing is that risk words embedded in URL, path,
filename or markdown-link metadata do not trip tier detection, and the
``destructive`` flag only crosses the confirm threshold on a positive
irreversible action that is not negated inside the same clause. Tier
precedence, size escalation and the ``second_opinion`` defaults are kept
verbatim from the original.

A small metadata-stripping pass identifies URL, markdown link target, Windows path,
Unix absolute path and ``word.word`` filename tokens. Risk-word and
irreversible-action matches whose start position falls inside any metadata
span are skipped. Code fences are NOT stripped wholesale: their contents are
real commands and may themselves be the operation the spec describes.

Negation is checked clause-locally: the match position's clause is the text
between the last sentence/segment separator at or before it (one of ``.!?\n;,``)
and the match itself, resetting at ``and / but / then``. A clause containing ``do not / don't / never / avoid /
without`` or ``no <action>`` before the action match is treated as negated.
"""
import re


HEURISTIC = [
    (r"\bmigrat|\bdrop table|\bauth|\bpassword|\bsecret|\bpayment|\bstripe|\bmoney|\bdeploy",
     "high_stakes"),
    (r"\bdesign|\barchitect|\bplan\b|\bschema\b|\brefactor across|\bapi shape", "design"),
    (r"\bdebug|\bflaky|\bintermittent|\bcannot reproduce|\bno repro|\bwhy does",
     "diagnosis"),
    (r"\brename\b|\btypo\b|\bformat\b|\bcomment\b|\blint\b", "mechanical"),
]

# Real irreversible operations. Each must hit prose (or a code fence), not
# metadata, and not be negated in the same clause.
DESTRUCTIVE_OPS = (
    r"\bdrop\s+(?:table|database|column|schema|index|view|partition|sequence)",
    r"\btruncate\b",
    r"\b(?:wipe|purge|destroy)\s+(?:the\s+)?(?:data|database|table|production|prod|cluster)",
    r"\breset\s+(?:the\s+)?(?:database|cluster|production|prod)\b",
    r"\bdelete\s+from\s+(?:production|prod|users|customers|orders)\b",
    r"\bdeploy\b",
    r"\bpublish\b",
    r"\bforce[\s\-]+push\b",
    r"\bgit\s+push\s+--force\b",
    r"\bgit\s+push\s+-f\b",
    r"\boverwrite\s+(?:the\s+)?production\b",
    r"\boverwrite\s+(?:production|production\s+data)\b",
)

NEGATION = re.compile(
    r"\b(?:do\s+not|don'?t|never|avoid|without)\b"
    r"|"
    r"\bno\s+(?:publish|publishing|deploy|deployment|drop|dropping|push|pushing|"
    r"overwrite|delete|deleting|destroy|truncate|reset|wipe|clear|purge|"
    r"force-?push)\b",
    re.IGNORECASE,
)

# Spans whose contents are metadata, not task prose. Order matters only for
# diagnostics; overlaps are tolerated because position-based membership is the
# only thing checked against them.
METADATA_PATTERNS = (
    r"https?://[^\s\)\]\>\"']+",
    r"\b[a-z][a-z0-9+.\-]*://[^\s\)\]\>\"']+",
    r"(?<=\]\()[^)\n]*(?=\))",
    r"[A-Za-z]:\\[^\s\"'\n]+",
    r"\b[\w.-]+\\[^\s\"'\n]+",
    r"\"[A-Za-z]:\\[^\"\n]+\"",
    r'"/[^"\n]+"',
    r"'/[^'\n]+'",
    r"(?:^|(?<=[\s(\[]))/[^\s)\]\>]+",
    r"\b\w[\w-]*\.\w+\b",
)

SIZE_ESCALATES = (r"\bacross\b", r"\ball \b", r"\bevery \b", r"\brepo-wide\b")


def _metadata_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in METADATA_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            spans.append((match.start(), match.end()))
    return spans


def _in_metadata(spans: list[tuple[int, int]], position: int) -> bool:
    for start, end in spans:
        if start <= position < end:
            return True
    return False


def _clause_boundary(text: str, position: int) -> int:
    boundary = -1
    for sep in ".!?\n;,":
        idx = text.rfind(sep, 0, position)
        if idx > boundary:
            boundary = idx
    return boundary


def _is_negated(text: str, match_start: int) -> bool:
    boundary = _clause_boundary(text, match_start)
    clause_before = re.split(r"\b(?:and|but|then)\b", text[boundary + 1:match_start], flags=re.IGNORECASE)[-1]
    return bool(NEGATION.search(clause_before))


def _classify_tier(lower: str, spans: list[tuple[int, int]]) -> str:
    for pattern, label in HEURISTIC:
        for match in re.finditer(pattern, lower, flags=re.IGNORECASE):
            if _in_metadata(spans, match.start()):
                continue
            return label
    return "implementation"


def _is_destructive(original: str, spans: list[tuple[int, int]]) -> bool:
    lower = original.lower()
    for pattern in DESTRUCTIVE_OPS:
        for match in re.finditer(pattern, lower, flags=re.IGNORECASE):
            if _in_metadata(spans, match.start()):
                continue
            if _is_negated(original, match.start()):
                continue
            return True
    return False


def _size_score(lower: str, spans: list[tuple[int, int]]) -> float:
    for pattern in SIZE_ESCALATES:
        for match in re.finditer(pattern, lower, flags=re.IGNORECASE):
            if _in_metadata(spans, match.start()):
                continue
            return 2.0
    return 0.6


def heuristic(spec: str) -> dict:
    """Offline stand-in for the typed judgment.

    Returns the same dict shape and default scales as
    ``rightsize.heuristic``; only tier detection against metadata and the
    destructive flag's coupling to high_stakes are tightened.
    """
    spans = _metadata_spans(spec)
    lower = spec.lower()
    tier = _classify_tier(lower, spans)
    size = _size_score(lower, spans)
    destructive = 0.7 if _is_destructive(spec, spans) else 0.1
    return {
        "tier": tier,
        "tier_confidence": None,
        "size": size,
        "second_opinion": 0.7 if tier in ("high_stakes", "design", "diagnosis") else 0.2,
        "spec_complete": 0.6,
        "destructive": destructive,
    }
