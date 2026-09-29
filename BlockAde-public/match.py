"""Question-to-transcript matching interfaces and conservative local suggestions.

Local lexical overlap is only a candidate generator. It never confirms a match or
claims to understand medical semantics. A cloud provider can implement
``MatchProvider.suggest`` and be injected by the job runner later.
"""

from __future__ import annotations

import re
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import db
from text_conversion import to_traditional_chinese


STOPWORDS = {
    "about", "after", "also", "among", "before", "between", "could", "does", "from", "have",
    "into", "more", "most", "other", "should", "than", "that", "their", "there", "these", "they",
    "this", "those", "through", "under", "using", "which", "while", "with", "without", "what",
    "when", "where", "who", "will", "would", "對於", "以下", "何者", "下列", "關於", "何種", "之中",
}

# Words shared by many multiple-choice questions and ordinary lecture speech
# cannot establish that a question belongs to this lecture.
_CANDIDATE_STOPWORDS = STOPWORDS | {
    "all", "also", "an", "and", "any", "are", "as", "at", "be", "been", "being", "by",
    "can", "correct", "during", "each", "either", "except", "false", "following",
    "for", "has", "had", "how", "in", "is", "it", "its", "may", "not", "of", "on",
    "one", "only", "or", "our", "patient", "patients", "than", "the", "them", "then",
    "to", "true", "two", "was", "were", "why", "you", "your", "about", "according",
    "associated", "best", "choose", "common", "description", "descriptions",
    "finding", "findings", "include", "included", "incorrect", "likely",
    "major", "most", "part", "possible", "related", "result", "results",
    "statement", "statements", "usually", "whereas",
}

_CJK_PATTERN = r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
_LOCAL_QUESTION_BOILERPLATE = (
    "下列", "何者", "請問", "以下", "何種", "哪一項", "哪一個", "哪個", "哪一種",
    "有關", "關於", "敘述", "描述", "正確的是", "正確的", "是不正確", "不正確",
    "錯誤的是", "錯誤", "除外", "除了", "為何者", "為何", "下列何者", "下列有關",
)
_LOCAL_ENGLISH_STOPWORDS = STOPWORDS | {
    "the", "and", "for", "are", "was", "were", "not", "which", "does", "what",
    "patient", "following", "except", "true", "false", "correct", "incorrect",
}
_LOCAL_CJK_FUNCTION_CHARS = set("的之與和及或為是在也於者其而但了到把從向以會能就有被讓跟")


@dataclass
class MatchSuggestion:
    segment_id: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    question_time_ms: int | None = None
    status: str = "pending_confirmation"
    confidence: float | None = None
    confidence_label: str | None = None
    verdict: str | None = None
    candidate_score: float | None = None
    reason: str = ""
    evidence: str = ""
    evidence_segment_ids: list[str] | None = None
    last_evidence_segment_id: str | None = None
    provider: str = "local_lexical"


class MatchProvider(Protocol):
    """Provider contract for local or cloud matching implementations."""

    name: str
    auto_confirm_high_confidence: bool

    def suggest(self, question: dict[str, Any], segments: list[dict[str, Any]]) -> MatchSuggestion:
        """Return one evidence-backed suggestion; providers must not auto-confirm."""


@lru_cache(maxsize=8192)
def _tokens(text: str, *, question_stem: bool = False) -> frozenset[str]:
    normalized = _normalized_local_text(text, question_stem=question_stem)
    words = {
        word for word in re.findall(r"[a-z][a-z0-9_-]{1,}", normalized)
        if word not in _CANDIDATE_STOPWORDS
    }
    # Keep CJK runs separate so punctuation does not create artificial phrases.
    for run in re.findall(_CJK_PATTERN + "+", normalized):
        for width in (2, 3):
            words.update(run[index:index + width] for index in range(max(0, len(run) - width + 1)))
    return frozenset(words)


