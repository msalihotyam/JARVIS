"""Deterministic JARVIS dev dashboard renderer.

Reads status.json, writes dashboard.html. Python standard library only:
no network, no LLM, no dependencies. Same input -> byte-identical output.
"""

from __future__ import annotations

import html
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
STATUS = HERE / "status.json"
OUTPUT = HERE / "dashboard.html"

STATUS_CLASSES = {"green", "yellow", "red", "grey"}

CSS = """
:root{--bg:#f5f6f8;--card:#fff;--fg:#15181c;--muted:#5b6570;--line:#e3e7ec;
--track:#e7ebef;--green:#1f8f4e;--yellow:#b5760a;--red:#c53a30;--grey:#8a95a1}
@media (prefers-color-scheme:dark){:root{--bg:#12151a;--card:#1a1f26;--fg:#e6eaef;
--muted:#9aa5b1;--line:#2a323b;--track:#2a323b;--green:#3fc47a;--yellow:#e0a33a;
--red:#f0736a;--grey:#7d8895}}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px 48px;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1000px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 4px}
h2{font-size:.8rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
margin:0 0 10px;font-weight:600}
h3{font-size:1rem;margin:0 0 4px}
p{margin:0 0 8px}
.sub{color:var(--muted);font-size:.85rem;margin-bottom:20px}
section{margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
.big{font-size:2.4rem;font-weight:700;line-height:1.1}
.bar{background:var(--track);border-radius:99px;height:9px;overflow:hidden;margin:8px 0 4px}
.bar>span{display:block;height:100%;background:var(--green);border-radius:99px}
.bar.yellow>span{background:var(--yellow)}
.bar.red>span{background:var(--red)}
.bar.grey>span{background:var(--grey)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;
margin-right:7px;vertical-align:middle;background:var(--grey)}
.dot.green{background:var(--green)}.dot.yellow{background:var(--yellow)}
.dot.red{background:var(--red)}
.muted{color:var(--muted);font-size:.85rem}
ul{margin:0;padding-left:18px}li{margin:3px 0}
ul.plain{list-style:none;padding:0}
.row{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.tags{display:flex;flex-wrap:wrap;gap:6px;list-style:none;padding:0;margin:0}
.tags li{border:1px solid var(--line);border-radius:99px;padding:3px 10px;
font-size:.82rem;color:var(--muted)}
details summary{cursor:pointer;font-weight:600}
dl{display:grid;grid-template-columns:auto 1fr;gap:4px 16px;margin:12px 0 0;font-size:.88rem}
dt{color:var(--muted)}dd{margin:0;word-break:break-all}
""".strip()


