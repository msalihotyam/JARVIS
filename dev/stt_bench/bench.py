"""Offline STT scoring. Supply an engine and local <phrase id>.wav files."""

import csv
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Protocol


class Engine(Protocol):
    def transcribe(self, audio_path: Path, hint: tuple[str, ...]) -> tuple[str, float]:
        """Return the transcript and elapsed seconds, with required terms as hints."""


@dataclass(frozen=True)
class Phrase:
    id: str
    reference: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class Result:
    phrase_id: str
    status: str
    word_error_rate: float | None = None
    term_hit_rate: float | None = None
    corrected_term_hit_rate: float | None = None
    seconds: float | None = None


def load_phrases() -> list[Phrase]:
    corpus = Path(__file__).with_name("phrases_tr.json")
    entries = json.loads(corpus.read_text(encoding="utf-8"))
    return [Phrase(entry["id"], entry["reference"], tuple(entry["terms"])) for entry in entries]


# Uppercase "I" is ı in Turkish but i in Latin-script terms (JARVIS, API), so it stays a wildcard.
_ANY_I = ""
_TURKISH_CASE = str.maketrans({"I": _ANY_I, "İ": "i"})


def _words(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFC", text).replace("i̇", "i")
    return [word.translate(_TURKISH_CASE).lower() for word in re.findall(r"[^\W_]+", normalized)]


def _same(expected: str, actual: str) -> bool:
    return len(expected) == len(actual) and all(
        left == right or (_ANY_I in (left, right) and {left, right} <= {_ANY_I, "i", "ı"})
        for left, right in zip(expected, actual)
    )


def word_error_rate(reference: str, transcript: str) -> float:
    """Return word edit distance divided by the number of reference words."""
    expected, actual = _words(reference), _words(transcript)
    previous = list(range(len(actual) + 1))
    for index, expected_word in enumerate(expected, start=1):
        current = [index]
        for position, actual_word in enumerate(actual, start=1):
            current.append(min(
                previous[position] + 1,
                current[-1] + 1,
                previous[position - 1] + (not _same(expected_word, actual_word)),
            ))
        previous = current
    return previous[-1] / max(len(expected), 1)


def term_hit_rate(transcript: str, terms: Sequence[str]) -> float | None:
    """Return the fraction of listed terms present as contiguous whole words."""
    if not terms:
        return None
    words = _words(transcript)
    hits = 0
    for term in terms:
        tokens = _words(term)
        if tokens and any(
            all(map(_same, tokens, words[start:start + len(tokens)]))
            for start in range(len(words) - len(tokens) + 1)
        ):
            hits += 1
    return hits / len(terms)


def run_benchmark(
    engine: Engine,
    phrases: Iterable[Phrase],
    audio_dir: Path,
    result_path: Path,
    *,
    corrector: Callable[[str], str] | None = None,
) -> list[Result]:
    """Score phrases in input order and write a UTF-8 CSV to result_path."""
    results = []
    for phrase in phrases:
        audio_path = audio_dir / f"{phrase.id}.wav"
        if not audio_path.is_file():
            results.append(Result(phrase.id, "missing"))
            continue
        try:
            transcript, seconds = engine.transcribe(audio_path, phrase.terms)
        except Exception:
            results.append(Result(phrase.id, "failed"))
            continue

        results.append(Result(
            phrase.id,
            "ok",
            word_error_rate(phrase.reference, transcript),
            term_hit_rate(transcript, phrase.terms),
            term_hit_rate(corrector(transcript), phrase.terms) if corrector is not None else None,
            seconds,
        ))

    with result_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        columns = [field.name for field in fields(Result)]
        writer.writerow(columns)
        for row in results:
            writer.writerow(getattr(row, column) for column in columns)
    return results
