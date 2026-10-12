"""DFTONE (dogfood, Salih 2026-10-12): the Turkish interface addresses the user
formally, with "siz", everywhere.

The guard scans every finished tr-TR translation for the informal second person:
the pronoun "sen" in any case, the 2nd-singular verb endings that cannot be
anything else (-iyorsun, -eceksin, -ebilirsin, -mısın, the -sen/-san
conditional, -malısın, -mazsın, -mışsın, a word-final -diğin) and a bare
imperative that ends a sentence ("... seç."). Plain-command labels without a
full stop ("Kaydet", "Proje seç") stay natural and are not matched. Endings
that Turkish shares with other persons (-sin as the 3rd-person "silinsin mi?",
-diğinde, the genitive -in, the past -dın) are left to review: a pattern for
them would flag correct text.

No model, network or live DB: only the shipped catalog file is read.
"""
from __future__ import annotations

import re
import unittest

from jarvis.ui.i18n import catalog_path, load_catalog

_VERBS = ("seç|kaldır|yaz|dene|bekle|gir|çalıştır|tıkla|bas|aç|ekle|sil|onayla|gönder|bağla|kapat|başlat"
          "|sürdür|yenile|kullan|incele|düzenle|ara|sor|yap|tut|dön|ver|al|bak|oku|et")
INFORMAL = re.compile(
    r"\b(?:sen|seni|sana|senin|sende|senden|seninle)\b"           # the pronoun
    r"|\w+yorsun\b|\w+(?:acak|ecek)s[ıi]n\b|\w+(?:abilir|ebilir)sin\b"  # -iyorsun, -eceksin, -ebilirsin
    r"|\b(?:m[ıiuü]s[ıiuü]n)\b"                                    # yardım eder misin?
    r"|\w+[rzy]s[ae]n\b|\w+m[ae]s[ae]n\b|\w+(?:acak|ecek)s[ae]n\b"  # istersen, seçmezsen, yapmasan, edeceksen
    r"|\w+m[ae]l[ıi]s[ıi]n\b|\w+m[ae]zs[ıi]n\b|\w+m[ıiuü]şs[ıiuü]n\b"  # -malısın, -mazsın, -mışsın
    r"|\w+[dt][ıiuü]ğ[ıiuü]n\b"                                     # seçtiğin (word-final; 3sg is -diği/-diğini)
    r"|\b(?:" + _VERBS + r")[.;!?](?=\s|$)",                        # "... bir klasör seç."
    re.IGNORECASE)

# (context, source) -> why the informal form is right there. Keep it short.
ALLOWED = {
    # The user's own words to JARVIS (a sample prompt), not JARVIS addressing the user.
    ("Bridge", "Can you review my open tasks, check for any important updates, and help me plan the next steps for today?"),
    # Operation summaries on the approval card: what the action does, not an instruction to the user
    # (tests.test_v21d6 pins them).
    ("Present", "Create the project folder on the Desktop and link it. Nothing is sent."),
    ("Present", "Create the project folder in Documents\\JARVIS Projeleri and link it. Nothing is sent."),
}


class FormalTurkishCase(unittest.TestCase):
    def test_a_catalog_uses_siz(self) -> None:
        catalog = load_catalog(catalog_path("tr-TR"))
        self.assertGreater(len(catalog), 1000)                       # the real catalog, not an empty read
        found = sorted((key, text) for key, text in catalog.items()
                       if INFORMAL.search(text) and key not in ALLOWED)
        self.assertEqual([], found)

    def test_b_allowlist_is_live(self) -> None:
        catalog = load_catalog(catalog_path("tr-TR"))
        for key in ALLOWED:                                           # every exemption still exists and still needs it
            self.assertIn(key, catalog)
            self.assertRegex(catalog[key], INFORMAL)

    def test_c_pattern_catches_the_old_forms(self) -> None:
        for text in ("Ne yapmak istiyorsun?", "yalnız senin için bağlamdır", "Seni bekliyor",
                     "Proje seçmezsen sohbet projelerin dışında kalır", "Önce bağlantıyı kaldır.",
                     "Klasörü tam adıyla seç.", "Bunu yapabilirsin", "Yardım eder misin?",
                     "Klasörün seçtiğin klasör olduğu", "Önce kaydetmelisin"):
            self.assertRegex(text, INFORMAL)
        for text in ("Ne yapmak istiyorsunuz?", "yalnız sizin için bağlamdır", "Sizi bekliyor", "Kaydet",
                     "Proje seç", "Bu sohbet kalıcı olarak silinsin mi?", "Klasörü tam adıyla seçin.",
                     "Önce bekleyen onayı yanıtlayın (Onayla, Reddet veya İptal).", "insan", "Gizle, yer boş kalsın"):
            self.assertNotRegex(text, INFORMAL)


if __name__ == "__main__":
    unittest.main()
