"""Deterministic Turkish lane router (speed plan S2, not wired yet).

route(text) picks the lane before any model call:
  L0  answerable locally (time, date, greeting, thanks, close, cancel, stop)
  L1  short chit-chat or a one-line factual question, no tools
  L2  one-turn task that needs tools, files or live data
  L3  long, multi-step or background project work
Fail safe: when unsure, the higher lane. L0 only when every word of the
message belongs to one local intent (explicit word lists, each content word
once, so a negation like "kapatma", a modal question or any second clause
drops it). L1 only on positive evidence: every word is known vocabulary
(exact forms: chat words, question words, a small factual vocabulary) and
the message does not ask about the present moment; anything else is L2.
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


_APOSTROPHE = re.compile(
    "['`\u00b4\u02b9\u02bb\u02bc\u02bd\u02c8\u02ca\u02cb"
    "\u2018\u2019\u201b\u2032\u2035\uff07]")


def normalize(text: str) -> str:
    """Turkish casefold, strip diacritics and punctuation, collapse spaces.

    "İ"->"i", "I"->"ı" before lower(); then accents are dropped so
    "saat kaç" and "saat kac" normalize the same way.
    """
    t = str(text or "").replace("İ", "i").replace("I", "ı").lower()
    # Before NFKD: it turns "´" into a space plus a combining accent.
    t = _APOSTROPHE.sub("", t)  # istanbul'da / istanbul´da -> istanbulda
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).replace("ı", "i")
    t = re.sub(r"[\W_]+", " ", t)
    return " ".join(t.split())


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.VERBOSE)


# Words allowed around any L0 intent: fillers, dictation noise, politeness,
# address, question particles, and the small social words themselves.
_SOFT = _rx(r"""
    jarvis|hey|lutfen|acaba|peki|simdi|hemen|artik|bana|ya|yahu|abi|abla|kanka
    |dostum|kardesim|hocam|bi|iste|yani|sey|e+|e?h+m+|a+|i+|evet|tamam(dir)?
    |ok(ey)?|m[iu](s[iu]n(iz)?)?|merhaba(lar)?|selam(lar)?|cok
    |tesekkur(ler|ederim)?|sagol(un|asin)?|eyvallah|super|harika|guzel|iyi
