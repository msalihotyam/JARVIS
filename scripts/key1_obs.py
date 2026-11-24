"""KEY1OBS1: read-only K2/K4 observer (Devir B). Compares the bot's key 1 decisions in build/bot/decisions.jsonl
with the real key 1 files in build/approvals/done/ since a given time and prints the K2 and K4 lines.

  python scripts/key1_obs.py [--root R] [--since 2026-11-24T00:00:00Z] [--now ISO]

Exit 0 for readable input, 2 for an unreadable or malformed one or an inconsistent approval (header sha differs
from the file name sha; fail closed). Writes nothing."""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchbot import PRE_RE, SALIH_RE  # noqa: E402  orchbot.key1_side's own classifiers, never a copy

WINDOW_S = 2 * 3600  # a bot write with no real key 1 within 2 h is unsafe
# conveyor.to_done stamps a name clash AFTER the suffix: <name>.refused.<YYYYmmdd-HHMMSS> (orchbot.STAMP_RE)
NAME_RE = re.compile(r"push-([0-9a-f]{40})-[A-Za-z0-9._-]+?(\.refused)?(?:\.\d{8}-\d{6})?")
# tolerant on by= (the live bot writes by=hands, which the push gate's header regex does not know yet)
HEAD_RE = re.compile(r"KEY1 v1 sha=(?P<sha>[0-9a-f]{40}) ci=\S+ lane=(?P<lane>\S+) by=(?P<by>[A-Za-z0-9_]+)"
                     r"(?: reason=(?P<reason>[^=]*))? .*at=(?P<at>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)")
SHA_RE = re.compile(r"push-([0-9a-f]{40})")
V1_RE = re.compile(r"\s*KEY\s*1\s*V\s*1(?!\d)", re.I)  # legacy free text ("key1 INT-X ...") is not a v1 attempt


def ts(s):
    """Epoch seconds; Z/offset values keep their zone, a naive value (decisions.jsonl, conveyor.iso) is local time."""
    return datetime.fromisoformat(str(s).strip().upper()).timestamp()


def load_decisions(path, since):
    """[(t, action, mode, lane, sha)] of the key 1 decisions since; ValueError on a malformed line."""
    out = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                if not isinstance(d, dict):
                    raise ValueError("not an object")
                act = d.get("did") if d.get("mode") == "live" else d.get("would_do")
                if act not in ("write_key1", "ask_salih_key1"):
                    continue
                t = ts(d["ts"])
            except (ValueError, KeyError, TypeError) as e:
                raise ValueError(f"decisions.jsonl line {i}: {e}"[:200]) from None
            if t >= since:
                m = SHA_RE.search(str(d.get("detail", "")))
                out.append((t, act, d.get("mode", "shadow"), str(d.get("target", "")), m and m.group(1)))
    return out


def side(by, reason):
    """orchbot.key1_side on this tolerant header: hands, conveyor, write_key1 (an orchestrator key 1 that may
    AGREE) or ask_salih_key1 (the orchestrator escalated: a bot write against it never agrees)."""
    if by.casefold() == "hands":
        return "hands"
    if by == "conveyor":  # exact case like the push gate; any other spelling is gate-invalid and fails closed
        return "conveyor"
    if by == "key1_check":
        return "write_key1" if reason is None else "ask_salih_key1"
    if by == "manual" and reason and PRE_RE.search(reason):
        return "conveyor"  # an O42 pre-approval: conveyor side, outside key-1 graduation
    # DECISION TAKEN: a Salih reason, a reason the push gate refuses (non-ASCII, missing) or an unknown writer
    # is ask_salih_key1, fail closed (alternative: drop the file as no key 1)
    if by == "manual" and reason and reason.isascii() and reason.isprintable() and not SALIH_RE.search(reason):
        return "write_key1"
    return "ask_salih_key1"


def load_approvals(done):
    """[{sha, lane, by, side, at, refused, name, bad}] of every approval in done/; ValueError on a malformed one."""
    out = []
    for p in sorted(Path(done).iterdir()):
        m = NAME_RE.fullmatch(p.name)
        if not m:
            continue
        with open(p, "rb") as f:
            first = f.read(1024).partition(b"\n")[0].decode("utf-8", "replace").rstrip("\r").lstrip("﻿")
        h = HEAD_RE.fullmatch(first)
        if not h:
            if m.group(2):  # a refused file may carry any text: its name is the record
                out.append({"sha": m.group(1), "lane": "", "by": "?", "side": "?", "at": None, "refused": True,
                            "name": p.name, "bad": False})
                continue
            if V1_RE.match(first):  # a KEY1 v1 header attempt that does not parse fails closed
                raise ValueError(f"approval {p.name}: malformed KEY1 v1 header")
            continue  # legacy free-text approval (pre KEY1 v1): not a key 1 record
        out.append({"sha": h["sha"], "lane": h["lane"], "by": h["by"], "side": side(h["by"], h["reason"]), "at": ts(h["at"]),
                    "refused": bool(m.group(2)), "name": p.name, "bad": h["sha"] != m.group(1)})
    return out


