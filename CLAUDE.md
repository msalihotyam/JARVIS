# JARVIS — Claude Code çalışma kuralları

Bu dosya, bu depoda çalışan Claude Code oturumları için kalıcı talimatlardır.
Mimari karar kaynağı `ARCHITECTURE.md` ve `PROJECT_CONTEXT.md`'dir; bu dosya
onların yerine geçmez.

## Development Dashboard

Milestone panosu (`dev/dashboard/`) yalnızca bir geliştirme aracıdır: LLM yok,
ağ yok, bağımlılık yok. Ayrıntılı sözleşme `dev/dashboard/UPDATE.md`'dedir.

- Usable-v1 roadmap'inde anlamlı bir milestone gerektiği biçimde review edilip
  kabul edildikten ve canonical main'e entegre edildikten sonra, yalnızca
  ilgili milestone/durum alanlarını `dev/dashboard/status.json` içinde
  güncelle.
- Sonra çalıştır:

  ```
  python dev/dashboard/render_dashboard.py
  ```

- Yüzdeleri asla elle hesaplama; yüzdeler renderer'ın sorumluluğundadır.
- Ara işler, sıradan bugfix'ler, test tekrarları ve yalnızca dokümantasyon
  değişiklikleri için panoyu güncelleme.
- Bir milestone yalnızca canonical entegrasyondan sonra `completed` sayılır.
- Pano özetlerini kısa ve insana dönük tut.
- Açıkça talimat verilmedikçe roadmap ağırlıklarını ve ilerleme kurallarını
  değiştirme.
- Canonical bir milestone pano güncellemesinden sonra, ortaya çıkan takipli
  `dev/dashboard/status.json` değişikliğini küçük bir dev-tooling commit'ine
  dahil et; böylece canonical main temiz duruma döner.
- `dashboard.html` üretilen ve yok sayılan bir dosyadır; asla commit etme.

## Kod Erişimi

- JARVIS kod yapısı sorularında önce Serena sembolik erişimini kullan
  (semboller, referanslar, çağıran/çağrılan, ilgili dosya keşfi).
- Güvenlik açısından hassas veya davranış-kritik iddialarda bulunmadan önce
  gerçek kaynağı oku.
- Serena hafızasını doğruluk kaynağı sayma.
- Sembolik erişim soruyu yanıtlayabiliyorken geniş tam-dosya okumalarından ve
  tekrarlayan Grep/Glob taramalarından kaçın.
- Serena yetersiz veya erişilemez olduğunda yerel Read/Grep/Glob'a düş.
- Kaynak kod her zaman nihai otoritedir.
