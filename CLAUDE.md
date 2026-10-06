# JARVIS — Claude Code çalışma kuralları

Kalıcı çalışma kurallarının tek doğruluk kaynağı `JARVIS_RUNBOOK.md`'dir;
bu dosya onu tekrar etmez. Mimari karar kaynağı `ARCHITECTURE.md` ve
`PROJECT_CONTEXT.md`'dir.

## Başlarken

Her oturumda, geliştirmeye veya herhangi bir worktree yaşam döngüsü
işlemine başlamadan önce `JARVIS_RUNBOOK.md` → "Session bootstrap"
adımlarını uygula: runbook + `JARVIS_ACTIVE_STATE.md` + aktif roadmap
bölümü, ardından durum dosyasının son commit'inden bu yana git ile tazelik
kontrolü.

Anında gereken, runbook'ta ayrıntılanan kısa alt küme:

- `uv.lock`'a asla dokunma (izlenmez, stage/commit edilmez).
- Açık yetki olmadan asla push yapma; yetki bir sonraki push'a taşınmaz.
- GREEN MEANS STOP: gerekli test yeşilse commit et ve dur.
- Dilim (slice) başlatırken `jarvis-slice`, kuyruk çalıştırırken `jarvis-queue`
  skill'ini kullan (`.claude/skills/`).

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