def git(*args: str) -> str | None:
    """Local read-only git query. Never fetches. None when git cannot answer."""
    try:
        run = subprocess.run(
            ["git", *args], cwd=HERE, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout.strip() if run.returncode == 0 else None


def git_facts() -> dict:
    """HEAD, working-tree cleanliness and ahead/behind, derived at render time.

    dashboard.html is generated and git-ignored, so re-rendering never dirties
    the tree and needs no special case here.
    """
    porcelain = git("status", "--porcelain")
    tree = None if porcelain is None else len(porcelain.splitlines())

    counts = git("rev-list", "--left-right", "--count", "origin/main...HEAD")
    behind, ahead = (counts.split() + ["", ""])[:2] if counts else ("", "")

    return {
        "head": git("rev-parse", "HEAD") or "unknown",
        "dirty_paths": tree,
        "ahead": ahead or "unknown",
        "behind": behind or "unknown",
    }


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def cls(value: object) -> str:
    text = str(value)
    return text if text in STATUS_CLASSES else "grey"


def bar(percent: object, status: str = "green") -> str:
    try:
        pct = max(0, min(100, int(percent)))
    except (TypeError, ValueError):
        pct = 0
    return (
        f'<div class="bar {cls(status)}" role="img" '
        f'aria-label="yüzde {pct} (proje tahmini)">'
        f'<span style="width:{pct}%"></span></div>'
    )


def render(data: dict) -> str:
    out: list[str] = []
    add = out.append
    facts = git_facts()

    dirty = facts["dirty_paths"]
    if dirty is None:
        tree_row = {"name": "Kanonik ağaç", "status": "grey", "detail": "bilinmiyor"}
    elif dirty:
        tree_row = {"name": "Kanonik ağaç", "status": "yellow",
                    "detail": f"Kirli: {dirty} değişmiş yol."}
    else:
        tree_row = {"name": "Kanonik ağaç", "status": "green", "detail": "Temiz."}

    branch_row = {
        "name": "Yerel main", "status": "grey",
        "detail": f'{facts["ahead"]} önde, {facts["behind"]} geride origin/main'
                  f' · {data.get("push_note", "")}'.strip(),
    }

    add('<!doctype html><html lang="tr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>JARVIS Geliştirme Panosu</title>')
    add(f"<style>{CSS}</style></head><body><main>")
    add("<h1>JARVIS Geliştirme Panosu</h1>")
    add(f'<p class="sub">Güncelleme: {esc(data.get("updated", "bilinmiyor"))} '
        f'&middot; phase {esc(data.get("phase", "bilinmiyor"))}</p>')

    # 1. Overall progress
    add('<section><h2>Kullanılabilir V1 ilerlemesi</h2><div class="card">')
    add(f'<div class="big">{esc(data.get("overall_percent", 0))}%</div>')
    add(bar(data.get("overall_percent", 0)))
    add(f'<p class="muted">{esc(data.get("overall_note", ""))}</p></div></section>')

    # 2 + 3. Milestone and next step
    milestone = data.get("milestone", {})
    add('<section><h2>Şu an</h2><div class="grid">')
    add('<div class="card">'
        f'<h3><span class="dot {cls(milestone.get("status"))}"></span>'
        f'{esc(milestone.get("name", "bilinmiyor"))}</h3>'
        f'<p class="muted">{esc(milestone.get("status_label", ""))}</p>'
        f'<p>{esc(milestone.get("summary", ""))}</p></div>')
    add('<div class="card"><h3>Sonraki adım</h3>'
        f'<p>{esc(data.get("next_step", ""))}</p></div>')
    add("</div></section>")

    # 4. Blockers
    blockers = data.get("blockers") or []
    add(f'<section><h2>Engeller ({len(blockers)})</h2><div class="card">')
    if blockers:
        add("<ul>" + "".join(f"<li>{esc(b)}</li>" for b in blockers) + "</ul>")
    else:
        add('<p><span class="dot green"></span>Engel yok.</p>')
    add("</div></section>")

    # 5. Capabilities
    add('<section><h2>Yetenekler</h2><div class="grid">')
    for cap in data.get("capabilities", []):
        add('<div class="card">'
            f'<div class="row"><h3><span class="dot {cls(cap.get("status"))}"></span>'
            f'{esc(cap.get("name", ""))}</h3>'
            f'<span class="muted">{esc(cap.get("percent", 0))}%</span></div>'
            + bar(cap.get("percent", 0), cap.get("status", "green")) +
            f'<p class="muted">{esc(cap.get("note", ""))}</p></div>')
    add('</div><p class="muted">Yüzdeler proje tahminidir, ölçüm değildir.</p></section>')

    # 6 + 7. Recently completed and system health
    add('<section><div class="grid">')
    add('<div class="card"><h2>Son tamamlananlar</h2><ul>'
        + "".join(f"<li>{esc(item)}</li>" for item in data.get("recent", [])[:5])
        + "</ul></div>")
    add('<div class="card"><h2>Sistem sağlığı</h2><ul class="plain">'
        + "".join(
            f'<li><span class="dot {cls(h.get("status"))}"></span>'
            f'<strong>{esc(h.get("name", ""))}</strong> '
            f'<span class="muted">{esc(h.get("detail", ""))}</span></li>'
            for h in [*data.get("health", []), tree_row, branch_row]
        )
        + "</ul></div>")
    add("</div></section>")

    # 8. Parked
    add('<section><h2>Park edilenler / ertelenenler</h2><div class="card"><ul class="tags">'
        + "".join(f"<li>{esc(item)}</li>" for item in data.get("parked", []))
        + "</ul></div></section>")

    # 9. Technical details
    add('<section><div class="card"><details><summary>Teknik ayrıntılar</summary><dl>'
        f'<dt>Kanonik HEAD</dt><dd>{esc(facts["head"])}</dd>'
        f'<dt>Aktif phase</dt><dd>{esc(data.get("phase", "bilinmiyor"))}</dd>'
        f'<dt>Güncelleme</dt><dd>{esc(data.get("updated", "bilinmiyor"))}</dd>'
        f'<dt>Review durumu</dt><dd>{esc(data.get("review_state", "yok"))}</dd>'
        + "".join(
            f"<dt>{esc(term)}</dt><dd>{esc(detail)}</dd>"
            for term, detail in data.get("technical", [])
        )
        + "</dl></details></div></section>")

    add("</main></body></html>")
    return "\n".join(out) + "\n"


def main() -> int:
    data = json.loads(STATUS.read_text(encoding="utf-8"))
    OUTPUT.write_text(render(data), encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
