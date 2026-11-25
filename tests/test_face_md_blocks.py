"""FACEMD1: markdown -> typed blocks for the reading view (jarvis/face/md_blocks.py). Pure; no file, network or DB."""

import ast
import time
import unittest
from pathlib import Path

from jarvis.face import md_blocks as md
from jarvis.face.md_blocks import Inline
from tests.adversarial import HOSTILE_TEXT, each

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "jarvis" / "face" / "md_blocks.py"

SAMPLE = (
    "# Title\n\nSome **bold** and *italic* and `code` and [a link](https://example.com/x).\n\n"
    "- one\n- two\n  - two.a\n  - two.b\n\n3. three\n4. four\n\n"
    "| L | C | R |\n|:--|:-:|--:|\n| 1 | 2 | 3 |\n\n"
    "```python\nprint('hi')\n```\n\n> [!NOTE]\n> careful\n\n---\n\n### End\nlast para\n"
)


def text(*parts):
    return tuple(Inline("text", p) for p in parts)


def links(doc):
    """Every link/image chip anywhere: block inlines, list items and their children, table header and rows."""
    groups = []
    for b in doc.blocks:
        groups += [b.inlines, *b.header, *(cell for row in b.rows for cell in row)]
        for item in b.items:
            groups += [item.inlines, *(c.inlines for c in item.children)]
    return [i for g in groups for i in g if i.kind in ("link", "image")]


def shape(doc):
    """List blocks as [(item text, [child texts])] per block, for nesting asserts."""
    flat = lambda inl: "".join(i.text for i in inl)  # noqa: E731
    return [[(flat(i.inlines), [flat(c.inlines) for c in i.children]) for i in b.items] for b in doc.blocks]


# Default_Ignorable_Code_Point that are not Cf (from Unicode DerivedCoreProperties), one per range end
IGNORABLE_NOT_CF = "͏ᅟᅠ឴឵᠋᠍᠏ㅤ︀️ﾠ\U000e0100\U000e01ef"