""")

# State-changing L0 intents: only these whole utterances, after normalization
# and one optional leading "jarvis" / trailing "lutfen". Nothing else is added.
_L0_ACTION = {"iptal": "cancel", "iptal et": "cancel", "vazgec": "cancel",
              "dur": "cancel", "durdur": "cancel", "kapat": "close_window",
              "tamam kapat": "close_window", "pencereyi kapat": "close_window"}

# Read-only L0 intents: (intent, core pattern on the normalized text, extra
# words it may contain)
_L0 = (
    ("time.now", _rx(r"\bsaat(i|in)?\b"),
     _rx(r"saat(i|e)?|kac(tir)?|oldu|olmus|su|an|suan|simdiki|ne|nedir|tam|olarak"
         r"|soyle|soyler|soylesene|soyleyebilir|soyleyebilirsin|soyleyebilirmisin"
         r"|ogrenebilir|ogrenebilirmiyim|bak|bakar|baksana|bakabilir|bakabilirsin")),
    ("date.today", _rx(r"gunlerden|tarih|\bayin\s+kac|\b(hangi|ne)\s+gun|gundeyiz"
                       r"|kacindayiz"),
     _rx(r"bugun(un|ku)?|gunlerden|ne|nedir|tarih(i|ini|imiz)?|kac|kaci|kacinci"
         r"|kacindayiz|ayin|hangi|gun(u|un|e|deyiz|undeyiz)?|haftanin|soyle|soyler"
         r"|soylesene|soyleyebilir|soyleyebilirsin|ogrenebilir|su|an|itibariyla")),
    ("thanks", _rx(r"tesekkur|sag\s?ol|eyvallah|\b(tsk|tsks|tskler|eyv|mersi|sagolasin)\b"
                   r"|eline\s+saglik"),
     _rx(r"tesekkur(ler|ederim|ederiz)?|sag|ol|olun|sagol(un|asin|dun)?|eyv|tsk(ler|s)?"
         r"|mersi|ederim|ederiz|eline"
         r"|saglik|bin|kere|varol|varolasin|olasin|olsun")),
    ("greet", _rx(r"gorusuruz|hosca\s?kal|iyi\s+geceler|bay\s+bay|\bbye\b|gule\s+gule"),
     _rx(r"gorusuruz|gorusmek|uzere|hosca|kal|hoscakal|geceler|bay|bye|gule"
         r"|sonra|yarin")),
    ("greet", _rx(r"merhaba|selam|\b(slm|mrb|hey|hello)\b|gunaydin|tunaydin"
                  r"|iyi\s+(aksamlar|gunler|sabahlar)|aleykum"),
     _rx(r"slm|mrb|hello|gunaydin|tunaydin|aksamlar|gunler|sabahlar|selamun"
         r"|aleykum(selam)?|aleyna")),
)
# The task verbs / tool nouns an L0 intent owns; any other one refuses L0
# ("tarihi güncelle", "saati ayarla" are tasks).
_L0_OWN = {"time.now": _rx(r"bak|bakar|baksana|bakabilir|bakabilirsin")}

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
    r"|buyut|artir|azalt|yukselt|yenile|tercume|kurulum|topla(?!nti)|uyandir"
    r"|kilitle|oynat|sustur|tikla|kaydir|uyut|incele|cagir|sondur)\w*"
    r"|(yap|yaz|kur|sil|bul|oku|ac|ara|tasi|ciz|tara|cal|cek|gecir|kis|uyar|bak|ode|al)"
    + _SFX
)
# "X et" compounds: "test et", "tercüme et", "kontrol eder misin".
_ET_TASK = _rx(r"\b(test|tercume|transfer|rezerve|siparis|organize|tamir|kayit|teslim"
               r"|takip|optimize|format|deploy|commit|push|merge|install|update"
               r"|not|senkronize)\s+(et|eder|ed[ie]\w*|etsene|etmek|etmen\w*)\b"
               r"|\bsiparis\s+ver\w*"
               r"|\bnot\s+al\w*|\bmail\s+at\w*|\bara\s+(?!sira)\w")
# Nouns that need files, apps, the OS or live data. Ambiguous nouns only in
# their object/locative forms ("bilgisayarı kapat", not "bilgisayar nedir").
_TOOL_NOUN = _rx(r"""
    dosya\w*|klasor\w*|dizin\w*|tablo\w*|rezervasyon\w*|excel\w*|pdf\w*|word\w*|docx|xlsx|csv\w*
    |json\w*|belge\w*|dokuman\w*|rapor\w*|sunum\w*|slayt\w*|powerpoint\w*|e?mail\w*
    |eposta\w*|posta(mi|lari|larimi|yi)|takvim\w*|toplanti\w*|randevu\w*
    |hatirlatici\w*|alarm\w*|link\w*|url\w*|internet\w*|web\w*|google\w*
    |tarayici\w*|chrome\w*|sekme\w*|spotify\w*|youtube\w*|muzi\w*|sarki(yi|lari)
    |playlist\w*|video\w*|fotograf\w*|foto(lar\w*|yu|yi|da|dan)?|resim\w*|gorsel\w*|github\w*|issue\w*
    |script\w*|betik\w*|bilgisayar(i|imi|da|daki|imda|imdaki)|ekran(i|goruntusu)?
    |goruntusu|pencere(yi|leri)|uygulama\w*|mesaj\w*|whatsapp\w*|telegram\w*|sms\w*
    |hava(si|lar|yi|da)?|havadurumu|dolar\w*|euro\w*|borsa\w*|bitcoin\w*|kripto\w*
    |haber(ler\w*|i|in)|skor\w*|trafik\w*|masaustu\w*|indirilenler\w*|ses(|i|in|ini)|parlakli\w*
    |wifi\w*|bluetooth\w*|kod(u|unu|umu|da|daki|lari|larini|um)|fonksiyon\w*
    |test(leri|lerini|i)|notlar\w*|yedek\w*|eczane\w*|tarif\w*|cv\w*|hesap(tan|ima|imdan)
    |fiyat\w*|oturum(u|unu|dan)|ozet\w*|sterlin\w*|yagmur\w*|ezan\w*|benzinlik\w*
    |pil(im|i|in)?|baglanti(m|mi|si|yi)\w*|mac(i|in|lar\w*)?|altin\w*|gumus\w*
    |fatura\w*|bakiye\w*|cpu\w*|gpu\w*|ram(i|im|imi|in)|islemci(m|mi|nin|yi)
    |bellek(im|imi|i)|disk(im|imi|i|in|te)|sicakli\w*|telemetri\w*|batarya\w*|sarj\w*
    |fan(i|in|lar\w*)?|disari(si|da)|bilgisayar(im|imin|imiz|in)|benzin\w*|mazot\w*
    |deprem\w*|gundem\w*|iftar\w*|sahur\w*|yol(u|lar|da)?
