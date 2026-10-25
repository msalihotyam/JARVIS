"""Deterministic Turkish lane router (speed plan S2, not wired yet).

route(text) picks the lane before any model call:
  L0  answerable locally (time, date, greeting, thanks, close, cancel, stop)
  L1  short chit-chat or a one-line factual question, no tools
  L2  one-turn task that needs tools, files or live data
  L3  long, multi-step or background project work
Fail safe: when unsure, the higher lane. L0 only when every word of the
message belongs to one local intent; a single leftover content word drops it.
Pure: no network, no model, no I/O, no mutable module state; never executes.
"""

from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple


class Route(NamedTuple):
    lane: str
    intent: str
    confidence: float
    reason: str


def normalize(text: str) -> str:
    """Turkish casefold, strip diacritics and punctuation, collapse spaces.

    "İ"->"i", "I"->"ı" before lower(); then accents are dropped so
    "saat kaç" and "saat kac" normalize the same way.
    """
    t = str(text or "").replace("İ", "i").replace("I", "ı").lower()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).replace("ı", "i")
    t = re.sub(r"['’‘`´]", "", t)  # istanbul'da -> istanbulda
    t = re.sub(r"[\W_]+", " ", t)
    return " ".join(t.split())


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.VERBOSE)


# Words allowed around any L0 intent: fillers, dictation noise, politeness,
# address, question particles, and the small social words themselves.
_SOFT = _rx(r"""
    jarvis|hey|lutfen|acaba|peki|simdi|hemen|artik|bana|ya|yahu|abi|abla|kanka
    |dostum|kardesim|hocam|bi|iste|yani|sey|e+|e?h+m+|a+|i+|o+|evet|tamam(dir)?
    |ok(ey)?|m[iu](s[iu]n(iz)?|y[iu]m|y[iu]z)?|merhaba(lar)?|selam(lar)?|cok
    |tesekkur(ler|ederim)?|sagol(un|asin)?|eyvallah|super|harika|guzel|iyi
""")

# (intent, core pattern on the normalized text, extra words it may contain)
_L0 = (
    ("cancel", _rx(r"^(.*\s)?(dur|durdur|dursana|sus|sussana|kes|yeter)(\s.*)?$"),
     _rx(r"dur|durdur|dursana|sus|sussana|kes|yeter|bir|dakika|saniye|konusma(yi)?")),
    ("cancel", _rx(r"\b(iptal|vazgec\w*|bosver|bos\s+ver|gerek\s+yok)\b"),
     _rx(r"iptal|et|etsene|eder|edin|edebilir\w*|vazgec(tim|tik|elim|iyorum)?|bosver"
         r"|bos|ver|gerek|yok|neyse|bunu|onu|hepsini|islemi|unut")),
    ("close_window", _rx(r"\bkapat"),
     _rx(r"kapat(ir|sana|in|iver\w*|alim|abilir\w*)?|pencere(yi)?|bunu|onu|bu|olur")),
    ("time.now", _rx(r"\bsaat(i|in)?\b"),
     _rx(r"saat(i|in|e)?|kac(tir|ti)?|oldu|olmus|su|an|suan|simdiki|ne|nedir|tam"
         r"|olarak|soyle\w*|soyler|ogren\w*|bak\w*|var")),
    ("date.today", _rx(r"gunlerden|tarih|\bayin\s+kac|\b(hangi|ne)\s+gun|gundeyiz"
                       r"|kacindayiz"),
     _rx(r"bugun(un|ku)?|gunlerden|ne|nedir|tarih(i|ini|imiz)?|kac|kaci|kacinci"
         r"|kacindayiz|ayin|hangi|gun\w*|haftanin|soyle\w*|soyler|ogren\w*"
         r"|su|an|itibariyla")),
    ("thanks", _rx(r"tesekkur|sag\s?ol|eyvallah|\b(tsk|tsks|tskler|eyv|mersi|sagolasin)\b"
                   r"|eline\s+saglik"),
     _rx(r"tesekkur\w*|sag|ol|olun|sagol\w*|eyv|tsk\w*|mersi|ederim|ederiz|eline"
         r"|saglik|bin|kere|varol|varolasin|olasin|olsun")),
    ("greet", _rx(r"gorusuruz|hosca\s?kal|iyi\s+geceler|bay\s+bay|\bbye\b|gule\s+gule"),
     _rx(r"gorusuruz|gorusmek|uzere|hosca|kal|hoscakal|geceler|bay|bye|gule"
         r"|sonra|yarin")),
    ("greet", _rx(r"merhaba|selam|\b(slm|mrb|hey|hello)\b|gunaydin|tunaydin"
                  r"|iyi\s+(aksamlar|gunler|sabahlar)|aleykum"),
     _rx(r"slm|mrb|hello|gunaydin|tunaydin|aksamlar|gunler|sabahlar|selamun"
         r"|aleykum(selam)?|aleyna")),
)