def _question_text(question: dict[str, Any]) -> str:
    parts = [str(question.get("stem", ""))]
    options = question.get("options", [])
    if isinstance(options, list):
        for option in options:
            if isinstance(option, dict):
                parts.append(str(option.get("text", "")))
            else:
                parts.append(str(option))
    return " ".join(parts)


def _segment_times(segment: dict[str, Any]) -> tuple[int, int]:
    if "start_ms" in segment:
        return int(segment["start_ms"]), int(segment["end_ms"])
    return round(float(segment.get("start", 0)) * 1000), round(float(segment.get("end", 0)) * 1000)


def top_candidate_windows(question: dict[str, Any], segments: list[dict[str, Any]], limit: int = 3,
                          window_size: int = 3, minimum_score: float = 0.04) -> list[dict[str, Any]]:
    """Return ranked short transcript windows for a semantic provider or UI."""
    stem_tokens = _tokens(str(question.get("stem", "")), question_stem=True)
    option_tokens = _tokens(_question_text({"options": question.get("options", [])}))
    question_tokens = stem_tokens | option_tokens
    if not question_tokens or not segments:
        return []
    candidates: list[dict[str, Any]] = []
    for left in range(len(segments)):
        for length in range(1, min(window_size, len(segments) - left) + 1):
            window = segments[left:left + length]
            evidence = " ".join(str(item.get("text", "")) for item in window).strip()
            tokens = _tokens(evidence)
            overlap = question_tokens & tokens
            if not overlap:
                continue
            # A single shared word is not lecture evidence. Two content terms
            # may come from the choices when a bilingual stem is absent from
            # the transcript or the stem is a generic multiple-choice prompt.
            if len(overlap) < 2:
                continue
            # A ranking score only; it is not model confidence or semantic relevance.
            score = (2 * len(overlap)) / max(1, len(question_tokens) + len(tokens))
            if score < minimum_score:
                continue
            start_ms, _ = _segment_times(window[0])
            _, end_ms = _segment_times(window[-1])
            candidates.append({
                "segment_ids": [str(item["id"]) for item in window],
                "segments": [
                    {"id": str(item["id"]), "start_ms": _segment_times(item)[0],
                     "end_ms": _segment_times(item)[1], "text": str(item.get("text", ""))}
                    for item in window
                ],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "evidence": evidence[:1800],
                "candidate_score": min(1.0, score),
            })
    candidates.sort(key=lambda item: (item["candidate_score"], item["end_ms"]), reverse=True)
    # Avoid returning highly overlapping alternatives from the same transcript area.
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        if any(candidate["start_ms"] < old["end_ms"] and candidate["end_ms"] > old["start_ms"] for old in selected):
            continue
        selected.append(candidate)
        if len(selected) >= max(0, limit):
            break
    return selected


def question_is_matchable(question: dict[str, Any]) -> bool:
    """Whether the question has enough content to locate its lecture passage.

    Answer quality is deliberately separate: an unreviewed answer must not
    prevent finding a suggested time in the transcript.
    """
    stem = str(question.get("stem") or "").strip()
    if not stem or stem in {"（此題文字需人工校對）", "（題目內容尚未辨識）"}:
        return False
    options = question.get("options")
    return isinstance(options, list) and sum(
        bool(str(option.get("text") or "").strip())
        for option in options if isinstance(option, dict)
    ) >= 2


def question_is_quiz_ready(question: dict[str, Any]) -> bool:
    """Only reviewed questions with a valid answer may be auto-confirmed."""
    answer_key = str(question.get("answer_key") or "").strip().upper()
    options = question.get("options", [])
    option_keys = {
        str(option.get("key", "")).strip().upper()
        for option in options if isinstance(option, dict)
    } if isinstance(options, list) else set()
    return bool(answer_key and answer_key in option_keys) and not bool(question.get("needs_review"))