""")
# Live prices: "altın kaç lira", "gram altın ne kadar", "kaç TL".
_LIVE = re.compile(r"\b(kac|ne\s+kadar)\s+(lira|tl|kurus|dolar|euro)\b"
                   r"|\b(altin|gumus|benzin|mazot|motorin|akaryakit|faiz)\w*\s+"
                   r"(\w+\s+)?(ne\s+kadar|kac\b|fiyat)"
                   r"|\byag(ar|acak|iyor)\b|\bgun\s+batim|\bsistem\s+saat|\ben\s+yakin\b"
                   r"|\bip\s+adres|\bkaca\b|\bkac\s+kac\b"
                   # "kaç derece" is the weather unless it is a fixed point;
                   # "(bir) kahve kaç / ne kadar" (one bare noun) asks a price.
                   r"|(?<!noktasi\s)\bkac\s+derece\b"
                   r"|^(bir\s+)?(?!saat\b)\w+\s+(kac(tir)?|ne\s+kadar)$")
# L3: project nouns + build verbs, rewrite markers, background/recurring work.
_PROJECT = _rx(r"""
    proje\w*|\w*repo\w*|depo(yu|nun|daki|da)|uygulama\w*|app\w*|(web)?site\w*|sistem\w*
    |altyapi\w*|oyun(u|unu)?|backend\w*|frontend\w*|api\w*|servis\w*|bot(u|unu)?
    |eklenti\w*|kutuphane\w*|mimari\w*|codebase\w*|veritabani\w*|platform\w*
    |otomasyon\w*|pipeline\w*|sunucu\w*|crm\w*|erp\w*|chatbot\w*