# Verbs that ask for an action with tools. Short stems get explicit suffixes
# (vowel harmony aware) so "acaba", "yapay", "bulut", "silah" stay out;
# negations ("yapma", "kapatma") are not requests and are never L0.
_SFX = (r"(|[aeiu]r|[aeiu]?y?[ae]bil\w*|s[ae]n[ae]|y?[iu]n(iz)?|y?[iu]p"
        r"|m[ae]n[ia]?\w*|m[ae]k|m[ae]m\w*|y?[ae]l[iu]m|y?[iu]ver\w*"
        r"|y?[ae]c[ae]k\w*|d[iu]r\w*|t[iu]r\w*)")
_TASK_VERB = _rx(
    r"(ozetle|olustur|hazirla|duzenle|duzelt|guncelle|kaydet|yukle|gonder|yolla"
    r"|paylas|kopyala|listele|sirala|filtrele|ekle|cevir|donustur|planla|hatirlat"
    r"|ayarla|degistir|temizle|yedekle|klonla|derle|kapat|durdur|baslat|calistir"
    r"|iptal|analiz|karsilastir|hesapla|getir|goster|doldur|yazdir|arastir|tasarla"
    r"|gelistir|kontrol|cikart|sikistir|adlandir|indir(?!im)|isaretle|yayinla"
    r"|kodla|programla|refactor|migrate|otomatiklestir|raporla|ayikla|ayir|kucult"
    r"|buyut|artir|azalt|yukselt|yenile|tercume|kurulum|topla(?!nti)|uyandir)\w*"
    r"|(yap|yaz|kur|sil|bul|oku|ac|ara|tasi|ciz|tara|cal|cek|gecir|kis)" + _SFX
)
# "X et" compounds: "test et", "tercüme et", "kontrol eder misin".
_ET_TASK = _rx(r"\b(test|tercume|transfer|rezerve|siparis|organize|tamir|kayit|teslim"
               r"|takip|optimize|format|deploy|commit|push|merge|install|update"
               r"|not)\s+(et|eder|ed[ie]\w*|etsene|etmek|etmen\w*)\b"
               r"|\bnot\s+al\w*|\bmail\s+at\w*|\bara\s+(?!sira)\w")
# Nouns that need files, apps, the OS or live data. Ambiguous nouns only in
# their object/locative forms ("bilgisayarı kapat", not "bilgisayar nedir").
_TOOL_NOUN = _rx(r"""
    dosya\w*|klasor\w*|dizin\w*|tablo\w*|excel\w*|pdf\w*|word\w*|docx|xlsx|csv\w*
    |json\w*|belge\w*|dokuman\w*|rapor\w*|sunum\w*|slayt\w*|powerpoint\w*|e?mail\w*
    |eposta\w*|posta(mi|lari|larimi|yi)|takvim\w*|toplanti\w*|randevu\w*
    |hatirlatici\w*|alarm\w*|link\w*|url\w*|internet(te|ten|i)|web\w*|google\w*
    |tarayici\w*|chrome\w*|sekme\w*|spotify\w*|youtube\w*|muzi\w*|sarki(yi|lari)
    |playlist\w*|video\w*|fotograf\w*|foto(lar\w*|yu|yi|da|dan)?|resim\w*|gorsel\w*|github\w*|issue\w*
    |script\w*|betik\w*|bilgisayar(i|imi|da|daki|imda|imdaki)|ekran(i|goruntusu)?
    |goruntusu|pencere(yi|leri)|uygulama\w*|mesaj\w*|whatsapp\w*|telegram\w*|sms\w*
    |hava(si|lar|yi|da)?|havadurumu|dolar\w*|euro\w*|borsa\w*|bitcoin\w*|kripto\w*
    |haber(ler\w*|i|in)|skor\w*|trafik\w*|masaustu\w*|indirilenler\w*|ses(i|ini)|parlakli\w*
    |wifi\w*|bluetooth\w*|kod(u|unu|umu|da|daki|lari|larini|um)|fonksiyon\w*
    |test(leri|lerini|i)|notlar\w*|yedek\w*|eczane\w*|tarif\w*|cv\w*|hesap(tan|ima|imdan)
""")
# L3: project nouns + build verbs, rewrite markers, background/recurring work.
_PROJECT = _rx(r"""
    proje\w*|\w*repo\w*|depo(yu|nun|daki|da)|uygulama\w*|app\w*|\w*site\w*|sistem\w*
    |altyapi\w*|oyun(u|unu)?|backend\w*|frontend\w*|api\w*|servis\w*|bot(u|unu)?
    |eklenti\w*|kutuphane\w*|mimari\w*|codebase\w*|veritabani\w*|platform\w*
    |otomasyon\w*|pipeline\w*|sunucu\w*|crm\w*|erp\w*
""")
_BUILD = _rx(r"(kur|yap|yaz|tasi|gecir)" + _SFX +
             r"|(gelistir|olustur|tasarla|insa|kodla|programla|refactor|migrate"
             r"|donustur|kurulum)\w*")
