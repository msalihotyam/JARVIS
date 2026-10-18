"""Deterministic 'never stored' category filter for memory candidates (V2.1a M2).

Core is the authority here (I1/I5): a candidate memory text is checked by
plain regex and checksum rules - never by a model - before it is stored, and
the store checks it again. Anything the filter cannot decide is refused:
an exception inside it is ``filter_error`` and an over-long input is
``too_long``. Over-blocking is the safe direction; a refused candidate is
simply not remembered.

Every pattern is linear in the input: repetitions are bounded or anchored to
the start of a run, so no adversarial string triggers catastrophic
backtracking. The verdict reason names the category only and never echoes
the matched text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .contracts import credential_bearing

#: Memory candidates are short notes; anything longer is refused unread.
MAX_CANDIDATE_CHARS = 8_000

# Every format (Cf) character is dropped before the checks (soft hyphen,
# zero-width, bidi, word joiners ...), plus these blank non-Cf fillers.
_INVISIBLE_FILLERS = frozenset("͏ᅟᅠㅤﾠ")
# Marks, unassigned code points (with Cf and the fillers above this covers every
# D17 default-ignorable) and the blank braille cell are dropped only where both
# neighbours belong to a number spelling, so they cannot split a card, IBAN or
# id. Elsewhere they stay: Turkish text and the raw-redaction comparison in
# skill_filter/experience rely on a mark after a lower-case letter surviving.
_GLUE_CATEGORIES = frozenset({"Mn", "Mc", "Me", "Cn"})
_NUMBER_CONTEXT = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ .-/")


@dataclass(frozen=True)
class FilterVerdict:
    allowed: bool
    category: str | None
    reason: str


# --- protected paths: data\, ~\.jarvis\, .env* followed by quoted content ---
_PROTECTED_PATH_PATTERN = re.compile(
    r"(?:(?<![\w.])data[\\/]|\.jarvis[\\/]|(?<![\w.])\.env(?:\.[\w-]{1,20})?(?![\w-]))"
    r"[^\n]{0,80}?(?::[ \t]*\S|\n[ \t]*\S|```|\b(?:contents?|içeriği|içerik)\b)",
    re.IGNORECASE,
)

# --- .env-style KEY=VALUE lines whose name is a secret ---
_ENV_LINE_PATTERN = re.compile(
    r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]{0,63})[ \t]*=[ \t]*\S", re.MULTILINE
)
_ENV_SECRET_PARTS = frozenset(
    {"KEY", "APIKEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PASS", "PWD",
     "CREDENTIAL", "CREDENTIALS", "AUTH", "DSN", "PRIVATE", "SALT", "PIN"}
)
_ENV_SECRET_SUFFIXES = ("APIKEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")

# --- credentials beyond contracts.credential_bearing ---
# Label -> value separator: blanks, ":", "=", "-", "->", one line break. Bounded
# repetitions so a long blank run after a label stays linear.
_LABEL_SEP = r"(?=[ \t\n:=>-])[ \t]{0,8}[:=>-]{0,2}[ \t]{0,8}\n?[ \t]{0,8}"
_EXTRA_CREDENTIAL_PATTERNS = (
    re.compile(r"-{3,10}[ \t]{0,3}BEGIN[A-Z \t_-]{0,40}PRIVATE[ \t_-]{0,3}KEY", re.IGNORECASE),
    # bare DER/PKCS#8 body: a base64 run of 64+ characters starting "MII"
    re.compile(r"(?<![A-Za-z0-9+/])MII[A-Za-z0-9+/]{61}"),
    re.compile(r"(?<![A-Za-z0-9])(?:[rs]k_(?:live|test)_\w{16,}|xox[abposr]-[\w-]{10,})"),
    # user:password@host inside a URL; the scheme start is anchored to a run start.
    re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s/:@]{1,256}:[^\s/@]{1,256}@"),
    re.compile(r"(?<!\w)(?:şifre|sifre|parola)\w{0,6}[ \t]*[:=][ \t]*\S", re.IGNORECASE),
    # "şifrem 1234", "Wi-Fi şifresi Kediler2024": a value token carrying a digit.
    re.compile(r"(?<!\w)(?:şifre|sifre|parola)\w{0,6}[ \t]+\S{0,64}\d", re.IGNORECASE),
    # "Parolam KediTopu": the first-person form names the speaker's own password;
    # a value with a capital or a digit, so "Şifrem değişti" stays prose.
    re.compile(r"(?<!\w)(?i:şifre|sifre|parola)(?:m|miz|mız)" + _LABEL_SEP + r"\S{0,63}[A-Z0-9ÇĞİÖŞÜ]"),
    re.compile(r"(?<!\w)(?:password|passcode|passphrase)\s+(?:is|was)\s+\S", re.IGNORECASE),
    # "password hunter2": a short unquoted value carrying a digit after the label.
    re.compile(
        r"(?<!\w)(?:password|passwd|passcode|passphrase|pwd)s?" + _LABEL_SEP + r"\S{0,63}\d",
        re.IGNORECASE,
    ),
    # "key sk-AbC123": a value with a digit and mixed case (6+) or any letter (12+)
    # after a key/token label; "key sha256", "token utf8mb4" stay prose.
    re.compile(
        r"(?<!\w)(?i:(?:api[ _-]?)?key|token|secret)(?i:s?)" + _LABEL_SEP + r"(?=\S{0,63}\d)"
        r"(?:(?=\S{0,63}[a-z])(?=\S{0,63}[A-Z])\S{6}|(?=\S{0,63}[A-Za-z])\S{12})"
    ),
)

# --- payment cards (Luhn) and IBANs (mod 97) ---
# A run of digits joined by up to three spaces/dots/dashes/slashes; card spans
# are tried inside it.
_DIGIT_RUN = re.compile(r"\d(?:[ .\-/]{0,3}\d)*")
_DIGIT_SEPARATORS = re.compile(r"([ .\-/]+)")
_CARD_MAX_GROUPS = 6
# Country code may be lower case ("tr33 0006 ..."); the BBAN stays upper case so
# ordinary prose after "ab12" is not read as an account number.
_IBAN_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9])(?i:[A-Z]{2}) ?\d{2}(?:[ \-]{0,3}[A-Z0-9]){11,30}(?![A-Za-z0-9])"
)
_IBAN_LENGTHS = {
    "TR": 26, "DE": 22, "GB": 22, "FR": 27, "NL": 18, "ES": 24, "IT": 27, "BE": 16,
    "CH": 21, "AT": 20, "PL": 28, "SE": 24, "NO": 15, "DK": 18, "FI": 18, "IE": 22,
    "PT": 25, "LU": 20, "GR": 27,
}

# --- government ids: TC kimlik (checksum), US SSN ---
_TC_CANDIDATE = re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")
# Only the 3-3-3-2 and 3-2-2-2-2 spellings are joined; other digit groups
# (dates, versions, phone numbers) are never compacted into a candidate.
_TC_GROUPED = re.compile(
    r"(?<!\d)[1-9]\d\d(?:[ .\-]{1,3}\d{3}[ .\-]{1,3}\d{3}[ .\-]{1,3}\d\d"
    r"|[ .\-]{1,3}\d\d[ .\-]{1,3}\d\d[ .\-]{1,3}\d\d[ .\-]{1,3}\d\d)(?!\d)"
)
_SSN_PATTERN = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])")

# --- phone numbers ---
_PHONE_PATTERNS = (
    # TR mobile and landline: 0532 123 45 67, +90 532 123 4567, 5321234567, 0212 345 67 89
    re.compile(
        r"(?<![\d+])(?:\+?90[\s-]?)?\(?(?:0?5|0[2-4])\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)"
    ),
    # international with a leading +
    re.compile(r"(?<![\w+])\+\d{1,3}[\s.-]?(?:\(\d{1,4}\)[\s.-]?)?\d{2,4}(?:[\s.-]?\d{2,4}){1,3}(?!\d)"),
    # North American: (415) 555-0132, 415-555-0132
    re.compile(r"(?<![\w(])\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"),
)

# --- precise home address ---
_ADDRESS_PATTERNS = (
    # TR: a mahalle/sokak/cadde/bulvar part and a door number nearby.
    re.compile(
        r"(?<!\w)(?:mahallesi|mah\.|sokak|sokağı|sok\.|sk\.|caddesi|cad\.|cd\.|bulvarı|blv\.)"
        r"[^\n]{0,80}?\bno\s*[:.]?\s*\d",
        re.IGNORECASE,
    ),
    # EN: house number + capitalised street name + street type.
    re.compile(
        r"(?<!\w)\d{1,5}[A-Z]?\s+(?:[A-Z][a-z]+\.?\s+){1,3}"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Way|Place|Pl)\b"
    ),
    re.compile(
        r"(?<!\w)(?:home address|my address|ev adres(?:im|i)|adresim)\s*(?:is|:|=|şu)\s*\S",
        re.IGNORECASE,
    ),
)

# --- health data keywords (TR + EN); word-start stems ---
# ponytail: keyword list, not NLP; Turkish dotted capital İ does not fold to i.
_HEALTH_PATTERN = re.compile(
    r"(?<!\w)(?:"
    r"diagnosed|diagnosis|depress|anxiety|cancer|tumou?r|chemotherap|diabet|insulin"
    r"|antidepressant|psychiatr|schizophren|bipolar|pregnan|miscarriage|blood pressure"
    r"|hypertension|prescription|medication|medical condition|disease|illness|seizure"
    r"|epilep|asthma|allerg|therapist|hiv\b|adhd\b"
    r"|teşhis(?:i)? kon|tanı(?:sı)? kon|depresyon|anksiyete|kanser|tümör|kemoterapi|diyabet"
    r"|şeker hastası|insülin|antidepresan|psikiyatr|şizofren|hamile|gebelik|tansiyon"
    r"|hipertansiyon|ilaç kullan|hastalı[kğ]|astım|alerji|epilep|terapist|ameliyat|dehb\b"
    r")",
    re.IGNORECASE,
)


# --- exfiltration carriers (I20): text a renderer or model could turn into a fetch ---
#: A base64 run this long (non-whitespace characters) is a carrier.
BASE64_MIN_RUN = 40
_CARRIER_PATTERNS = (
    # http/https/ftp(s) scheme; browsers read backslashes as slashes.
    re.compile(r"(?:https?|ftps?)\s{0,3}:\s{0,3}[/\\]\s{0,3}[/\\]", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])www\s{0,3}\.\s{0,3}[A-Za-z0-9]", re.IGNORECASE),
    re.compile(r"data:(?:[a-z][\w.+-]{0,63}/[\w.+-]|[;,])", re.IGNORECASE),
    # HTML: <tag after a non-word char (so `List<String>` and x<y stay prose), a
    # fetching tag anywhere, </tag, <!-- and <!DOCTYPE / <![CDATA[. An unclosed tag
    # still counts: the next text in a rendered feed can close it.
    re.compile(
        r"(?<!\w)<[A-Za-z][\w:-]{0,63}(?:[\s/>]|$)|</[A-Za-z]|<!(?:--|\[|[A-Za-z])"
        r"|<(?:img|image|a|svg|iframe|frame|script|style|link|meta|object|embed|base|video"
        r"|audio|source|picture|form|input|body|math|template|track)(?:[\s/>]|$)",
        re.IGNORECASE,
    ),
    # autolink <scheme:...>, e.g. <mailto:...?body=...>, <javascript:...>
    re.compile(r"<[A-Za-z][A-Za-z0-9+.-]{1,31}:"),
)
_MD_IMAGE_PATTERN = re.compile(r"!\[")  # any image: inline, reference or shortcut
_MD_LINK_PATTERNS = (
    re.compile(r"\]\("),  # inline [a](u); nested brackets in the label do not hide it
    re.compile(r"\]\["),  # reference [a][r] and collapsed [a][]
    # definition [r]: u - also inside list items / quotes and with escaped or
    # line-split labels, so any "]:" counts
    re.compile(r"\]:"),
)
# ponytail: segments of 16+ chars joined by single whitespace; base64 chopped
# into shorter chunks reads like prose and passes. Upper, lower and a digit
# are required so hex hashes, paths and long words stay allowed.
_BASE64_RUN = re.compile(
    r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{16,}(?:\s[A-Za-z0-9+/_-]{16,})*"
)


def _has_base64_run(text: str) -> bool:
    for match in _BASE64_RUN.finditer(text):
        run = "".join(match.group().split())
        if (
            len(run) >= BASE64_MIN_RUN
            and re.search(r"[A-Z]", run)
            and re.search(r"[a-z]", run)
            and re.search(r"\d", run)
        ):
            return True
    return False


def _has_exfil_carrier(text: str) -> bool:
    return (
        _MD_IMAGE_PATTERN.search(text) is not None
        or any(p.search(text) for p in _CARRIER_PATTERNS + _MD_LINK_PATTERNS)
        or _has_base64_run(text)
    )


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _iban_valid(compact: str) -> bool:
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 == 1


def _tc_kimlik_valid(number: str) -> bool:
    d = [int(char) for char in number]
    tenth = ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10
    return d[9] == tenth and d[10] == sum(d[:10]) % 10


def _has_env_secret(text: str) -> bool:
    for match in _ENV_LINE_PATTERN.finditer(text):
        name = match.group(1).upper()
        if name.endswith(_ENV_SECRET_SUFFIXES) or any(
            part in _ENV_SECRET_PARTS for part in name.split("_")
        ):
            return True
    return False


def _has_card(text: str) -> bool:
    # Every span of up to six adjacent groups is tried, so neighbouring digits
    # ("no 1 4111 ...", "... 1111 12/27") cannot hide a valid card number.
    # A span joined by anything but a single space/dash must look like a card
    # layout (groups of 4-6, the last 3-6: 4-4-4-4, 4-6-5, 4-4-4-4-3), so date
    # ranges and IP lists do not pass Luhn by chance.
    for match in _DIGIT_RUN.finditer(text):
        parts = _DIGIT_SEPARATORS.split(match.group())
        groups, separators = parts[::2], parts[1::2]
        for start in range(len(groups)):
            digits = ""
            loose = False
            layout = True
            for index in range(start, min(start + _CARD_MAX_GROUPS, len(groups))):
                if index > start:
                    loose = loose or separators[index - 1] not in (" ", "-")
                    layout = layout and 4 <= len(groups[index - 1]) <= 6
                group = groups[index]
                digits += group
                if len(digits) > 19:
                    break
                if (
                    len(digits) >= 13
                    and (not loose or (layout and 3 <= len(group) <= 6))
                    and _luhn_valid(digits)
                ):
                    return True
    return False


def _has_iban(text: str) -> bool:
    for match in _IBAN_CANDIDATE.finditer(text):
        compact = re.sub(r"[ \-]", "", match.group()).upper()
        length = _IBAN_LENGTHS.get(compact[:2])
        if length is not None:
            if len(compact) < length:
                continue
            compact = compact[:length]
        if _iban_valid(compact):
            return True
    return False


def _has_national_id(text: str) -> bool:
    if any(_tc_kimlik_valid(match.group()) for match in _TC_CANDIDATE.finditer(text)):
        return True
    if any(
        _tc_kimlik_valid(re.sub(r"\D", "", match.group())) for match in _TC_GROUPED.finditer(text)
    ):
        return True
    return _SSN_PATTERN.search(text) is not None


def _has_credential(text: str) -> bool:
    return credential_bearing(text) or any(p.search(text) for p in _EXTRA_CREDENTIAL_PATTERNS)


#: Checked in order; the first hit names the category.
_CHECKS = (
    ("protected_path", "quotes content from a protected path", _PROTECTED_PATH_PATTERN.search),
    ("env_secret", "contains a .env-style secret assignment", _has_env_secret),
    ("credential", "contains a credential, key, token or password", _has_credential),
    ("exfil_carrier", "contains a URL, Markdown link/image, data: URI, base64 blob or HTML", _has_exfil_carrier),
    # IBAN first: its digit groups can pass Luhn by chance.
    ("iban", "contains an IBAN", _has_iban),
    ("payment_card", "contains a payment card number", _has_card),
    ("national_id", "contains a government ID number", _has_national_id),
    ("phone", "contains a phone number", lambda text: any(p.search(text) for p in _PHONE_PATTERNS)),
    ("address", "contains a precise home address", lambda text: any(p.search(text) for p in _ADDRESS_PATTERNS)),
    ("health", "contains health data", _HEALTH_PATTERN.search),
)


def _normalise(text: str) -> str:
    """NFKC, no Cf/fillers, ASCII digits, no glue inside a number spelling."""
    out: list[str] = []
    glue_start = -1
    for char in unicodedata.normalize("NFKC", text):
        category = unicodedata.category(char)
        if category == "Cf" or char in _INVISIBLE_FILLERS:
            continue
        if category == "Nd" and not char.isascii():
            char = str(unicodedata.decimal(char))
        elif category == "Pd" or char == "−":  # en/em dash, minus sign ...
            char = "-"
        elif category in _GLUE_CATEGORIES or char == "⠀":
            if glue_start < 0:
                glue_start = len(out)
            out.append(char)
            continue
        if glue_start >= 0:
            if char in _NUMBER_CONTEXT and glue_start and out[glue_start - 1] in _NUMBER_CONTEXT:
                del out[glue_start:]
            glue_start = -1
        out.append(char)
    return "".join(out)


def _scan(text: str) -> FilterVerdict:
    text = _normalise(text)
    if len(text) > MAX_CANDIDATE_CHARS:  # NFKC can expand a short input
        return FilterVerdict(False, "too_long", f"longer than {MAX_CANDIDATE_CHARS} characters")
    for category, reason, check in _CHECKS:
        if check(text):
            return FilterVerdict(False, category, reason)
    return FilterVerdict(True, None, "no never-stored category matched")


def classify_candidate_text(text: str) -> FilterVerdict:
    """Decide whether a memory candidate may be stored. Fail-closed."""
    try:
        if not isinstance(text, str):
            raise TypeError("memory candidate text must be str")
        if len(text) > MAX_CANDIDATE_CHARS:
            return FilterVerdict(False, "too_long", f"longer than {MAX_CANDIDATE_CHARS} characters")
        return _scan(text)
    except Exception:  # noqa: BLE001 - any failure refuses the candidate (I5)
        return FilterVerdict(False, "filter_error", "filter failed; refused")


def classify_subject_key(key: str) -> FilterVerdict:
    """Like classify_candidate_text for a normalized subject key.

    The key normalizer casefolds and turns punctuation into "-", which hides
    case- and separator-shaped secrets (ghp_..., AKIA..., "Bearer x") from
    the text filter, so the folded-away forms are checked too. Fail-closed.
    ponytail: mixed-case prefixes (AIza...) and words split by stripped
    characters cannot be rebuilt from the key; only the raw key at the
    writer closes that.
    """
    if not isinstance(key, str):
        return classify_candidate_text(key)
    verdict = classify_candidate_text(key)
    for form in (key.replace("-", "_"), key.upper(), key.replace("-", " ")):
        if not verdict.allowed:
            break
        verdict = classify_candidate_text(form)
    return verdict