def _normalized_local_text(text: str, *, question_stem: bool = False) -> str:
    normalized = to_traditional_chinese(str(text)).casefold()
    # A common spoken form for a medical intern. This keeps a direct phrase match
    # when Whisper transcribes the lecture wording and the question uses the title.
    normalized = normalized.replace("實習醫學生", "實習醫師")
    if question_stem:
        normalized = re.sub(r"[（(]\s*[A-ZＡ-Ｚ]?\d{1,3}\s*[）)]", " ", normalized, flags=re.IGNORECASE)
        for phrase in sorted(_LOCAL_QUESTION_BOILERPLATE, key=len, reverse=True):
            normalized = normalized.replace(phrase.casefold(), " ")
    return normalized


def _local_runs(text: str, *, question_stem: bool = False) -> list[str]:
    normalized = _normalized_local_text(text, question_stem=question_stem)
    return re.findall(r"[a-z][a-z0-9_-]*|\d+(?:\.\d+)?|" + _CJK_PATTERN + "+", normalized)


def _local_tokens(text: str, *, question_stem: bool = False) -> set[str]:
    runs = _local_runs(text, question_stem=question_stem)

    tokens: set[str] = set()
    for run in runs:
        if re.fullmatch(r"[a-z][a-z0-9_-]*", run):
            if len(run) >= 2 and run not in _LOCAL_ENGLISH_STOPWORDS:
                tokens.add(run)
            continue
        if not re.fullmatch(_CJK_PATTERN + "+", run):
            continue
        for width in (2, 3, 4, 5, 6):
            tokens.update(run[index:index + width] for index in range(max(0, len(run) - width + 1)))
    return tokens


def _phrase_support(question_stem: str, evidence: str, shared: set[str],
                    asks_for_self_introduction: bool) -> tuple[int, bool]:
    """Count separate shared ideas and check whether one main statement is covered."""
    question_runs = _local_runs(question_stem, question_stem=True)
    evidence_runs = _local_runs(evidence)
    matches: list[tuple[int, int, str, int, int]] = []
    for token in shared:
        if re.fullmatch(_CJK_PATTERN + "{4,6}", token):
            if any(character in token for character in _LOCAL_CJK_FUNCTION_CHARS):
                continue
            q_occurrences = [
                (run_index, position)
                for run_index, run in enumerate(question_runs)
                for position in range(len(run)) if run.startswith(token, position)
            ]
            e_occurrences = [
                (run_index, position)
                for run_index, run in enumerate(evidence_runs)
                for position in range(len(run)) if run.startswith(token, position)
            ]
            for (q_run, q_pos) in q_occurrences:
                for (e_run, e_pos) in e_occurrences:
                    matches.append((q_run, q_pos, token, e_run, e_pos))
        elif token.isascii() and token.isalpha() and len(token) >= 4:
            if token in question_runs and token in evidence_runs:
                matches.append((question_runs.index(token), 0, token, evidence_runs.index(token), 0))

    matches.sort(key=lambda item: (-len(item[2]), item[0], item[1], item[3], item[4]))
    selected: list[tuple[int, int, str, int, int]] = []
    used_question: set[tuple[int, int]] = set()
    used_evidence: set[tuple[int, int]] = set()
    covered_question: set[tuple[int, int]] = set()
    for match_item in matches:
        q_run, q_pos, token, e_run, e_pos = match_item
        q_range = {(q_run, index) for index in range(q_pos, q_pos + len(token))}
        e_range = {(e_run, index) for index in range(e_pos, e_pos + len(token))}
        if used_question.intersection(q_range) or used_evidence.intersection(e_range):
            continue
        selected.append(match_item)
        used_question.update(q_range)
        used_evidence.update(e_range)
        if token.isascii():
            # English words count as four content units below; use the same
            # units here so even a fully supported English stem can pass.
            covered_question.update((q_run, index) for index in range(4))
        else:
            covered_question.update(
                (q_run, index) for index in range(q_pos, q_pos + len(token))
                if question_runs[q_run][index] not in _LOCAL_CJK_FUNCTION_CHARS
            )

    def content_size(runs: list[str]) -> int:
        return sum(
            4 if run.isascii() else sum(character not in _LOCAL_CJK_FUNCTION_CHARS for character in run)
            for run in runs
        )

    content_total = max(1, content_size(question_runs))
    coverage = len(covered_question) / content_total
    longest_content = max((
        sum(character not in _LOCAL_CJK_FUNCTION_CHARS for character in token)
        if not token.isascii() else 4
        for _, _, token, _, _ in selected
    ), default=0)
    supports_main_statement = coverage >= 0.58 and longest_content >= 4

    # An explicit first-person role introduction is one complete statement in
    # speech, even when the surrounding question also gives its clinical reason.
    normalized_question = _normalized_local_text(question_stem, question_stem=True)
    normalized_evidence = _normalized_local_text(evidence)
    if asks_for_self_introduction and "實習醫師" in normalized_question and re.search(
        r"(?:我是|我叫).{0,8}實習醫師", normalized_evidence
    ):
        supports_main_statement = True
    return len(selected), supports_main_statement


