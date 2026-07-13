import html
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

_BRACKET_RE = re.compile(r"([\[【（(])([^\]】）)]{1,120})[\]】）)]")
_EVENT_RE = re.compile(
    r"^(?:c\d+[a-z]?|comiket\s*\d+|comic\s*market\s*\d+|"
    r"comitia\s*\d+|コミティア\s*\d+|ac\d+|gw超同人祭)$",
    re.IGNORECASE,
)
_VARIANT_RE = re.compile(
    r"汉化|漢化|翻译|翻譯|翻訳|中国翻訳|英訳|上色|全彩|カラー|"
    r"full[ -]?colou?r|无码|無碼|无修正|無修正|uncensored|decensored|"
    r"digital|dl\s*版|complete|incomplete|中文|chinese|english|japanese|translated",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TitleIdentity:
    raw_title: str
    normalized_title: str
    identity_core: str
    context_terms: tuple[str, ...]


def normalize_identity_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    normalized = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+", " ", normalized)
    return " ".join(normalized.split()).strip()


def parse_title_identity(raw_title: str, normalized_title: str) -> TitleIdentity:
    contexts: list[str] = []
    for match in _BRACKET_RE.finditer(
        unicodedata.normalize("NFKC", html.unescape(raw_title))
    ):
        content = " ".join(match.group(2).split()).strip()
        normalized_content = normalize_identity_text(content)
        if not normalized_content or _VARIANT_RE.search(content) or _EVENT_RE.match(content):
            continue
        is_leading_creator = match.start() <= 1 and match.group(1) in {"[", "【"}
        if is_leading_creator:
            continue
        contexts.append(normalized_content)

    core = normalize_identity_text(normalized_title)
    for context in sorted(set(contexts), key=len, reverse=True):
        core = re.sub(
            rf"(?<!\w){re.escape(context)}(?!\w)", " ", core, flags=re.IGNORECASE
        )
    core = " ".join(core.split()).strip() or normalize_identity_text(normalized_title)
    return TitleIdentity(
        raw_title=raw_title,
        normalized_title=normalize_identity_text(normalized_title),
        identity_core=core,
        context_terms=tuple(dict.fromkeys(contexts)),
    )


def identity_names(
    raw_title: str, normalized_title: str, aliases: list[str] | tuple[str, ...]
) -> set[str]:
    names = {parse_title_identity(raw_title, normalized_title).identity_core}
    names.update(
        parse_title_identity(alias, alias).identity_core for alias in aliases if alias
    )
    return {name for name in names if name}


def weighted_text_similarity(left: str, right: str) -> float:
    left_value = normalize_identity_text(left)
    right_value = normalize_identity_text(right)
    if not left_value or not right_value:
        return 0.0
    if left_value == right_value:
        return 1.0
    sequence_score = SequenceMatcher(None, left_value, right_value).ratio()
    ngram_score = _dice(_ngrams(left_value), _ngrams(right_value))
    token_score = _dice(set(left_value.split()), set(right_value.split()))
    structural_score = max(ngram_score, token_score)
    return round(sequence_score * 0.62 + structural_score * 0.38, 5)


def best_identity_similarity(left_names: set[str], right_names: set[str]) -> float:
    return max(
        (
            weighted_text_similarity(left, right)
            for left in left_names
            for right in right_names
        ),
        default=0.0,
    )


def shared_context(left: TitleIdentity, right: TitleIdentity) -> list[str]:
    return sorted(set(left.context_terms) & set(right.context_terms))


def _ngrams(value: str, size: int = 2) -> set[str]:
    compact = re.sub(r"\s+", "", value)
    if len(compact) < size:
        return {compact} if compact else set()
    return {compact[index : index + size] for index in range(len(compact) - size + 1)}


def _dice(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))