_L3_ALONE = _rx(r"(gelistir|refactor|migrate|migrasyon|otomatiklestir)\w*")
_L3_PHRASE = re.compile(
    r"\b(bastan|sifirdan|bastanbasa|komple|tamamen|yeniden|uctan\s+uca)\b"
    r"|\barka\s+planda\b|\bbitince\s+haber|\bkod\s+taban|\bweb\s+sayfa|\blanding\s+page"
    r"|\belden\s+gecir")
_RECURRING = re.compile(r"\bher\s+(gun|sabah|aksam|gece|hafta|ay|saat|pazartesi|cuma)\w*")
_ALL = re.compile(r"\b(tum|butun|hepsini|her\s+bir)\b")
_SEQUENCE = re.compile(r"\b(sonra|ardindan|akabinde|sonrasinda|once|en\s+son|daha\s+sonra)\b")
# Request markers: "yapar mısın", "-abilir", "-sana", "lütfen".
_REQUEST = _rx(r"\w{2,}([ae]bil(ir|ecek)\w*|s[ae]n[ae])|lutfen")
_REQ_PARTICLE = _rx(r"m[iu]s[iu]n(iz)?")
_NOT_REQUEST = _rx(r"olabil\w*|ol[ae]bil\w*")
# Conversational verbs: a request with one of these is still one-line chat.
_CHAT_VERB = _rx(r"(anlat|soyle|acikla|tanimla|oner|tavsiye|sor|cevapla|yorumla|dusun"
                 r"|bil|tahmin|ver|konus|sohbet|espri|fikra|sev|say|yardim)\w*")
# "bir espri yap" / "fıkra anlat": a made-up line, not a tool task.
_CHAT_OBJ = _rx(r"(espri|fikra|saka|bilmece|tekerleme)\w*")
_MAKE = _rx(r"(yap|soyle)\w*")
_QUESTION = _rx(r"ne|nedir|neden|nicin|niye|nasil|kim|kimdir|nerede|neresi|nereye"
                r"|hangi\w*|kac\w*|m[iu]\w*|ne\w+")

_LONG = 35


def _l0(tokens: list[str], text: str) -> str | None:
    for intent, core, extra in _L0:
        if core.search(text) and all(
            _SOFT.fullmatch(t) or extra.fullmatch(t) for t in tokens
        ):
            return intent
    return None


def _is_request(tokens: list[str]) -> bool:
    for i, t in enumerate(tokens):
        if _NOT_REQUEST.fullmatch(t):
            continue
        if _REQUEST.fullmatch(t):
            return True
        if i and _REQ_PARTICLE.fullmatch(t) and tokens[i - 1].endswith("r"):
            return True
    return False


def route(text: str) -> Route:
    norm = normalize(text)
    tokens = norm.split()
    if not tokens:
        return Route("L1", "chat", 0.5, "empty")
    intent = _l0(tokens, norm)
    if intent and len(tokens) <= 12:
        return Route("L0", intent, 0.95, "l0:" + intent)

    verbs = [t for t in tokens if _TASK_VERB.fullmatch(t)]
    verb_count = len(verbs) + len(_ET_TASK.findall(norm))
    objects = [t for t in tokens if _TOOL_NOUN.fullmatch(t)]
    task = bool(verb_count or objects)
    build = any(_BUILD.fullmatch(t) for t in tokens)

    if any(_L3_ALONE.fullmatch(t) for t in tokens):
        return Route("L3", "project", 0.85, "l3:develop/refactor/migrate verb")
    if build and any(_PROJECT.fullmatch(t) for t in tokens):
        return Route("L3", "project", 0.85, "l3:project noun + build verb")
    if task and _L3_PHRASE.search(norm) and (build or verb_count):
        return Route("L3", "project", 0.8, "l3:rewrite/background marker")
    if task and _RECURRING.search(norm):
        return Route("L3", "background", 0.75, "l3:recurring task")
    if task and _ALL.search(norm) and (objects or verb_count >= 2):
        return Route("L3", "project", 0.7, "l3:bulk work")
    if verb_count >= 3 or (verb_count >= 2 and _SEQUENCE.search(norm)):
        return Route("L3", "project", 0.7, "l3:multi-step")
    if task and len(tokens) > _LONG:
        return Route("L3", "project", 0.6, "l3:long task")

    if (verb_count == 1 and not objects and verbs and _MAKE.fullmatch(verbs[0])
            and any(_CHAT_OBJ.fullmatch(t) for t in tokens)):
        return Route("L1", "chat", 0.6, "l1:joke/riddle request")
    if task:
        why = "verb" if verb_count else "tool/live-data noun"
        return Route("L2", "task", 0.8, "l2:" + why)
    chat_verb = any(_CHAT_VERB.fullmatch(t) for t in tokens)
    if _is_request(tokens) and not chat_verb:
        return Route("L2", "task", 0.55, "l2:request without known verb")
    if len(tokens) > _LONG:
        return Route("L2", "task", 0.5, "l2:long message")
    if intent:  # L0 words but too long to trust
        return Route("L1", "chat", 0.6, "l1:long l0-like")
    is_q = any(_QUESTION.fullmatch(t) for t in tokens)
    return Route("L1", "question" if is_q else "chat", 0.6, "l1:no task signal")