class _LocalTextIndex:
    """Precomputed course-local n-gram index for conservative text evidence."""

    def __init__(self, segments: list[dict[str, Any]], window_size: int = 4) -> None:
        self.segments = segments
        self.segment_tokens = [_local_tokens(str(segment.get("text", ""))) for segment in segments]
        self.document_frequency: dict[str, int] = {}
        for tokens in self.segment_tokens:
            for token in tokens:
                self.document_frequency[token] = self.document_frequency.get(token, 0) + 1
        self.document_count = max(1, len(segments))
        self.windows: list[dict[str, Any]] = []
        for left in range(len(segments)):
            for length in range(1, min(window_size, len(segments) - left) + 1):
                window = segments[left:left + length]
                start_ms, _ = _segment_times(window[0])
                _, end_ms = _segment_times(window[-1])
                ids = [str(segment.get("id", "")) for segment in window]
                self.windows.append({
                    "indices": tuple(range(left, left + length)),
                    "segment_ids": ids,
                    "tokens": set().union(*self.segment_tokens[left:left + length]),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "evidence": " ".join(str(segment.get("text", "")) for segment in window).strip(),
                })

    def idf(self, token: str) -> float:
        frequency = self.document_frequency.get(token, 0)
        return math.log((self.document_count + 1) / (frequency + 1)) + 1.0

    def find(self, question: dict[str, Any]) -> dict[str, Any] | None:
        # The stem carries the topic. Options and answer explanations can mention
        # unrelated distractors and must not drag a question to the wrong lecture.
        query_tokens = _local_tokens(str(question.get("stem", "")), question_stem=True)
        original_stem = to_traditional_chinese(str(question.get("stem", "")))
        asks_for_self_introduction = any(
            phrase in original_stem for phrase in ("自稱", "介紹自己", "告知身分", "說明自己的身分")
        )
        if len(query_tokens) < 4 or not self.windows:
            return None
        weights = {token: self.idf(token) for token in query_tokens}
        total_weight = sum(weights.values())
        rare_frequency_limit = max(2, math.ceil(self.document_count * 0.05))

        def rare(token: str) -> bool:
            return self.document_frequency.get(token, 0) <= rare_frequency_limit

        ranked: list[dict[str, Any]] = []
        for window in self.windows:
            shared = query_tokens & window["tokens"]
            if len(shared) < 4:
                continue
            matched_weight = sum(weights[token] for token in shared)
            recall = matched_weight / total_weight
            anchor_count = sum(1 for token in shared if rare(token))
            has_distinctive_phrase = any(
                (
                    re.fullmatch(_CJK_PATTERN + "{5,6}", token)
                    and not any(character in token for character in "的之與和及或為是在也於者其而但了到把從向")
                    and rare(token)
                )
                or (token == "實習醫師" and asks_for_self_introduction and rare(token))
                or (token.isascii() and token.isalpha() and len(token) >= 5 and rare(token))
                for token in shared
            )
            candidate_weight = sum(self.idf(token) for token in window["tokens"])
            precision = matched_weight / max(matched_weight, candidate_weight)
            score = 0.8 * recall + 0.2 * precision
            if has_distinctive_phrase:
                # Rare phrase overlap gives the candidate a useful rank. It is
                # not enough by itself to confirm a complex question.
                score = max(score, min(0.62, 0.34 + 0.02 * (anchor_count - 1)))
            if not has_distinctive_phrase and (recall < 0.22 or precision < 0.10 or anchor_count < 2):
                continue
            if has_distinctive_phrase and anchor_count < 1:
                continue
            independent_phrases, supports_main_statement = _phrase_support(
                str(question.get("stem", "")), window["evidence"], shared, asks_for_self_introduction,
            )
            if independent_phrases < 2 and not supports_main_statement:
                continue
            ranked.append({
                **window,
                "score": score,
                "recall": recall,
                "precision": precision,
                "anchor_count": anchor_count,
                "matched_count": len(shared),
                "independent_phrases": independent_phrases,
                "supports_main_statement": supports_main_statement,
            })
        ranked.sort(key=lambda candidate: (
            candidate["score"], candidate["recall"], candidate["matched_count"],
            -len(candidate["indices"]), -candidate["start_ms"],
        ), reverse=True)
        if not ranked:
            return None
        best = ranked[0]
        # Nearby overlapping windows represent the same spoken passage. A strong
        # second passage with a similar score makes the location ambiguous.
        best_indices = set(best["indices"])
        for alternative in ranked[1:]:
            if best_indices.intersection(alternative["indices"]):
                continue
            if alternative["score"] >= 0.30 and best["score"] - alternative["score"] < 0.08:
                return None
            break
        if best["score"] < 0.32:
            return None
        return best