def same(bot_lane, bot_sha, a):
    return a["sha"] == bot_sha if bot_sha else bool(bot_lane) and a["lane"].casefold() == bot_lane.casefold()


def report(decisions, approvals, since, now):
    lines, c = [], {"agree": 0, "unsafe": 0, "stricter": 0, "unmatched": 0, "pending": 0, "conveyor": 0}
    bad = [a for a in approvals if a["bad"]]  # header sha != name sha: a key 1 for neither sha
    approvals = [a for a in approvals if not a["bad"]]
    lines += [f"{a['name']} header sha={a['sha']} differs from its file name INCONSISTENT" for a in bad]
    real = [a for a in approvals if a["side"] != "hands"]
    used, paired = set(), set()  # paired: a key 1 already agreed with one shadow write never agrees again
    for t, act, mode, lane, sha in decisions:
        near = [a for a in real if same(lane, sha, a) and (a["at"] is None or abs(a["at"] - t) <= WINDOW_S)]
        # only an orchestrator-side key 1 pairs with the bot; a conveyor-side one is no orchestrator decision
        key1 = next((a for a in near if not a["refused"] and a["side"] != "conveyor"
                     and (act != "write_key1" or a["name"] not in paired)), None)
        salih = any(not a["refused"] and a["side"] == "ask_salih_key1" for a in near)  # order-independent
        conv = next((a for a in near if not a["refused"] and a["side"] == "conveyor"), None)
        refusal = next((a for a in near if a["refused"]), None)
        used.update(a["name"] for a in near)
        shown = key1 or conv
        real_txt = f"real key1 by={shown['by']} at={iso(shown['at'])}" if shown else "no real key1"
        if act == "ask_salih_key1":
            if key1 and key1["side"] == "write_key1":
                c["stricter"] += 1
                lines.append(f"{iso(t)} bot ask_salih_key1 {lane} {sha or '-'} | {real_txt} STRICTER")
            elif shown:  # both escalated, or conveyor side: neither stricter nor a K2 count
                lines.append(f"{iso(t)} bot ask_salih_key1 {lane} {sha or '-'} | {real_txt} "
                             f"{'SAME' if key1 else 'CONVEYOR'}")
            continue
        if refusal or salih:  # bot laxer than the orchestrator
            kind = "unsafe"
        elif key1:
            kind = "agree"
            paired.add(key1["name"])
        elif conv:
            kind = "conveyor"
        elif now - t > WINDOW_S:
            kind = "unsafe"
        else:
            kind = "pending"
        if mode != "live":  # K2 counts the shadow (switch OFF) writes only; live writes are K4's
            c[kind] += 1
        lines.append(f"{iso(t)} bot write_key1 ({mode}) {lane} {sha or '-'} | {real_txt}"
                     f"{' + refusal ' + refusal['name'] if refusal else ''} {kind.upper()}")
    for a in real:  # a real key 1 no bot decision named; a conveyor-side one is listed, never unmatched
        if a["name"] not in used and not a["refused"] and a["at"] is not None and a["at"] >= since:
            conveyor = a["side"] == "conveyor"
            c["unmatched"] += not conveyor
            lines.append(f"{iso(a['at'])} real key1 by={a['by']} {a['lane']} {a['sha']} | no bot decision "
                         f"{'CONVEYOR' if conveyor else 'UNMATCHED'}")
    live = {(d[4] or d[3].casefold()) for d in decisions if d[1] == "write_key1" and d[2] == "live"}
    hands = [a for a in approvals if a["side"] == "hands" and a["at"] is not None and a["at"] >= since]
    # DECISION TAKEN: a hands key 1 mismatches when refused or no live bot write names its sha (alt: a verdict line)
    mism = sum(a["refused"] or (a["sha"] not in live and a["lane"].casefold() not in live) for a in hands)
    lines.append(f"agree {c['agree']}, unsafe {c['unsafe']}, bot-stricter {c['stricter']}, "
                 f"unmatched {c['unmatched']}, pending {c['pending']}, inconsistent {len(bad)}, "
                 f"conveyor {c['conveyor']}")
    lines.append(f"K2: {c['agree']}/10 matching, unsafe {c['unsafe']}")
    lines.append(f"K4: {len(hands)} hands key1 audited, mismatches {mism}")
    return lines


def iso(t):
    return "-" if t is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--since", default="1970-01-01T00:00:00Z")
    ap.add_argument("--now", default=None)
    a = ap.parse_args(argv)
    root = Path(a.root)
    try:
        since = ts(a.since)
        now = ts(a.now) if a.now else time.time()
        decisions = load_decisions(root / "build" / "bot" / "decisions.jsonl", since)
        approvals = load_approvals(root / "build" / "approvals" / "done")
    except (OSError, ValueError) as e:
        print(f"key1_obs: unreadable or malformed input: {e}"[:300], file=sys.stderr)
        return 2
    print("\n".join(report(decisions, approvals, since, now)))
    if any(a["bad"] for a in approvals):
        print("key1_obs: inconsistent approval (header sha differs from file name sha)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
