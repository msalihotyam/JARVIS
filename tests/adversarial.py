"""Shared hostile-input corpus for slice tests (ADVCORPUS1).

Test-support only: imported by tests, never by jarvis/ or scripts/.
Data-only, stdlib only, deterministic; importing it touches no file system
or network. Every entry carries a one-line comment naming its class.
"""

import contextlib
import io

HOSTILE_PATH_PARTS = (
    "CLAUDE~1",  # 8.3 short name of .claude
    "GITHUB~1",  # 8.3 short name of .github
    "PROGRA~1",  # 8.3 short name of Program Files
    "x:stream",  # NTFS alternate data stream
    "x::$DATA",  # NTFS default data stream suffix
    "a.",  # trailing dot (Win32 strips it)
    "a ",  # trailing space (Win32 strips it)
    "CON",  # reserved device name
    "PRN",  # reserved device name
    "AUX",  # reserved device name
    "NUL",  # reserved device name
    "COM1",  # reserved device name
    "LPT9",  # reserved device name
    "con.txt",  # reserved device name with extension, lower case
    "NUL.png",  # reserved device name with extension
    ".Claude",  # case variant of protected dir .claude
    ".GITHUB/workflows",  # case variant of protected dir .github/workflows
    "DATA",  # case variant of protected dir data
    ".Jarvis",  # case variant of protected dir .jarvis
    "..",  # parent traversal
    ".",  # current dir
    "//",  # doubled forward separator
    "\\",  # backslash separator alone
    "a/b\\c",  # mixed separators
    "C:x",  # drive-relative path
    "C:\\x",  # absolute Windows path
    "/x",  # absolute POSIX / root-relative path
    "\\\\?\\C:\\x",  # \\?\ extended-length prefix
    "\\\\.\\C:\\x",  # \\.\ device namespace prefix
    "\\\\host\\share",  # UNC path
    "\\\\localhost\\C$",  # UNC admin share on localhost
    "a\uff0fb",  # unicode look-alike: FULLWIDTH SOLIDUS U+FF0F
    "a\u2215b",  # unicode look-alike: DIVISION SLASH U+2215
    ".cl\u0430ude",  # unicode look-alike: Cyrillic a in .claude
    "g\u0435t",  # unicode look-alike: Cyrillic e
    "d\u043ecs",  # unicode look-alike: Cyrillic o
)

HOSTILE_TEXT = (
    "a\u202ab",  # bidi control LRE U+202A
    "a\u202bb",  # bidi control RLE U+202B
    "a\u202cb",  # bidi control PDF U+202C
    "a\u202db",  # bidi control LRO U+202D
    "a\u202eb",  # bidi control RLO U+202E
    "a\u2066b",  # bidi control LRI U+2066
    "a\u2067b",  # bidi control RLI U+2067
    "a\u2068b",  # bidi control FSI U+2068
    "a\u2069b",  # bidi control PDI U+2069
    "a\u200bb",  # zero-width space U+200B
    "a\u200cb",  # zero-width non-joiner U+200C
    "a\u200db",  # zero-width joiner U+200D
    "\ufeffa",  # zero-width no-break space / BOM U+FEFF
    "a\u034fb",  # default-ignorable COMBINING GRAPHEME JOINER U+034F
    "a\u180bb",  # default-ignorable MONGOLIAN FVS1 U+180B
    "a\u180cb",  # default-ignorable MONGOLIAN FVS2 U+180C
    "a\u180db",  # default-ignorable MONGOLIAN FVS3 U+180D
    "a\ufe00b",  # default-ignorable VARIATION SELECTOR-1 U+FE00
    "a\ufe0fb",  # default-ignorable VARIATION SELECTOR-16 U+FE0F
    "a\u115fb",  # default-ignorable HANGUL CHOSEONG FILLER U+115F
    "a\u3164b",  # default-ignorable HANGUL FILLER U+3164
    "a\x07b",  # C0 control BEL
    "\x1b[31mred",  # C0 control ESC (ANSI escape sequence)
    "a\x7fb",  # DEL control
    "a\x85b",  # C1 control NEL U+0085
    "a\x9bb",  # C1 control CSI U+009B
    "a\x00b",  # NUL byte
    "a\ud800b",  # lone high surrogate U+D800
    "a\udfffb",  # lone low surrogate U+DFFF
    "a\r\nX-Injected: 1",  # CRLF injection
    "a\rb",  # bare CR injection
    "a<br>b",  # HTML injection
    "<script>alert(1)</script>",  # HTML script injection
    "![x](https://x)",  # markdown image injection (remote fetch)
    "[x](javascript:alert(1))",  # markdown link injection
    "A" * 100_000,  # very long string (100k)
    "\u0130\u0323",  # Turkish dotted capital I U+0130 + combining dot below
    "I\u0307",  # Turkish I + combining dot above U+0307
    "\u0131",  # Turkish dotless i U+0131 (not in cp1252)
)


def deep_json(depth=5000):
    """Nesting-depth bomb, built lazily so import stays cheap."""
    return "[" * depth + "]" * depth


HOSTILE_JSON = (
    '{"value": "a", "value": "b"}',  # duplicate keys (last wins silently)
    "1" + "0" * 400,  # 10**400 integer as JSON text
    "1e400",  # 10**400 float text (overflows to inf)
    "NaN",  # NaN literal (not valid JSON)
    "Infinity",  # Infinity literal (not valid JSON)
    "-Infinity",  # -Infinity literal (not valid JSON)
    '{"value": ["x"]}',  # wrong type for str field: list
    '{"value": {"x": 1}}',  # wrong type for str field: dict
    '{"value": null}',  # wrong type for str field: None
    '{"value": true}',  # wrong type for str field: bool
    '{"value": 1}',  # wrong type for str field: int
    '{"count": true}',  # bool where int expected
)

HOSTILE_NUMBERS = (
    True,  # bool posing as int 1
    False,  # bool posing as int 0
    -0.0,  # negative zero
    float("nan"),  # NaN
    float("inf"),  # +infinity
    float("-inf"),  # -infinity
    10**400,  # huge int (float() overflows)
    "1",  # numeric str
    None,  # missing value
)


def each(case, values, check):
    """Run check(value) in case.subTest per value; every failing value is reported."""
    for i, value in enumerate(values):
        with case.subTest(i=i, value=repr(value)[:80]):
            check(value)


@contextlib.contextmanager
def narrow_stdout():
    """Swap sys.stdout for a strict cp1252 stream (catches UnicodeEncodeError in print paths)."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    with contextlib.redirect_stdout(stream):
        yield stream