class LocalCandidateProvider:
    """Strong local text matches only; weak or ambiguous results are omitted."""

    name = "local_text"
    auto_confirm_high_confidence = True

    def __init__(self, segments: list[dict[str, Any]] | None = None) -> None:
        self.index = _LocalTextIndex(segments or []) if segments is not None else None

    def suggest(self, question: dict[str, Any], segments: list[dict[str, Any]]) -> MatchSuggestion:
        def unmatched(reason: str) -> MatchSuggestion:
            return MatchSuggestion(status="unmatched", verdict="no_match", reason=reason, provider=self.name)

        if not question_is_matchable(question):
            return unmatched("題幹或選項沒有足夠文字可供比對。")
        if self.index is None or self.index.segments is not segments:
            self.index = _LocalTextIndex(segments)
        candidate = self.index.find(question)
        if candidate is None:
            return unmatched("逐字稿中沒有找到足夠明確且唯一的題幹文字證據。")
        evidence_ids = candidate["segment_ids"]
        if not evidence_ids or not all(evidence_ids):
            return unmatched("逐字稿段落缺少可驗證的識別碼。")
        score = round(float(candidate["score"]), 4)
        return MatchSuggestion(
            segment_id=evidence_ids[0], start_ms=candidate["start_ms"], end_ms=candidate["end_ms"],
            question_time_ms=candidate["end_ms"] + 500, status="confirmed", confidence=None,
            confidence_label="high", verdict="match", candidate_score=score,
            reason=(f"本機題幹文字比對找到唯一強證據（題幹涵蓋 {candidate['recall']:.0%}、"
                    f"區辨字詞 {candidate['anchor_count']} 個）。"),
            evidence=candidate["evidence"][:8000], evidence_segment_ids=evidence_ids,
            last_evidence_segment_id=evidence_ids[-1], provider=self.name,
        )


