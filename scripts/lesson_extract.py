"""LESSONS1: candidate lessons + ORCH rule classification from orchestration history.

Plan v2.4 step 2 (4.1-4.2, 6.1, 12.3). Chunks the JARVIS orchestration files,
drops every chunk that hits the Dora/company deny list BEFORE any API call
(dropped text is never sent and never written), sends the rest through
conveyor_api.call inside one 'ders' envelope, keeps only candidates whose
quote is verbatim in their chunk. Output is CANDIDATES only (4.1): nothing
here becomes a rule.

    python scripts/lesson_extract.py --ceiling 2.0 [--root build/orch] [--out build/lessons]
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis import dora_screen  # noqa: E402
from jarvis.dora_screen import _screen, denied, screen  # noqa: E402,F401  (public names kept)

HERE = Path(__file__).resolve().parent
DENY_PATH = HERE / "lesson_deny_patterns.json"
MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 2000
CHUNK_LINES = 40
MIN_QUOTE = 8
RULE_COUNT = 81
CLASSES = ("enforced_in_code", "every_round", "topic", "historical")
FIELDS = ("olay", "kok_neden", "kural", "zorlanma_yeri", "n6_davranisi", "tur", "alinti")
RULE_RE = re.compile(r"^(?:#{1,6}\s+|-\s+)?O(\d+)[a-z]?(?![0-9A-Za-z])")
HEAD_RE = re.compile(r"^#{1,6}\s")

SYSTEM = (
    "JARVIS orkestrasyon gecmisinden DERS ADAYI cikariyorsun. Yalniz verilen parcayi kullan, "
    "uydurma. Yalniz kati JSON don, baska metin yok:\n"
    '{"lessons": [{"olay": str, "kok_neden": str, "kural": str, "zorlanma_yeri": str, '
    '"n6_davranisi": str, "tur": str, "alinti": str}], "classification": str|null}\n'
    "alinti: parcadan BIREBIR kopyalanmis kisa bir alinti (en az 8 karakter). "
    "zorlanma_yeri: kod / test / istem / insan. tur: hata / yontem / sinir / karar. "
    "classification yalniz parca bir ORCH kurali (Onn) ise: enforced_in_code (kodda zorlaniyor), "
    "every_round (her turda uygulanir), topic (konuya ozel), historical (artik gecerli degil); "
    "degilse null. Ders yoksa lessons bos liste."
)


def load_deny(path: Path = DENY_PATH):
    """DORAGATE1: the shared jarvis.dora_screen list loader, same file and errors."""
    return dora_screen.load_deny(path)


def hit_lines(path: Path, deny):
    """Whole-file screen, shapes and words: (first - 1, last + 1) line of every hit, so a
    phone or 'yapi\\nkredi' split over a chunk boundary drops every chunk it touches.
    None = screen failed -> drop the whole file."""
    try:
        text = "\n".join(_read_lines(path))
        nl = [i for i, ch in enumerate(text) if ch == "\n"]
        return [(max(1, bisect.bisect_left(nl, s)), bisect.bisect_left(nl, e) + 2)
                for s, e in _screen(text, deny)]
    except Exception:  # fail closed
        return None


def _hit(c: dict, lines) -> bool:
    return lines is None or any(s <= he and hs <= e for s, e in c["spans"] for hs, he in lines)


def _mk(rel: str, rule, spans, lines) -> dict:
    text = "\n".join("\n".join(lines[s - 1:e]) for s, e in spans)
    return {"source": rel, "rule": rule, "spans": spans,
            "start": spans[0][0], "end": spans[-1][1], "text": text}


def pointer(c: dict) -> str:
    return c["source"] + ":" + ",".join(f"{s}-{e}" for s, e in c["spans"])


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def chunk_rules(path: Path, rel: str) -> list[dict]:
    """ORCH_RULES: one chunk per rule number (scattered updates merged as spans),
    then the non-rule sections. Rule chunks sorted numerically."""
    lines = _read_lines(path)
    segs, cur = [], [None, 1]
    for i, line in enumerate(lines, 1):
        m = RULE_RE.match(line)
        if m or HEAD_RE.match(line):
            if i > cur[1]:
                segs.append((cur[0], cur[1], i - 1))
            cur = [int(m.group(1)) if m else None, i]
    if lines:
        segs.append((cur[0], cur[1], len(lines)))
    by_rule: dict[int, list] = {}
    other = []
    for rule, s, e in segs:
        if rule is None:
            body = lines[s - 1:e]
            if any(ln.strip() and not HEAD_RE.match(ln) for ln in body):
                other.append(_mk(rel, None, [[s, e]], lines))
            continue
        spans = by_rule.setdefault(rule, [])
        if spans and spans[-1][1] + 1 == s:
            spans[-1][1] = e
        else:
            spans.append([s, e])
    return [_mk(rel, n, by_rule[n], lines) for n in sorted(by_rule)] + other


def chunk_plain(path: Path, rel: str) -> list[dict]:
    """Other files: split at headings, at most CHUNK_LINES lines per chunk."""
    lines = _read_lines(path)
    out, start = [], 1
    for i, line in enumerate(lines, 1):
        if i > start and (HEAD_RE.match(line) or i - start >= CHUNK_LINES):
            out.append(_mk(rel, None, [[start, i - 1]], lines))
            start = i
    if lines:
        out.append(_mk(rel, None, [[start, len(lines)]], lines))
    return [c for c in out if c["text"].strip()]


def sources(root: Path) -> tuple[list[Path], list[str]]:
    """The fixed JARVIS-scope source set (origin classification). Nothing else in
    kayit/ is read. Returns (existing paths in order, missing relative names)."""
    k = root / "kayit"
    fixed = [root / "ORCH_RULES.md", k / "KARARLAR.md", k / "ACIK_KONULAR.md",
             k / "OZET_2026-11-11_13.md"]
    globbed = sorted((k / "sohbet").glob("*.md")) if (k / "sohbet").is_dir() else []
    reviews = sorted(set(k.glob("*INCELEME*.md")) | set(k.glob("*DANISMA*.md"))) if k.is_dir() else []
    present = [p for p in fixed if p.is_file()] + globbed + reviews
    missing = [p.relative_to(root).as_posix() for p in fixed if not p.is_file()]
    return present, missing


def chunks(root: Path) -> tuple[list[dict], list[str]]:
    present, missing = sources(root)
    out = []
    for p in present:
        rel = p.relative_to(root).as_posix()
        out += chunk_rules(p, rel) if rel == "ORCH_RULES.md" else chunk_plain(p, rel)
    return out, missing


def parse_answer(ans: str) -> dict:
    s = ans.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    d = json.loads(s)
    if not isinstance(d, dict):
        raise ValueError("answer is not an object")
    return d


def _valid(lesson) -> bool:
    return isinstance(lesson, dict) and all(
        isinstance(lesson.get(f), str) and lesson[f].strip() for f in FIELDS)


def _spent(api, eid) -> str:
    try:
        rows = api.read_ledger(Path(api.ROOT) / api.LEDGER_NAME)
        usd = sum(float(r.get("usd", 0)) - float(r.get("release", 0) or 0)
                  for r in rows if r.get("envelope") == eid and "ceiling" not in r)
        return f"{usd:.4f}"
    except Exception:  # spend unknown is reported, never guessed
        return "unknown"


def run(root: Path, out: Path, ceiling: float, api, deny_path: Path = DENY_PATH) -> dict:
    """One batch. api = conveyor_api (APIBUDGET1) or a test fake. Always writes all
    four outputs; a refused call (envelope ceiling) stops the run as partial."""
    if not (isinstance(ceiling, (int, float)) and math.isfinite(ceiling) and ceiling > 0):
        raise ValueError(f"ceiling must be a finite positive USD amount, got {ceiling!r}")
    deny = load_deny(deny_path)  # unreadable deny list -> raise before any call
    all_chunks, missing = chunks(root)
    rep = {"chunks": len(all_chunks), "dropped_dora": 0, "sent": 0, "api_errors": 0,
           "parse_errors": 0, "candidates": 0, "quote_dropped": 0, "invalid_dropped": 0,
           "partial": False, "stop": "complete", "envelope": None,
           "deny_patterns_version": deny[0], "missing_sources": missing}
    cands, cls = [], {}
    eid = None
    if getattr(api, "open_envelope", None) is None:
        rep.update(partial=True, stop="refused: broker has no open_envelope (APIBUDGET1 not integrated)")
    else:
        try:
            eid = api.open_envelope(ceiling, "ders lesson batch")
            rep["envelope"] = eid
        except api.ApiRefused as exc:
            rep.update(partial=True, stop=f"refused: {exc}")
    hits = {src: hit_lines(root / src, deny) for src in {c["source"] for c in all_chunks}}
    seen = {}  # source -> [dropped, screened]
    for c in all_chunks if eid is not None else []:
        ptr = pointer(c)
        purpose = f"ders lesson {Path(c['source']).name}:{c['start']}"[:80]
        st = seen.setdefault(c["source"], [0, 0])
        st[1] += 1
        if (_hit(c, hits[c["source"]]) or any(not screen(t, deny)[0] for t in  # + secrets
                                              (c["text"], c["source"], ptr, purpose))):
            rep["dropped_dora"] += 1
            st[0] += 1
            if c["rule"] is not None:
                cls[c["rule"]] = "dropped_dora"
            continue
        msg = [{"role": "user", "content": f"Kaynak: {ptr}\n<<<\n{c['text']}\n>>>"}]
        try:
            ans = api.call(MODEL, SYSTEM, msg, MAX_TOKENS, purpose, envelope=eid)
        except api.ApiRefused as exc:
            rep.update(partial=True, stop=f"refused at {ptr}: {exc}")
            break
        except api.ApiError:
            rep["sent"] += 1
            rep["api_errors"] += 1
            continue
        rep["sent"] += 1
        try:
            d = parse_answer(ans)
        except (ValueError, TypeError):
            rep["parse_errors"] += 1
            continue
        if c["rule"] is not None:
            cls[c["rule"]] = d.get("classification") if d.get("classification") in CLASSES else "unclassified"
        lessons = d.get("lessons")
        for lesson in lessons if isinstance(lessons, list) else []:
            if not _valid(lesson):
                rep["invalid_dropped"] += 1
            elif len(lesson["alinti"].strip()) < MIN_QUOTE or lesson["alinti"] not in c["text"]:
                rep["quote_dropped"] += 1
            else:
                rec = {f: lesson[f] for f in FIELDS}
                rec.update(kaynak=ptr, dogrulama_durumu="aday",
                           rule=None if c["rule"] is None else f"O{c['rule']}")
                cands.append(rec)
    rep["drop_ratio"] = [f"{'<denied name>' if denied(s, deny) else s} {d}/{n}"
                         for s, (d, n) in sorted(seen.items())]
    dropped, screened = rep["dropped_dora"], sum(n for _, n in seen.values())
    if screened and dropped / screened > 0.4:
        rep["warning"] = f"dropped {dropped}/{screened} chunks (> 40%): deny screen may be too wide"
        print(f"WARNING: {rep['warning']}", file=sys.stderr)
    rep["candidates"] = len(cands)
    rep["usd_spent"] = _spent(api, eid) if eid is not None else "0.0000"
    rule_chunks = [c for c in all_chunks if c["rule"] is not None]
    found = {c["rule"] for c in rule_chunks}
    rep["rules_found"] = len(found)
    rep["rules_missing"] = [f"O{n}" for n in range(1, RULE_COUNT + 1) if n not in found]
    rows = [{"rule": f"O{c['rule']}", "class": cls.get(c["rule"], "unclassified"),
             "kaynak": pointer(c)} for c in rule_chunks]
    _write(out, cands, rows, rep)
    return rep


def _dump(rows) -> str:
    return "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows)


def _write(out: Path, cands, rows, rep) -> None:
    out.mkdir(parents=True, exist_ok=True)
    nic = [r for r in rows if r["class"] in ("every_round", "topic")]
    md = ["# Rule exists but not in code (CANDIDATE list, plan v2.4 4.1: not a rule)", "",
          "Classes every_round + topic; historical and dropped_dora excluded.", ""]
    md += [f"- {r['rule']} ({r['class']}) {r['kaynak']}" for r in nic] or ["- none"]
    rp = ["# LESSONS1 run report", ""] + [
        f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in sorted(rep.items())]
    files = {"candidates.jsonl": _dump(cands), "rules_classification.jsonl": _dump(rows),
             "not_in_code.md": "\n".join(md) + "\n", "run_report.md": "\n".join(rp) + "\n"}
    for name, body in files.items():
        with open(out / name, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LESSONS1 lesson extraction (candidates only)")
    ap.add_argument("--ceiling", type=float, required=True, help="batch envelope ceiling, USD")
    ap.add_argument("--root", type=Path, help="orchestration dir (default <main checkout>/build/orch)")
    ap.add_argument("--out", type=Path, default=HERE.parent / "build" / "lessons")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(HERE))
    import conveyor_api as api
    mc = getattr(api, "main_checkout", None)
    root = a.root or (Path(mc(HERE.parent)) if mc else HERE.parent) / "build" / "orch"
    rep = run(root, a.out, a.ceiling, api)
    print(f"lessons: {rep['candidates']} candidates, sent {rep['sent']}, "
          f"dropped_dora {rep['dropped_dora']}, stop {rep['stop']}; see {a.out / 'run_report.md'}")
    return 1 if rep["partial"] else 0


if __name__ == "__main__":
    sys.exit(main())