""")
_BUILD = _rx(r"(kur|yap|yaz|tasi|gecir)" + _SFX +
             r"|(gelistir|olustur|tasarla|insa|kodla|programla|refactor|migrate"
             r"|donustur|kurulum)\w*")
_L3_ALONE = _rx(r"(gelistir|refactor|migrate|migrasyon|otomatiklestir)\w*")
_L3_PHRASE = re.compile(
    r"\b(bastan|sifirdan|bastanbasa|komple|tamamen|yeniden(?!\s+basla)|uctan\s+uca)\b"
    r"|\barka\s+planda\b|\bbitince\s+haber|\bkod\s+taban|\bweb\s+sayfa|\blanding\s+page"
    r"|\belden\s+gecir")
_RECURRING = re.compile(r"\bher\s+(gun|sabah|aksam|gece|hafta|ay|saat|pazartesi|sali"
                        r"|carsamba|persembe|cuma|cumartesi|pazar)\w*"
                        r"|\bher\s+\w+\s+(saatte|gunde|haftada)\b|\bhergun\w*"
                        r"|\b(haftada|gunde|ayda)\s+(bir|iki|uc|\d+)\b|\bsaat\s+basi\b"
                        r"|\b(pazartesi|sali|carsamba|persembe|cuma|cumartesi|pazar)(leri|lari)\b"
                        r"|\bsurekli\b")
_ALL = re.compile(r"\b(tum|butun|hepsini|her\s+bir)\b")
_SEQUENCE = re.compile(r"\b(sonra|ardindan|akabinde|sonrasinda|once|en\s+son|daha\s+sonra)\b")
# Request markers: "yapar mısın", "-abilir", "-sana", "lütfen".
_REQUEST = _rx(r"\w{2,}([ae]bil(ir|ecek)\w*|s[ae]n[ae])|lutfen")
_REQ_PARTICLE = _rx(r"m[iu]s[iu]n(iz)?")
# "-r mı / -ir misin / -acak mısın" asks me to act ("ısmarlayacak mısın").
_VERB_ASKED = _rx(r"\w+([aeiu]?r|[ae]c[ae]k)")
_NOT_REQUEST = _rx(r"olabil\w*|ol[ae]bil\w*|olur|var|\w+yor")
# Negated orders and refusals: "konuşmayın", "yapmasın", "istemiyorum".
_NEG = _rx(r"istem(iyor|em|ez)\w*|\w+m[ae]y[iu]n(iz)?|\w+m[ae]s[iu]n")
# "X istiyorum / lazım / gerek" with a project noun is project work;
# "oyun oynamak istiyorum" (an infinitive) is the speaker's own wish.
_WANT = _rx(r"isti(yorum|yoruz|yom)|isterim|isteriz|lazim|gerek(li|iyor)?|ihtiyacim")
_INFINITIVE = _rx(r"\w+m[ae]k")
# Denominal "-lA" imperative after an object ("oturumu kilitle"); not these.
_IMPERATIVE = _rx(r"\w{3,}l[ae]")
_NOT_IMPERATIVE = _rx(r"(b|s|o|bo|so|oy|boy|soy)le|ile|bile|hele|\w*(ce|ca)le")
# Conversational verbs: a request made with one of these is one-line chat.
# Exact forms only: "konuşlandırır" is not "konuş", "kaçırma" not "kaç".
_CV_SFX = (r"(|[aeiu]?r|[aeiu]?rm[iu]s[iu]n|s[ae]n[ae]|y?[iu]n(iz)?|y?[ae]bilir(sin|misin)?"
           r"|y?[ae]c[ae]k|[iu]?yor(sun|um)?)")
_CHAT_VERB = _rx(r"(anlat|soyle|acikla|tanimla|oner|sor|cevapla|yorumla|dusun|bil|ver"
                 r"|konus|sev|say)" + _CV_SFX
                 + r"|espri|fikra|sohbet|tavsiye|tahmin|yardim")
_OBJ_SFX = r"(|i|si|yi|u|yu|ler|lar|leri|lari)"
# "bir espri yap" / "fıkra anlat": a made-up line, not a tool task.
_CHAT_OBJ = _rx(r"(espri|fikra|saka|bilmece|tekerleme)" + _OBJ_SFX)
_MAKE = _rx(r"(yap|soyle)(|[ae]?r|s[ae]n[ae]|y?[iu]n|y?[ae]bilir(sin)?|y?[iu]ver)")
# Fail upward: L1 needs a recognised chat signal, anything else is L2.
_ASK = _rx(r"ne|nedir|neden|neler|neyi|neye|nesi|neyin|nicin|niye|nasil|nasilsin"
           r"|nasilsiniz|kim|kimdir|kimsin|kimin|kime|kimi|nere|nerede|neresi|nereden"
           r"|nereye|hangi|hangisi|kac|kactir|m[iu](s[iu]n(iz)?|y[iu]m|y[iu]z|d[iu]r|yd[iu])?")
# Acknowledgements are chat only on their own: "tamam bitir" is an order.
_ACK = _rx(r"naber|nbr|merhaba|selam(lar)?|tamam|peki|evet|hayir")
_SOCIAL = _rx(r"tesekkur(ler|ederim)?|sagol(un|asin)?|harika|super|guzel|anladim|anladik"
              r"|anlamadim|yorgun(um|uz)?|sikildim|sikiliyor(um)?|sikici|hissediyorum"
              r"|mutlu(yum)?|uzgun(um)?|sence|bence")
# "devam edelim mi", "silsem mi": a proposal to act, not a question.
_PROPOSAL = _rx(r"\w+[ae]lim|\w+s[ae]m|\w+[ae]y[iu]m")
# Mixed messages are routed per clause: "nasılsın, bir de pr at" is work.
# Clause breaks, including the converb: "bir şaka yapıp kapıyı kapa".
_CLAUSE = re.compile(r"[,;?!.]+|\b(?:ve|sonra|ayrica|bir de)\b|(?<=\w[iu]p)\s")
# "sor/söyle/ver/say" alone are orders or relays ("bana pizza söyle",
# "ahmete sor", "söyler misin", "sorabilir misin"); with a chat object
# ("bir söz söyle") they are chat.
_WEAK_CHAT = _rx(r"(sor|soyle|ver|say)(|sana|sene|[iu]n(iz)?|[ae]?r|[iu]r|y?[ae]bil\w*"
                 r"|y?[ae]c[ae]k\w*)")
_TALK_OBJ = _rx(r"(espri|fikra|saka|bilmece|tekerleme|soz|tavsiye|fikir|oneri"
                r"|bilgi|cevap|yorum|motivasyon|ornek)" + _OBJ_SFX + r"|fikri|fikrin|cevabi")
_WISH = _rx(r"isti(yorum|yoruz|yom)|isterim|isteriz")
# The speaker's own wish ("oyun oynamak istiyorum"), exact infinitives only.
_OWN_WISH = _rx(r"gitmek|oynamak|uyumak|dinlenmek|konusmak|gezmek|yurumek|kosmak"
                r"|yemek|icmek|ogrenmek|eglenmek")
_POINTED = _rx(r"(bu|su|o)(n[iu]|nlari)")
_ACCUSATIVE = _rx(r"\w+[iu]")
_STRONG_CHAT = _rx(r"(anlat|acikla|tanimla|oner|cevapla|yorumla|dusun|konus)" + _CV_SFX
                   + r"|tavsiye|tahmin|sohbet|espri|fikra")

# Positive evidence for L1: small function words that are neither content
# nor a task ("ben de", "biraz daha", "kısaca").
_L1_WORD = _rx(r"""
    bugun|canim|kendim(i|e|den)?|ben|beni|bana|benim|sen|seni|sana|senin|biz|siz|o
    |bu|su|an|bunlar|bir|en|az|daha|biraz|ama|fakat|ve|ile|gibi|icin|kadar|zaman
    |demek|fark|saniye|dakika|dur|kisaca|boyle|oyle|gercekten|var|yok|olur|ileri
    |geri|uzun|kisa|kotu|yapiyorsun|dersin|\d+