def validate_provider_suggestion(question: dict[str, Any], segments: list[dict[str, Any]],
                                 suggestion: MatchSuggestion, provider: MatchProvider,
                                 audio_duration_ms: int | None = None) -> MatchSuggestion:
    suggestion.provider = getattr(provider, "name", suggestion.provider)
    if isinstance(provider, LocalCandidateProvider):
        def leave_unmatched(reason: str | None = None) -> MatchSuggestion:
            suggestion.status = "unmatched"
            suggestion.segment_id = None
            suggestion.start_ms = None
            suggestion.end_ms = None
            suggestion.question_time_ms = None
            suggestion.confidence = None
            suggestion.candidate_score = None
            suggestion.evidence = ""
            suggestion.evidence_segment_ids = []
            suggestion.last_evidence_segment_id = None
            suggestion.verdict = "no_match"
            if reason:
                suggestion.reason = reason
            return suggestion

        if suggestion.status != "confirmed":
            return leave_unmatched()
        segment_by_id = {str(segment.get("id")): segment for segment in segments}
        ids = list(suggestion.evidence_segment_ids or [])
        last_id = suggestion.last_evidence_segment_id or (ids[-1] if ids else None)
        ordered_ids = [str(segment.get("id")) for segment in segments]
        evidence_valid = bool(ids) and len(ids) <= 4 and all(str(item) in segment_by_id for item in ids)
        evidence_valid = evidence_valid and last_id is not None and str(last_id) == str(ids[-1])
        if evidence_valid:
            try:
                first_index = ordered_ids.index(str(ids[0]))
                evidence_valid = ordered_ids[first_index:first_index + len(ids)] == [str(item) for item in ids]
            except ValueError:
                evidence_valid = False
        if not evidence_valid:
            return leave_unmatched("本機比對的逐字稿證據未通過安全檢查。")
        evidence_segments = [segment_by_id[str(segment_id)] for segment_id in ids]
        last_segment = segment_by_id[str(last_id)]
        suggestion.segment_id = str(ids[0])
        suggestion.start_ms = min(_segment_times(segment)[0] for segment in evidence_segments)
        suggestion.end_ms = max(_segment_times(segment)[1] for segment in evidence_segments)
        suggestion.question_time_ms = _segment_times(last_segment)[1] + 500
        suggestion.evidence = " ".join(str(segment.get("text", "")) for segment in evidence_segments)[:8000]
        if audio_duration_ms is not None and suggestion.question_time_ms > audio_duration_ms:
            return leave_unmatched("本機比對位置超出錄音長度，已略過自動對位。")
        if question_is_quiz_ready(question):
            suggestion.status = "confirmed"
        else:
            suggestion.status = "pending_confirmation"
            suggestion.reason = (suggestion.reason + " 題目答案或匯入內容尚待校對；出題時間需人工確認。").strip()
        return suggestion

    if not getattr(provider, "auto_confirm_high_confidence", False):
        if suggestion.status == "confirmed":
            suggestion.status = "pending_confirmation"
        return suggestion

    if suggestion.verdict == "no_match" or suggestion.status == "unmatched":
        suggestion.status = "unmatched"
        return suggestion
    segment_by_id = {str(segment.get("id")): segment for segment in segments}
    ids = list(suggestion.evidence_segment_ids or [])
    last_id = suggestion.last_evidence_segment_id or (ids[-1] if ids else None)
    ordered_ids = [str(segment.get("id")) for segment in segments]
    answer_key = str(question.get("answer_key") or "").strip().upper()
    option_keys = {
        str(option.get("key", "")).strip().upper()
        for option in question.get("options", []) if isinstance(option, dict)
    }
    evidence_valid = bool(ids) and len(ids) <= 3 and all(str(segment_id) in segment_by_id for segment_id in ids)
    evidence_valid = evidence_valid and last_id is not None and str(last_id) in segment_by_id
    evidence_valid = evidence_valid and str(last_id) == str(ids[-1])
    if evidence_valid:
        try:
            first_index = ordered_ids.index(str(ids[0]))
            evidence_valid = ordered_ids[first_index:first_index + len(ids)] == [str(item) for item in ids]
        except ValueError:
            evidence_valid = False
    answer_valid = bool(answer_key) and answer_key in option_keys
    question_valid = not bool(question.get("needs_review"))
    last_is_latest = False
    if evidence_valid and last_id is not None:
        last_end = _segment_times(segment_by_id[str(last_id)])[1]
        latest_end = max(_segment_times(segment_by_id[str(segment_id)])[1] for segment_id in ids)
        last_is_latest = last_end == latest_end
    if evidence_valid and suggestion.verdict in {"match", "uncertain"}:
        evidence_segments = [segment_by_id[str(segment_id)] for segment_id in ids]
        last_segment = segment_by_id[str(last_id)]
        suggestion.segment_id = str(ids[0])
        suggestion.start_ms = min(_segment_times(segment)[0] for segment in evidence_segments)
        suggestion.end_ms = max(_segment_times(segment)[1] for segment in evidence_segments)
        suggestion.question_time_ms = _segment_times(last_segment)[1] + 500
        suggestion.evidence = " ".join(str(segment.get("text", "")) for segment in evidence_segments)[:8000]

    if suggestion.verdict != "match" or suggestion.confidence_label != "high" or not evidence_valid or not answer_valid or not question_valid or not last_is_latest:
        suggestion.status = "pending_confirmation"
        if not evidence_valid:
            suggestion.reason = (suggestion.reason + " 證據段落 ID 無法在本機逐字稿驗證，需人工確認。").strip()
        elif not answer_valid:
            suggestion.reason = (suggestion.reason + " 題目尚無有效正解或選項，需人工確認。").strip()
        elif not question_valid:
            suggestion.reason = (suggestion.reason + " 題目匯入仍標示需校對，需人工確認。").strip()
        elif not last_is_latest:
            suggestion.reason = (suggestion.reason + " 最後證據段落時間不一致，需人工確認。").strip()
        elif suggestion.verdict != "match":
            suggestion.reason = (suggestion.reason + " 模型判斷為 uncertain，需人工確認。").strip()
        return suggestion

    if audio_duration_ms is not None and suggestion.question_time_ms is not None and suggestion.question_time_ms > audio_duration_ms:
        suggestion.status = "pending_confirmation"
        suggestion.reason = (suggestion.reason + " 出題時間超過錄音長度，需人工確認。").strip()
        return suggestion
    suggestion.status = "confirmed"
    suggestion.confidence = suggestion.confidence or 0.9
    suggestion.reason = (suggestion.reason or "高信心題目對應；觸發時間由最後證據段落結束時間加 500 毫秒計算。").strip()
    return suggestion


