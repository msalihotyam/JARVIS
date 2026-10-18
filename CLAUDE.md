# JARVIS — Claude Code çalışma kuralları

Prosedürün tek doğruluk kaynağı `JARVIS_RUNBOOK.md`'dir; mimari karar kaynağı
`ARCHITECTURE.md` ve `PROJECT_CONTEXT.md`'dir. Bu dosya yalnız sert kuralları
ve değişmezleri, her biri tek satır nedeniyle tutar; ayrıntı runbook'tadır.
Çelişkide runbook kazanır. Kurallar hook/deny kurallarıyla da zorlanır; yine
de burada okunur olmaları gerekir.

## Başlarken

Her oturumda, geliştirmeye veya herhangi bir worktree yaşam döngüsü
işlemine başlamadan önce `JARVIS_RUNBOOK.md` → "Session bootstrap"
adımlarını uygula: runbook + `JARVIS_ACTIVE_STATE.md` + aktif roadmap
bölümü, ardından durum dosyasının son commit'inden bu yana git ile tazelik
kontrolü.

Konveyör lane'leri (istemin ilk satırı `@jarvis lane=` ile başlar) bunun
yerine yalnız ana checkout'taki `build/conveyor/notes/LANE_RULES.md`'yi ve
kendi istemini okur; runbook, durum, roadmap ve `jarvis-slice` /
`jarvis-queue` / `jarvis-lifecycle` skill'lerini yüklemez. Lane'lerde
LANE_RULES bu dosyanın "Test ve bitiş" ve "Lane ve worktree" maddelerinin
yerine geçer (dosya sahipliği C2, test kapsamı C3, önceden var olan hatalar
C4, rapor C7); depo/git güvenliği ve veri/şema kuralları hiçbir yerde
gevşemez. Neden: kota ve çelişkisiz tek kaynak (Salih, 2026-10-12;
orkestratör sistem denetimi, 2026-10-17).

Dilim (slice) başlatırken `jarvis-slice`, kuyruk çalıştırırken `jarvis-queue`
skill'ini kullan (`.claude/skills/`). İstem biçimi: runbook → "Prompt template
(lanes)".

## Sert kurallar

Depo ve git güvenliği:

- `uv.lock`'a asla dokunma; stage/commit etme. Neden: izlenmez, yerelde
  `.git/info/exclude` ile dışlıdır.
- `dev/dashboard/*` stage edilmez. Neden: dashboard emekli (yerine konveyor:
  `scripts/conveyor.py`, `build/conveyor/`; durum panosu: pano artifact'ı),
  WIP'i stash'te park edilmiş.
- Stage'i yalnız açık yollarla yap. Neden: başka şeylerin yanlışlıkla commit'e
  girmesini önler.
- Açık yetki olmadan asla push yapma; yetki bir sonraki push'a taşınmaz.
  Neden: `main`'in push'u her seferinde Salih'in kararıdır. Bir lane `main`'i
  hiç push etmez; istisna yalnız `ci/*` dalları (runbook → "Push").
  Tek `main` push yolu konveyor betiğinin iki anahtarlı kapısıdır
  (orkestratör onay dosyası + deterministik kontroller, bkz.
  `JARVIS_RUNBOOK.md` "Konveyor"); karar Salih, 2026-10-09.
- Kuyruk (unattended) çalışmalarda commit yalnız lane'in worktree dalına
  gider, canonical `main`'e asla. Neden: entegrasyonu orkestratör yapar.
- reset / restore / clean / stash ile iş silme veya saklama yok; silmeden
  önce hedefe bak. Neden: geri alınamaz, başka lane'in işi olabilir.
- Canlı connector / model / auth / ağ çağrısı ve paketli uygulamayı başlatma
  veya yeniden kurma yok (adım açıkça istemedikçe). Neden: maliyet ve yan etki.

Veri ve şema:

- Korunan yollar: `data\**`, `~\.jarvis\`, `.env*`, anahtar/sertifika/veritabanı
  dosyaları. Neden: operatör verisi ve sırlar.
- Testler asla `data\jarvis.db`'yi (canlı DB) açmaz. Neden: operatör verisini
  bozmamak; testler kendi izole TEMP'ini kullanır.
- `SCHEMA_VERSION` 10'da kalır (yalnız ek tablo, ALTER yok) schema-10
  sürümleri geri dönüş hedefi olduğu sürece. Neden: rollback eski şemayı
  okuyabilmeli.

Test ve bitiş:

- Testler yalnız `python -m unittest` ile (`scripts/test.ps1`); `uv run`
  asla. Neden: UI testleri sistem Python'unda koşar; referans sonuç budur.
- GREEN MEANS STOP: gerekli test yeşilse commit et ve dur; fazladan test,
  denetim veya refactor ekleme. Neden: kabulden sonra iterasyon kapsamı
  şişirir.
- Gerekli tam suite yalnız TIER gerektiriyorsa, dondurulmuş ağaçta BİR kez.
  Neden: suite pahalıdır (runbook → "Testing discipline").
- Kural D: yalnız yük-duyarlı listedeki testler düşerse her biri tek başına
  bir kez yeniden koşulur, geçerse koşu yeşildir (ikisini de kaydet); başka
  her hata bloklar. Neden: makine boş değildir.
- Fail-closed: belirsizlik, daha yüksek risk tier'ı veya başka lane'in dosyası
  çıkarsa dur ve raporla; tahminle devam etme. Neden: sessiz genişleme güvenlik
  ve çakışma riskidir.
- Commit öncesi `git diff --check`; amend yalnız aynı commit'i tamamlamak
  içindir ve raporun İKİNCİ satırında `AMENDED: <neden>` olarak söylenir
  (ilk satır her zaman STATUS).

Lane ve worktree:

- Kod lane'i kendi native worktree'sinde çalışır (`EnterWorktree
  name=<slice-id>`); ilk iş `git log -1` = istemdeki base SHA, uyuşmazlıkta
  DUR. Neden: yanlış tabandan çalışmak sessiz çakışma üretir.
- Worktree içindeyken canonical checkout'a karşı git çalıştırma. Bitişte
  commit, rapor, `ExitWorktree` KEEP; entegre edilmeden worktree silinmez.
  Neden: entegrasyon yalnız Salih "entegre et" derse, orkestratör sırasıyla
  olur.
- Her lane yalnız sahip olduğu dosyalara dokunur. Neden: bir dosya aynı anda
  tek lane'indir.
- Son rapor çalıştığın checkout'taki `build/reports/<slice-id>.md`
  dosyasına da yazılır (worktree içindeyken worktree'ninki; konveyör ikisini
  de bulur; `build/` gitignore'lu). Neden: orkestratör raporu depodan okur.

## Kod Erişimi

- JARVIS kod yapısı sorularında önce Serena sembolik erişimini kullan
  (semboller, referanslar, çağıran/çağrılan, ilgili dosya keşfi).
- Güvenlik açısından hassas veya davranış-kritik iddialarda bulunmadan önce
  gerçek kaynağı oku.
- Serena hafızasını doğruluk kaynağı sayma.
- Sembolik erişim soruyu yanıtlayabiliyorken geniş tam-dosya okumalarından ve
  tekrarlayan Grep/Glob taramalarından kaçın.
- Serena yetersiz veya erişilemez olduğunda yerel Read/Grep/Glob'a düş.
- Konveyör lane'lerinde Serena yoktur: `jarvis-scout` / Explore kullan.
- Kaynak kod her zaman nihai otoritedir.