""")
# A small closed factual vocabulary; any other noun is unknown, so L2.
_FACT = _rx(r"""
    turkiyenin|baskenti|everestin|yuksekligi|pi|sayisi|isik|hizi|fotosentez|python
    |ataturk|dogdu|kuantum|bilgisayar|yilda|gun|gunde|saat|carpi|eder|sevdigin|renk
    |adin|rekursiyon|ingilizcede|elma|dunyanin|nehri|ayda|yercekimi|yapay|zeka
    |tehlikeli|kahve|cay|istanbulun|nufusu|mona|lisayi|yapti|suyun|kaynama|noktasi
    |derece|http|https|arasindaki|film|dilimi|saatler|aliniyor
""")
_KNOWN = (_SOFT, _L1_WORD, _ASK, _SOCIAL, _ACK, _TALK_OBJ, _CHAT_VERB, _WISH, _FACT)
# Questions about the present moment need live data ("X şu an ne kadar");
# "... ne dersin / olur mu" after an order is a proposal to act.
_NOW = re.compile(r"\b(su\s+an|suan|simdi\w*|anlik|guncel\w*|bugun\w*|yarin\w*|canli"
                  r"|son\s+durum|bu\s+(gun|dakika|saat)\w*)\b")
_PROPOSE_TAIL = re.compile(r"\b(ne\s+dersin|olur\s+mu|olmaz\s+mi|nasil\s+olur|tamam\s+mi)$")
# Bare "saat kaç" is L0; any other content around it ("tokyoda") needs a tool.
_CLOCK = re.compile(r"\bsaat\s+kac\b")
# A bare yes/no particle makes an order a question ("yeter mi", "kapat mı").
_YESNO = _rx(r"m[iu]")

_LONG = 35


def _l0(tokens: list[str], text: str) -> str | None:
    action = tokens[1:] if tokens[:1] == ["jarvis"] else tokens
    action = action[:-1] if action[-1:] == ["lutfen"] else action
    if " ".join(action) in _L0_ACTION:
        return _L0_ACTION[" ".join(action)]
    if _LIVE.search(text) or _ET_TASK.search(text):
        return None
    once = [t for i, t in enumerate(tokens) if not i or t != tokens[i - 1]]
    content = [t for t in once if not _SOFT.fullmatch(t)]
    if len(content) != len(set(content)):
        return None
    for intent, core, extra in _L0:
        own = _L0_OWN.get(intent)
        if core.search(text) and all(
            (_SOFT.fullmatch(t) or extra.fullmatch(t))
            and not ((_TASK_VERB.fullmatch(t) or _TOOL_NOUN.fullmatch(t))
                     and not (own and own.fullmatch(t)))
            for t in tokens
        ):
            return intent
    return None


def _known(t: str) -> bool:
    return any(p.fullmatch(t) for p in _KNOWN)


def _requests(tokens: list[str]) -> list[str]:
    """Words carrying a request marker ("yapabilir", "yapar" of "yapar mısın")."""
    found = []
    for i, t in enumerate(tokens):
        if _NOT_REQUEST.fullmatch(t):
            continue
        if _REQUEST.fullmatch(t):
            found.append(t)
        elif (i and (_REQ_PARTICLE.fullmatch(t) or _YESNO.fullmatch(t))
              and _VERB_ASKED.fullmatch(tokens[i - 1])
              and not _NOT_REQUEST.fullmatch(tokens[i - 1])):
            found.append(tokens[i - 1])
    return found


def _imperative(tokens: list[str]) -> bool:
    last = tokens[-1]
    return (len(tokens) >= 2 and tokens[-2][-1] in "iu"
            and bool(_IMPERATIVE.fullmatch(last))
            and not _NOT_IMPERATIVE.fullmatch(last) and not _CHAT_VERB.fullmatch(last))


def _route(norm: str) -> Route:
    norm = re.sub(r"\bne haber\b", "naber", norm)
    tokens = norm.split()
    if not tokens:
        return Route("L1", "chat", 0.5, "empty")
    intent = _l0(tokens, norm)
    if intent and len(tokens) <= 12:
        return Route("L0", intent, 0.95, "l0:" + intent)

    verbs = [t for t in tokens if _TASK_VERB.fullmatch(t)]
    verb_count = len(verbs) + len(_ET_TASK.findall(norm))
    objects = [t for t in tokens if _TOOL_NOUN.fullmatch(t)]
    task = bool(verb_count or objects or _LIVE.search(norm))
    build = any(_BUILD.fullmatch(t) for t in tokens)
    project = any(_PROJECT.fullmatch(t) for t in tokens)

    if any(_L3_ALONE.fullmatch(t) for t in tokens):
        return Route("L3", "project", 0.85, "l3:develop/refactor/migrate verb")
    if build and project:
        return Route("L3", "project", 0.85, "l3:project noun + build verb")
    if (project and any(_WANT.fullmatch(t) for t in tokens)
            and not any(_INFINITIVE.fullmatch(t) for t in tokens)):
        return Route("L3", "project", 0.75, "l3:project noun + want")
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

    # Negation and task checks come before any joke/social exception.
    if any(_NEG.fullmatch(t) for t in tokens):
        return Route("L2", "task", 0.55, "l2:negated order")
    if (verb_count == 1 and not objects and verbs and not _LIVE.search(norm)
            and _MAKE.fullmatch(verbs[0])
            and any(_CHAT_OBJ.fullmatch(t) for t in tokens)
            and all(_known(t) for t in tokens if t != verbs[0])):
        return Route("L1", "chat", 0.6, "l1:joke/riddle request")
    if task:
        why = "verb" if verb_count else "tool/live-data noun"
        return Route("L2", "task", 0.8, "l2:" + why)
    # A chat verb only makes its own request chat: "anlatır mısın" is L1,
    # "kilitleyebilir misin ve sonucu söyle" is still a request.
    if any(not _CHAT_VERB.fullmatch(t) for t in _requests(tokens)):
        return Route("L2", "task", 0.55, "l2:request without known verb")
    if _imperative(tokens):
        return Route("L2", "task", 0.5, "l2:imperative -lA verb")
    if len(tokens) > _LONG:
        return Route("L2", "task", 0.5, "l2:long message")
    if intent:  # L0 words but too long to trust
        return Route("L1", "chat", 0.6, "l1:long l0-like")
    talk = any(_TALK_OBJ.fullmatch(t) for t in tokens)
    if any(_WEAK_CHAT.fullmatch(t) for t in tokens) and not talk:
        return Route("L2", "task", 0.5, "l2:order/relay verb")
    strong = any(_STRONG_CHAT.fullmatch(t) for t in tokens)
    # A pointed object ("şunu hallet", "tamam bunu hallet", "bunu nasıl
    # hallederim") is work unless the verb is a chat verb ("bunu açıkla").
    if any(_POINTED.fullmatch(t) for t in tokens) and not (talk or strong):
        return Route("L2", "task", 0.5, "l2:pointed object")
    is_q = any(_ASK.fullmatch(t) for t in tokens)
    # Own wish ("oyun oynamak istiyorum"); "lazım" or an accusative object
    # ("ortalığı toparlamak istiyorum") asks for work.
    wish = (any(_WISH.fullmatch(t) for t in tokens)
            and any(_OWN_WISH.fullmatch(t) for t in tokens)
            and not any(_ACCUSATIVE.fullmatch(t) for t in tokens))
    if any(_PROPOSAL.fullmatch(t) for t in tokens):
        return Route("L2", "task", 0.5, "l2:proposal to act")
    social = (any(_SOCIAL.fullmatch(t) for t in tokens)
              or all(_ACK.fullmatch(t) for t in tokens))
    if all(_known(t) for t in tokens):
        # "bugün ne var", "bugün bilgi ver": a question or request about the
        # present moment needs a tool; "bugün nasılsın/yorgunum" is chat.
        live = _NOW.search(norm) and (talk or any(
            _CHAT_VERB.fullmatch(t) or (_ASK.fullmatch(t) and t not in ("nasilsin", "nasilsiniz"))
            for t in tokens))
        if live or _PROPOSE_TAIL.search(norm) or _CLOCK.search(norm):
            return Route("L2", "task", 0.5, "l2:live data or proposal")
        if is_q or talk or wish or strong or social:
            return Route("L1", "question" if is_q else "chat", 0.6, "l1:chat signal")
        return Route("L2", "task", 0.5, "l2:no chat signal")
    if wish:
        return Route("L1", "chat", 0.6, "l1:own wish")
    return Route("L2", "task", 0.5, "l2:unrecognised content")


def route(text: str) -> Route:
    found = _clauses(text)
    # "Her salı çiçekleri sula": a recurring order is background work.
    if found.lane == "L2" and _RECURRING.search(normalize(text)):
        return Route("L3", "background", 0.75, "l3:recurring task")
    return found


def _clauses(text: str) -> Route:
    whole = _route(normalize(text))
    if whole.lane != "L1":  # L0 already refuses any task word
        return whole
    # A bare "jarvis," is the address, not a clause.
    clauses = [c for part in re.split(r"[,;?!.]+", str(text or ""))
               for c in _CLAUSE.split(normalize(part)) if c.strip() not in ("", "jarvis")]
    if len(clauses) < 2:
        return whole
    worst = max((_route(c.strip()) for c in clauses), key=lambda r: r.lane)
    return worst if worst.lane in ("L2", "L3") else whole
