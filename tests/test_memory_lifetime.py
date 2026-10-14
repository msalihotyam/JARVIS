import unittest
from datetime import datetime, timedelta, timezone

from jarvis.memory_lifetime import (
    PHASE_TTL,
    SESSION_TTL,
    Lifetime,
    decide_lifetime,
    expires_for,
    is_phase,
    is_transient,
)

TRANSIENT = (
    "Bugün evden çalışıyorum",
    "BUGÜN toplantı yok",
    "bugun kod yazma",  # typed without Turkish letters
    "Bu akşam erken çıkacağım",
    "Şimdilik testleri atla",
    "Geçici olarak dark mode kullan",
    "Bu oturumda kısa cevap ver",
    "bu oturum boyunca",
    "Bu sohbette İngilizce konuş",
    "ŞU AN meşgulüm",
    "şu anlık böyle kalsın",
    "Şu  an\tyorgunum",  # any whitespace run between the words
    "Use tabs for now",
    "Today I work from home",
    "TONIGHT is free",
    "In this session answer briefly",
    "Only in this chat",
    "Temporarily skip lint",
    "I am busy right now",
)
PHASE = (
    "Bu sprint boyunca sadece hata düzelt",
    "Bu sprintte UI yok",
    "BU AŞAMADA şema değişmez",
    "bu aşama için mock kullan",
    "Bu milestone kapsamında",
    "Sürüme kadar API dondurulmuş",
    "This sprint we freeze the API",
    "In this phase no refactors",
    "For this milestone use mocks",
    "Keep it until release",
    "Keep it until the release",
)
PERMANENT = (
    "Bugünkü toplantı iptal",  # 'today's': suffixed form is not the marker
    "Kullanıcı Python tercih eder",
    "IŞIK temasını sever",  # dotless/dotted I fold, no marker
    "Prefers todays-style headlines",  # 'todays' is not 'today'
    "this chatbot is called JARVIS",
    "Released until further notice",
    "bu oturumlar uzun sürer",  # 'oturumlar' is not 'oturum(da)'
    "sprint planning happens weekly",
    "",
)


class MarkerTableTests(unittest.TestCase):
    def test_transient_markers(self):
        for text in TRANSIENT:
            with self.subTest(text=text):
                self.assertTrue(is_transient(text))
                self.assertIs(decide_lifetime(text), Lifetime.SESSION)

    def test_phase_markers(self):
        for text in PHASE:
            with self.subTest(text=text):
                self.assertFalse(is_transient(text))
                self.assertTrue(is_phase(text))
                self.assertIs(decide_lifetime(text), Lifetime.PHASE)

    def test_no_marker_is_permanent(self):
        for text in PERMANENT:
            with self.subTest(text=text):
                self.assertFalse(is_transient(text))
                self.assertFalse(is_phase(text))
                self.assertIs(decide_lifetime(text), Lifetime.PERMANENT)

    def test_dotted_capital_i_folds(self):
        self.assertTrue(is_transient("GEÇİCİ OLARAK kapat"))
        self.assertTrue(is_transient("Geçıcı olarak kapat"))
        self.assertTrue(is_phase("BU AŞAMA İÇİN"))

    def test_transient_wins_over_phase(self):
        self.assertIs(decide_lifetime("Bu sprint, şimdilik mock kullan"), Lifetime.SESSION)


class RequestedTests(unittest.TestCase):
    def test_request_never_raises_transient(self):
        for requested in (Lifetime.PERMANENT, Lifetime.PHASE, "permanent"):
            with self.subTest(requested=requested):
                self.assertIs(decide_lifetime("Bugün evden çalışıyorum", requested=requested), Lifetime.SESSION)
                self.assertIs(decide_lifetime("for now use tabs", requested=requested), Lifetime.SESSION)

    def test_phase_lowers_only_to_session(self):
        text = "Bu sprint sadece hata düzelt"
        self.assertIs(decide_lifetime(text, requested=Lifetime.SESSION), Lifetime.SESSION)
        self.assertIs(decide_lifetime(text, requested=Lifetime.PHASE), Lifetime.PHASE)
        self.assertIs(decide_lifetime(text, requested=Lifetime.PERMANENT), Lifetime.PHASE)

    def test_permanent_text_takes_lower_request(self):
        text = "Kullanıcı Python tercih eder"
        for requested in Lifetime:
            with self.subTest(requested=requested):
                self.assertIs(decide_lifetime(text, requested=requested), requested)

    def test_unknown_request_fails_closed(self):
        with self.assertRaises(ValueError):
            decide_lifetime("Kullanıcı Python tercih eder", requested="forever")


class ExpiryTests(unittest.TestCase):
    def test_ttl_constants(self):
        self.assertEqual(SESSION_TTL, timedelta(days=7))
        self.assertEqual(PHASE_TTL, timedelta(days=90))

    def test_expires_for_exact(self):
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(expires_for(Lifetime.SESSION, now), datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(expires_for(Lifetime.PHASE, now), datetime(2027, 1, 12, 12, 0, tzinfo=timezone.utc))
        self.assertIsNone(expires_for(Lifetime.PERMANENT, now))
        self.assertEqual(expires_for("session", now), now + timedelta(days=7))


if __name__ == "__main__":
    unittest.main()