def suggest_question(question: dict[str, Any], segments: list[dict[str, Any]],
                     provider: MatchProvider | None = None,
                     audio_duration_ms: int | None = None) -> MatchSuggestion:
    chosen = provider or LocalCandidateProvider(segments)
    suggestion = chosen.suggest(question, segments)
    return validate_provider_suggestion(question, segments, suggestion, chosen, audio_duration_ms)


def refresh_course_matches(course_id: str, provider: MatchProvider | None = None,
                           *, preserve_analysis: bool = True) -> None:
    segments = db.list_segments(course_id)
    course = db.get_course(course_id) or {}
    audio_duration_ms = round(float(course.get("duration_seconds") or 0) * 1000) or None
    chosen_provider = provider or LocalCandidateProvider(segments)
    for question in db.list_questions(course_id):
        existing = db.get_match(question["id"])
        if (preserve_analysis and isinstance(chosen_provider, LocalCandidateProvider)
                and existing and existing.get("provider") in {"openai", "codex"}):
            continue
        if existing and (existing["status"] == "disabled" or (
            existing["status"] == "confirmed" and existing.get("provider") in {"manual", "openai", "codex"}
        )):
            continue
        suggestion = suggest_question(question, segments, chosen_provider, audio_duration_ms)
        db.save_match(question["id"], {
            "segment_id": suggestion.segment_id, "start_ms": suggestion.start_ms,
            "end_ms": suggestion.end_ms, "question_time_ms": suggestion.question_time_ms,
            "status": suggestion.status, "confidence": suggestion.confidence,
            "candidate_score": suggestion.candidate_score, "reason": suggestion.reason,
            "evidence": suggestion.evidence, "provider": suggestion.provider,
        })