class StaticTests(unittest.TestCase):
    def test_pure_imports_and_no_io_calls(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertLessEqual(imported, {"__future__", "re", "string", "unicodedata", "dataclasses", "urllib.parse"})
        called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertFalse(called & {"open", "eval", "exec", "compile", "__import__"})

    def test_deterministic(self):
        self.assertEqual(md.parse(SAMPLE), md.parse(SAMPLE))


class AcceptanceTests(unittest.TestCase):
    def test_1_every_block_kind(self):
        doc = md.parse(SAMPLE)
        self.assertEqual(doc.refused, "")
        self.assertFalse(doc.truncated)
        self.assertEqual([b.kind for b in doc.blocks], ["heading", "paragraph", "list", "list", "table", "code",
                                                        "quote", "rule", "heading", "paragraph"])
        h, p, ul, ol, table, code, quote, _, h3, _ = doc.blocks
        self.assertEqual((h.level, h.inlines), (1, text("Title")))
        self.assertEqual(h3.level, 3)
        self.assertEqual(p.inlines, (
            Inline("text", "Some "), Inline("bold", "bold"), Inline("text", " and "), Inline("italic", "italic"),
            Inline("text", " and "), Inline("code", "code"), Inline("text", " and "),
            Inline("link", "a link", "https://example.com/x"), Inline("text", ".")))
        self.assertFalse(ul.ordered)
        self.assertEqual([i.inlines for i in ul.items], [text("one"), text("two")])
        self.assertEqual([c.inlines for c in ul.items[1].children], [text("two.a"), text("two.b")])
        self.assertEqual((ol.ordered, ol.start, len(ol.items)), (True, 3, 2))
        self.assertEqual(table.header, (text("L"), text("C"), text("R")))
        self.assertEqual(table.rows, ((text("1"), text("2"), text("3")),))
        self.assertEqual(table.align, ("left", "center", "right"))
        self.assertEqual((code.language, code.text), ("python", "print('hi')"))
        self.assertEqual((quote.note, quote.inlines), ("note", text("careful")))

    def test_1_heading_levels_cap_at_3_and_nesting_stops_at_2(self):
        self.assertEqual([b.level for b in md.parse("# a\n## b\n### c\n###### d\n").blocks], [1, 2, 3, 3])
        self.assertEqual(md.parse("####### x\n").blocks[0].kind, "paragraph")
        doc = md.parse("1. a\n   - b\n      - c\n")
        item = doc.blocks[0].items[0]
        self.assertEqual([c.inlines for c in item.children], [text("b"), text("c")])  # deeper -> level 2
        self.assertTrue(all(not c.children for c in item.children))
        self.assertFalse(item.child_ordered)

    def test_1_plain_quote_and_escapes(self):
        doc = md.parse("> just a quote\n\n\\*not italic\\* and \\`no code\\`\n")
        self.assertEqual((doc.blocks[0].kind, doc.blocks[0].note), ("quote", ""))
        self.assertEqual(doc.blocks[1].inlines, text("*not italic* and `no code`"))

    def test_2_raw_html_stays_visible_text(self):
        raw = '<script>alert(1)</script> <img src="http://x/y.png" onerror=z> <b>hi</b>'
        doc = md.parse(raw + "\n")
        self.assertEqual(doc.blocks[0].inlines, text(raw))
        self.assertEqual(md.parse("<div>\n\n# x\n").blocks[0].inlines, text("<div>"))

    def test_3_only_http_links_and_images_are_chips(self):
        doc = md.parse("[a](http://a.example/) ![cat](https://img.example/c.png)\n")
        self.assertEqual(links(doc), [Inline("link", "a", "http://a.example/"),
                                      Inline("image", "cat", "https://img.example/c.png")])
        self.assertEqual(links(md.parse("![](https://img.example/c.png)\n"))[0].text, "https://img.example/c.png")
        self.assertEqual(md.parse("hi! there\n").blocks[0].inlines, text("hi! there"))

    def test_4_streaming_closed_blocks_never_change(self):
        full = md.parse(SAMPLE)
        for k in range(len(SAMPLE) + 1):
            doc = md.parse(SAMPLE[:k])
            with self.subTest(k=k):
                self.assertEqual(doc.blocks[:doc.stable], full.blocks[:doc.stable])
                self.assertLessEqual(doc.stable, len(doc.blocks))
        self.assertEqual(md.parse(SAMPLE + "\n").stable, len(full.blocks))
        self.assertLess(md.parse("# a\n#").stable, 1 + 1)  # the growing "#" line is never final
        self.assertEqual(md.parse("para\n---").stable, 0)

    def test_4_unclosed_fence_is_code_to_the_end(self):
        doc = md.parse("intro\n\n```js\nlet a = 1;\n# not a heading\n\n- not a list\n")
        self.assertEqual([b.kind for b in doc.blocks], ["paragraph", "code"])
        self.assertEqual(doc.blocks[1].text, "let a = 1;\n# not a heading\n\n- not a list")
        self.assertEqual(doc.stable, 1)


def best_of_3(source):
    best = None
    for _ in range(3):
        start = time.perf_counter()
        doc = md.parse(source)
        took = time.perf_counter() - start
        best = took if best is None else min(best, took)
    return best, doc


class NegativeTests(unittest.TestCase):
    def test_1_unclosed_nested_and_list_fences(self):
        doc = md.parse("```\nnever closed\n- x\n")
        self.assertEqual([(b.kind, b.text) for b in doc.blocks], [("code", "never closed\n- x")])
        doc = md.parse("````md\n```\ninner\n```\n````\nafter\n")
        self.assertEqual([b.kind for b in doc.blocks], ["code", "paragraph"])
        self.assertEqual((doc.blocks[0].language, doc.blocks[0].text), ("md", "```\ninner\n```"))
        doc = md.parse("- a\n```\ncode\n```\n- b\n")
        self.assertEqual([b.kind for b in doc.blocks], ["list", "code", "list"])
        self.assertEqual(doc.blocks[1].text, "code")
        doc = md.parse("- a\n  ```\n  - code\n")  # an indented fence in a list still opens code
        self.assertEqual(doc.refused, "")
        self.assertIn("code", [b.kind for b in doc.blocks])

    def test_2_ragged_or_headerless_table_is_paragraph(self):
        for source in ("| a | b |\n|---|---|\n| 1 | 2 | 3 |\n", "|---|---|\n| 1 | 2 |\n",
                       "| a | b |\n|---|\n| 1 | 2 |\n", "| a | b |\n| 1 | 2 |\n"):
            with self.subTest(source=source):
                self.assertEqual([b.kind for b in md.parse(source).blocks], ["paragraph"])

    def test_3_unsafe_schemes_stay_text(self):
        for href in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "file:///etc/passwd",
                     "data:text/html,<b>x</b>", "\\\\server\\share\\x", "//host.example/x", "HTTP://a.example/",
                     "Https://a.example/", "https://user@a.example/", "vbscript:x", "https:///nohost",
                     "https://a.example/a b", "ftp://a.example/"):
            for source in (f"[x]({href})\n", f"![x]({href})\n", f"- [x]({href})\n"):
                with self.subTest(source=source):
                    doc = md.parse(source)
                    self.assertEqual(doc.refused, "")
                    self.assertEqual(links(doc), [])

    def test_4_deep_nesting_huge_input_and_wide_table_are_capped(self):
        for source in (">" * 10_000 + " x", "- " * 10_000 + "x", "  " * 10_000 + "- x", "[" * 10_000,
                       "*" * 10_000 + "x", "`" * 10_000 + "x", "](" * 10_000, "[a](" * 10_000,
                       "1. " * 10_000, "#" * 10_000 + " x", "|" * 10_000 + "\n" + "|-" * 10_000):
            with self.subTest(source=source[:12]):
                doc = md.parse(source)
                self.assertEqual(doc.refused, "")
                self.assertLessEqual(len(doc.blocks), md.MAX_BLOCKS)
        doc = md.parse("**x** [y](https://a.example/) `z`\n" * 30_000)  # about 1 MB
        self.assertTrue(doc.truncated)
        self.assertLessEqual(len(doc.blocks), md.MAX_BLOCKS)
        wide = "|" + "h|" * 10_000 + "\n|" + "-|" * 10_000 + "\n|" + "c|" * 10_000 + "\n"
        table = md.parse(wide).blocks[0]
        self.assertEqual((table.kind, len(table.header), len(table.rows[0])), ("table", 64, 64))
        self.assertTrue(md.parse(wide).truncated)
        self.assertTrue(md.parse("| " + "x" * 5_000 + " |\n|---|\n").truncated)  # cell text cap

    def test_4_linear_time_against_plain_text(self):
        size = 1_000_000
        plain, _ = best_of_3(("plain words " * 8 + "\n") * (size // 97))
        for chunk in ("*a _b `c [d](e ![f] ", "> - 1. # ``` | ", "[" * 50 + "](" * 50 + "\n", "**_`" * 25 + "\n"):
            source = (chunk * (size // len(chunk) + 1))[:size]
            took, doc = best_of_3(source)
            with self.subTest(chunk=chunk[:10]):
                self.assertEqual(doc.refused, "")
                self.assertLess(took, plain * 25 + 0.05)

    def test_5_bidi_and_zero_width_removed_from_targets_kept_in_text(self):
        doc = md.parse("[a\u202eb](https://ex.example/a\u200bb\u202e\u2066c\ufeff)\n")
        self.assertEqual(links(doc), [Inline("link", "a\u202eb", "https://ex.example/abc")])
        self.assertEqual(md.parse("x\u202ey\u200bz\n").blocks[0].inlines, text("x\u202ey\u200bz"))

        def check(value):
            doc = md.parse(f"{value} [t{value}](https://a.example/{value}) ![i](https://b.example/?q={value})\n")
            self.assertEqual(doc.refused, "")
            for link in links(doc):
                self.assertTrue(link.href.startswith(("http://", "https://")))
                self.assertFalse([c for c in link.href if c in "\u200b\u200e\u200f\u202a\u202b\u202c\u202d"
                                  "\u202e\u2066\u2067\u2068\u2069\ufeff" or not c.isprintable()])

        each(self, HOSTILE_TEXT, check)

    def test_6_line_endings_give_same_blocks(self):
        lf = md.parse(SAMPLE)
        for source in (SAMPLE.replace("\n", "\r\n"), SAMPLE.replace("\n", "\r"),
                       # a "\r" is always followed by "\r\n" here, so no accidental CRLF pair forms
                       "".join(line + ("\r\n", "\n", "\r")[i % 3] for i, line in enumerate(SAMPLE.split("\n")))):
            with self.subTest(source=source[:20]):
                self.assertEqual(md.parse(source).blocks, lf.blocks)

    def test_8_nesting_is_relative_to_the_root_marker_indent(self):
        cases = {
            "- a\n- b\n": [[("a", []), ("b", [])]],
            " - a\n - b\n": [[("a", []), ("b", [])]],
            "  - a\n  - b\n": [[("a", []), ("b", [])]],
            "   - a\n   - b\n   - c\n": [[("a", []), ("b", []), ("c", [])]],
            "\t- a\n\t- b\n": [[("a", []), ("b", [])]],  # tab root (expands to 4)
            "  - a\n   - b\n": [[("a", []), ("b", [])]],  # root + 1 is still a sibling
            "  - a\n    - b\n  - c\n": [[("a", ["b"]), ("c", [])]],  # root + 2 nests
            "   1. a\n      - b\n   2. c\n": [[("a", ["b"]), ("c", [])]],
            "   - a\n- b\n  - c\n": [[("a", []), ("b", ["c"])]],  # root drifts 3 -> 0, child measured from 0
            "  - a\n  1. b\n": [[("a", [])], [("b", [])]],  # type switch at an indented root: new list
        }
        for source, want in cases.items():
            with self.subTest(source=source):
                self.assertEqual(shape(md.parse(source)), want)
        for child in ("  - c\n", "  1. c\n"):  # child overflow is dropped and flagged, never promoted to a root
            with self.subTest(child=child):
                doc = md.parse("- p\n" + child * (md.MAX_ITEMS + 3) + "- q\n")
                self.assertTrue(doc.truncated)
                self.assertEqual([(t, len(c)) for t, c in shape(doc)[0]], [("p", md.MAX_ITEMS), ("q", 0)])
                self.assertEqual(len(doc.blocks), 1)
        doc = md.parse("1. step\n\n   - a\n   - b\n")  # common LLM shape: blank line, then an indented list
        self.assertEqual([b.kind for b in doc.blocks], ["list", "list"])
        self.assertEqual(shape(doc)[1], [("a", []), ("b", [])])
        source = "  - a\n  - b\n    - b.1\n  - c\n\nend\n"
        full = md.parse(source)
        for k in range(len(source) + 1):
            doc = md.parse(source[:k])
            with self.subTest(k=k):
                self.assertEqual(doc.blocks[:doc.stable], full.blocks[:doc.stable])

    def test_9_default_ignorable_removed_from_targets_kept_in_text(self):
        for ch in IGNORABLE_NOT_CF:
            with self.subTest(ch=hex(ord(ch))):
                doc = md.parse(f"[x{ch}](https://ex.example/a{ch}b)\n")
                self.assertEqual(links(doc), [Inline("link", f"x{ch}", "https://ex.example/ab")])
                doc = md.parse(f"[x](ht{ch}tps://ex.example/)\n")  # inside the scheme: stripped, then checked
                self.assertEqual(links(doc), [Inline("link", "x", "https://ex.example/")])
                doc = md.parse(f"[x]({ch}javascript:alert(1))\n")
                self.assertEqual(links(doc), [])
        for ch in "⁥￰\U000e0080":  # unassigned default-ignorables: the whole link stays text
            with self.subTest(ch=hex(ord(ch))):
                self.assertEqual(links(md.parse(f"[x](https://ex.example/a{ch}b)\n")), [])

    def test_10_links_helper_sees_children_and_table_cells(self):
        doc = md.parse("- a\n  - [c](https://c.example/)\n\n| [h](https://h.example/) |\n|---|\n"
                       "| [r](https://r.example/) |\n")
        self.assertEqual([i.href for i in links(doc)], ["https://c.example/", "https://h.example/",
                                                        "https://r.example/"])
        for href in ("javascript:alert(1)", "https://ex.example/͏"):
            source = f"- a\n  - [c]({href})\n\n| [h]({href}) |\n|---|\n| [r]({href}) |\n"
            with self.subTest(href=href):
                for link in links(md.parse(source)):
                    self.assertTrue(link.href.startswith("https://"))
                    self.assertFalse(set(link.href) & set(IGNORABLE_NOT_CF))

    def test_7_non_text_is_refused(self):
        class Sneaky(str):
            pass

        for value in (None, b"# x", bytearray(b"x"), 1, 1.5, ["# x"], {"a": 1}, object(), Sneaky("# x"),
                      True, False, -0.0):
            with self.subTest(value=repr(value)):
                self.assertEqual(md.parse(value), md.Doc(refused="not_text"))
