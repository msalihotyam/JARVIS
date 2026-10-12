# JARVIS V2+ — Roadmap, Decisions & Reference Architecture

**Belge tarihi:** 2026-09-19  
**Son güncelleme:** 2026-10-06 — G0: V2.0 Gate 6G/6H planı (§3.7), kapsam dondurma + Next release inbox ve kaba dilim tahminleri (V2.0), V2.1'e taşınan arayüz kalemleri.
**Amaç:** JARVIS V1 sonrasında uygulanması planlanan V2 ve daha ileri ürün kararlarını tek yerde korumak.  
**Bu belge canlı proje durumu değildir.** Commit, branch, milestone ilerlemesi veya `JARVIS_ACTIVE_STATE.md` anlık bilgileri burada tutulmaz.

---

## 0. DURUM ETİKETLERİ

- **KESİN / MASTER ROADMAP:** Ürün yönüne dahil edildi. Zamanı geldiğinde tasarlanıp uygulanacak.
- **ONAYLI YÖN:** Kullanıcı tarafından beğenildi/onaylandı; kesin teknik tasarım milestone başında yapılacak.
- **LATER / DEFERRED:** İptal değil; daha sonraki sürüme ertelendi.
- **OPSİYON / AÇIK KARAR:** İlginç aday; henüz ürün taahhüdü değil.
- **DO NOT COPY / RED:** Referans üründen özellikle alınmayacak yaklaşım.
- **SUPERSEDED:** Daha yeni karar tarafından geçersiz kılındı.

---

# 1. MASTER V2+ ROADMAP

## V2.0 — Projects
**Statü: KESİN / post-V1 #1 öncelik**

Projects, V1 sonrası ilk büyük ürün katmanıdır.

### Kapsam
- Birden fazla Project.
- Her Project içinde birden fazla chat.
- Project instructions / kullanıcı kararları.
- Project Files.
- Project context.
- Project’e bağlı canonical chat / task / result / history.
- Mevcut Gmail / GitHub / Browser / Desktop / Scheduler capability’lerinin Project bağlamında çalışabilmesi.
- Project-specific skill ve ileride Work / Automation geçmişi için temel.
- Provider değiştirildiğinde Project kimliği, history ve memory korunur.

### Temel scope modeli
- `Global`
- `Project`
- `Conversation`

Bu scope ayrımı Memory, Skills, Work ve Automation’ın temelidir.

### Güvenlik kuralları
- Project seçmek connector grant yaratmaz.
- Project instruction kullanıcı kaynaklı authority olabilir.
- Imported file, web page veya dış içerik otomatik trusted instruction değildir.
- Project File Core authority’yi genişletemez.
- Project ≠ agent.
- Project ≠ provider.
- Project ≠ model.
- Project Files prompt’a topluca doldurulmaz; selective retrieval + provenance kullanılır.

### Kullanım kararı
V2.0 dogfood/aktif geliştirme için usable olabilir; **resmî daily-driver geçişi değildir.**

### Kapsam dondurma ve Next release inbox
**Salih kararı (2026-10-06):** V2.0 kapsamı 2026-10-06 itibarıyla DONDURULDU. V2.0 artık yalnız hata ve engelleyici (blocker) alır. Yeni fikirler hiçbir V2.0 dilimine girmez; buraya, tarihiyle ve kaynağıyla yazılır ve V2.1 (ya da sonrası) başında Salih'le sıralanır.

**Next release inbox:**
- (boş)

### Kalan iş — kaba dilim tahminleri (TAHMİN, G0 2026-10-06)
Bunlar karar değil, planlama tahminidir; dilim = bir commit'lik iş. V2.0 kalemleri §3.7 "Salih kararları — V2.0 Gate 6G / 6H planı"na göredir; V2.1–V2.6 kalemleri bu belgedeki bölümlere göre.

| Blok | Kapsam | Tahmini dilim |
|---|---|---|
| 6G K1 simge seti | çizilmiş simgeler, tofu glifler | 1 |
| 6G K2 hareket sözleşmesi | `Theme.dur()`, yol, HoverTip, boşta görünüm | 1–2 |
| 6G kenar çubuğu v2 | V1/L3/N1/L2/T1–T4/DRAG/V3/V10/V9/P1 çubuk/L4/V6+saat satırı/D1–D2 | 4–6 |
| 6G arama (F8) | S1–S9; salt-okunur tarama (tam suite) + arayüz | 2 |
| 6G düzen + düzenleme kipi | L1, besteleyici alt satırı, E1'/E2 | 2 |
| 6G başlık + menü (F9) | H1–H3, M1 + menü hatası, N2, K4 | 2 |
| 6G besteleyici + model/kullanım | M2, C1, U1–U3 | 2–3 |
| 6G Projects UX | P1 açılır pencere, P2 kartlar, P3 iç sayfa, W1/W3 panel | 3–4 |
| 6G alıntılama v2 + uzun yapıştırma (G6Q1, G6LP: `main`de gönderildi) | Q1, Q2', Q3', Q5, Q8, Q9, Q10, Q11, LP1–LP4 | 2–3 |
| 6G dondurma kaplaması 2 | kaplama ayarı + kaplama 2 | 1 |
| 6G kalite K3/K5/K6 | dürüst göstergeler, akış, sayfalar | 3 |
| 6G toplam | | 23–29 |
| 6H klasör bağı | K1–K4 (ek tablo, T2) + otorite incelemesi | 2–3 |
| CI | gate CI koşusu (+ olası düzeltme) | 1 |
| Kurulum | release build + install (Salih onayı) | 1 |
| Dogfood | D-2 / D-3 | 1–2 |
| Rollback | D-4 paketlenmiş `-Rollback` ve geri | 1 |
| V2.0 kapanış | belge/durum dilimi | 1 |
| V2.0 kalan toplam | | 30–38 |
| V2.1 Advanced Memory + daily-driver | typed memory, SessionEnd/PreCompact capture, consolidation, retrieval, Memory Doctor, history import, tüm-projeler anahtarı, bağlam göstergesi, "Hatırla", taşınan arayüz kalemleri | 25–35 |
| Dağıtım: installer + onboarding | per-user installer, `%LOCALAPPDATA%` göçü, ilk-çalıştırma sihirbazı, imzalama/güncelleme | 6–10 |
| V2.2 Skills + Experience | skill nesnesi, experience, health/evolution, workflow recording → skill | 12–18 |
| V2.3 Deep Research | durable araştırma işi, plan, kaynak/atıf, rapor, pause/resume | 8–12 |
| V2.4 Work + Subagents + Persistent Agents | work durable state, subagent'lar, isimli ajanlar, handoff | 15–25 |
| V2.4A Jev Decision Engine | geliştirme pilotu (eval/shadow), ürün pilotu | 5–8 |
| V2.4B Orchestrated Tasks | Faz 1 (tek seviye, sıralı, read-only, <=5 child) | 5–8 |
| V2.5 Automation 2.0 + Routines | trigger modeli, job/run/result, background execution | 10–15 |
| V2.6 Browser/Desktop 2.0 | araç seçim sırası, replay, UIA, vision fallback | 12–18 |

### V2.1–V2.6 birleştirme / küçültme önerileri (Salih için ÖNERİ, karar değil)
- V2.2 "Workflow Recording → Skill" ile V2.6 "Workflow Recording" aynı kayıt/yeniden oynatma motorunu ister: motor bir kez (V2.6'da ya da öne çekilerek) yapılır, V2.2 yalnız skill tarafını alır.
- V2.4B Faz 1 (parent/child, Core pipeline'ı) V2.4 subagent işiyle aynı yüzeydedir: tek bir V2.4 bloğunda birleşebilir.
- V2.4A Jev: yalnız geliştirme pilotu (çevrimdışı eval + shadow) yapılır; ürün pilotu kalite kanıtlanana kadar ertelenir — blok küçülür.
- V2.3 Deep Research, V2.4 Work durable state'in bir görev türü olarak kurulabilir (pause/resume, checkpoint, evidence ortak): V2.4'ten sonra ya da onunla birlikte yapılırsa küçülür.
- V2.5 Routine = Skill'in zaman/koşul/olay sarmalayıcısı; V1 zamanlayıcısı ve V2.2 skill'leri üzerine ince bir katman olarak küçülebilir.
- V2.1'den Memory Doctor ve history import bir V2.1.x'e ayrılabilir; daily-driver kapısı typed memory + capture + retrieval ile daha erken gelir. (Mekanik lamba ve dondurma 3–4 V2.0'da gönderildi, Salih 2026-10-11; ayrı kaplama paketi gerekmez.)
- V2.4 isimli kalıcı ajanlar, subagent'lardan sonra ayrı bir alt sürüm olabilir.

---

## V2.1 — Projects + Advanced Memory
**Statü: KESİN / RESMÎ V2 DAILY-DRIVER GATE**

JARVIS’in V2’de ana günlük kullanım asistanına resmî geçişi, Projects ve Advanced Memory birlikte güvenilir olduğunda yapılacak.

### Hedef
Basit:
`message → embedding → vector search`
değil.

Hedef yaşam döngüsü:

`Working Context`
→ `Episodic Memory`
→ `Typed Semantic Knowledge`
→ `Procedural Memory / Experience`
→ `Consolidation`
→ `Selective Retrieval`
→ `Health / Evolution`

### Typed memory nesneleri
En az:
- `Fact`
- `Decision`
- `Rule`
- `Lesson`
- `Preference`

Her kayıtta mümkün olduğunca:
- scope
- source / provenance
- created_at
- last_verified
- confidence
- supersedes / superseded_by
- freshness / expiry
- ilgili Project / Conversation
- gerekirse actor / tool provenance

### Kesin tasarım ilkeleri
- **Memory capture modelin disiplinine bırakılamaz; mekanizma olmalı.**
- **SessionEnd capture olmalı.**
- **PreCompact capture / safety net olmalı.**
- Consolidation lifecycle/background işi olmalı.
- Provider-independent memory: Claude/Codex/başka provider için ayrı kişilik hafızaları oluşturulmaz.
- Retrieval task-targeted olmalı; bütün memory prompt’a dökülmez.
- En yeni geçerli Decision/Rule, superseded eski kaydı yenmeli.
- Başka Project’e memory sızmamalı.
- Provider switch memory scope’u bozmamalı.
- Stale/conflicting/orphan kayıtlar görünür ve düzeltilebilir olmalı.

### Salih kararı (2026-09-29): "tüm projelerin hafızası" anahtarı
- Global sohbette conversation başına açılıp kapanan anahtar; varsayılan kapalı.
- Yalnız memory: instruction ve dosya yok. Salt-okunur; gösterdiği her şeyde kaynak (provenance) etiketi.
- Yukarıdaki "Başka Project'e memory sızmamalı" ilkesine tek, adlandırılmış, kullanıcı-açık istisna budur.
- V2.1 bağlam kalemleri: PreCompact capture, tam arşiv retrieval, context-usage göstergesi, "Hatırla" butonu.
- **Gelecek uyumluluk sözleşmesi (Gate 6C, 2026-09-30):** anahtar kaydı ayrılmış tablo `conversation_global_memory_toggles`'da tutulur (V2.1 sahiplenir; append-only, yalnız açık-kullanıcı yazımı, `conversation_id` anahtarlı). V2.1 kabulü: "Projeye bağla", anahtarı bir kez açılmış bir konuşmayı reddeder (6C bu tabloyu her bağlamada okur; satır varsa veya tablo okunamıyorsa ret).

### Memory Doctor / Health
Planlanan kontroller:
- stale records
- contradictions/conflicts
- orphan records
- broken provenance/index
- consolidation backlog
- outdated rules/skills
- freshness problems

### History import
Planlanan akış:

`import`
→ `dedup`
→ `scope suggestion`
→ `extraction`
→ `user review/approval`
→ `consolidation`

Imported geçmişin içindeki talimatlar otomatik yürütülmez.

### V1 Context Manager ile fark
V1 Context Manager:
- aynı conversation’da recent raw turns
- checkpoint/summary
- pinned state
- conversation continuity

V2 Advanced Memory:
- Projects ve sessions arası uzun vadeli bilgi
- typed decisions/rules
- provenance
- supersession
- freshness
- import
- experience/skill bağlantısı

V1 Context Manager, Advanced Memory yerine sayılmaz.

### Gate 6G kapanış kararları (Salih 2026-10-11 03:35, GATE6G_review.md yanıtları)
- **G-2:** durum lambası varsayılanı: Salih "farketmez" → orkestratör kararı: gönderilen varsayılan korunur (mekanik lamba; cam küre ayarlardan seçilir). Kod değişikliği yok.
- **A-2/A-3:** mekanik lamba ve dondurma tuşu kaplamaları 3–4 V2.0'da gönderildi (V2.1 planlıydı): KABUL.
- **A-4:** W3 çalışma paneli akışın yanında yüzer (onaylı sağ-sütun üstü yerine): şimdilik kabul; Salih dogfood sırasında değerlendirecek. AÇIK DOGFOOD KALEMİ.
- **B-2:** dondurulmuşken (yürütme kapalı) K1 "Oluştur" klasörü yine oluşturur (kullanıcının tıklaması onaydır, Q5); K3/E7 dondurulmuşken reddeder. Salih: şimdilik önemli değil, dondurma özelliğine V2 sonrası döner. Mevcut davranış ARCHITECTURE.md 6H K1 paragrafında; ERTELENDİ.

### V2.0'dan taşınan arayüz kalemleri (Salih kararı 2026-10-06)
- **Mekanik lamba = üçüncü durum göstergesi kaplaması** (S1'in yerine; saat tipi lamba ve pano 12 "TornLamp" SVG'si reddedildi, Salih 2026-10-05). Mevcut ayar `bridge.statusIndicator` / SettingsDrawer "statusIndicatorChoice" ("orb" Canonical Orb, "hud" HUD Orb) "mechanical" seçeneğini alır; V2.0'da gönderildi (Salih 2026-10-11, Gate 6G kapanışı A-2/A-3: V2.1'den V2.0'a alındı, KABUL). VARSAYILAN: gönderilen varsayılan korunur, mekanik lamba varsayılandır; cam küre ayarlardan seçilir (Salih 2026-10-11 G-2: "farketmez" → orkestratör kararı; kod değişikliği yok; eski "varsayılan değişmez / mekanik isteğe bağlı" satırı geçersiz). Görünüm: JARVIS yüzeyi lamba yerinde yırtılmış, içinde ayrıntılı, gerçekçi mekanik iç (dişliler, eşapman, balans çarkı); metal renkler, renk/ışıkla dikkat çekme yok. Sanat: Astra kahraman görseli `master_v1.png` (onay 2026-10-05 23:10) + temiz plaka `plate_v1.png`; katmanlar `build.py` + `parts.py` ile; onaylı canlı prototip `mekanik_lamba.html` (artifact "Mekanik Durum Lambası" D4ECQ5x6MgZoCsGpwmPDcF, onay 2026-10-06 00:31). Hepsi `build\6g\lamp\` altında, henüz repoda değil. Boyut (onay 2026-10-05 23:56): 2,15x = kıvrık kanatlar dahil 120 px, üstten 4 px, sağ sütun üst kenarını tasarım gereği ~20 px örter; boyut TEK özelliktir, katmanlar ana çözünürlükte. TopBar durum yuvası kaplamaya göre boyutlanır (mekanik 120 px, diğerleri 60 px). Kinematik: tek motor açısı; büyük çark çalışırken %100'de 24 derece/s; ready %25; freezing ~4 s'de 0'a yavaşlar, sonra frozen durur; kafes 1,5 Hz salınır (çalışırken ±30, ready ±12); cırcır tıklar; zincirler/yay kayar; ambient kapalı = hareketsiz. 13 çark + 4 planet + kafes + 2 kol + 2 zincir + yay hareket eder; 14/15 kolları statik (örtücü). Hız tek sayıdır; son değer dogfood'da. QML katmanları canlandırır.
- **Dondurma tuşu kaplamaları 3–4** (V2.0'da gönderildi, Salih 2026-10-11 A-2/A-3 KABUL; kaplama ayarı ve kaplama 1–2 de V2.0'da): kaplama 3 = krom geçiş anahtarı (yarasa sap; plakada yazı YOK — dondurma kapatma değildir, Salih 2026-10-06 01:10); çok hızlı canlanır (~60–90 ms kol dönüşü + küçük oturma), ambient kapısından. Kaplama 4 = bıçaklı şalter, YIRTIK yüzey sürümü (Salih 2026-10-06 02:06; kaynak `build\6g\freeze\knife_torn_v1`, sığma testi `dondur_gorunumleri.png`); menteşelerinde döner (daha yavaş, ağırlıklı). Sanat kaynakları `build\6g\freeze\` (`toggle_v1.png`, `knife_torn_v1`); boyut kaplama başına tek sayı. Görsel durum yalnız `bridge.executionState`; yetki/anlam bugünküyle aynı.
- **Kullanım penceresinin bağlam penceresi bloğu** (Salih 2026-10-05): kullanılan / en çok, çubuk, "Otomatik sıkıştırmaya N", "Oturumu sıkıştır"; V2.0 penceresinin (U3) üstüne eklenir (yukarıdaki context-usage göstergesi kalemi).
- Plan gereği V2.1'de gelenler: proje sayfasında Hafıza satırı (6G P3), sohbet menüsünde "Mark as unread" (F9 H3; 6G D1/D2 yalnız tamamlanma alt kümesidir).

### Gate 6G review leftovers (Salih 2026-10-11, Gate 6G kapanışı; ertelendi)
Kaynak: build/6g/GATE6G_review.md; G6H3POL kalanları build/reports/G6H3POL.md OPEN ISSUES. Hepsi V2.1 temizlik listesidir, V2.0 kapsamı değil.
- **D-1:** yetim ModelSelector.qml / QuotaBar.qml.
- **D-2:** QML çağıranı olmayan bridge API'si.
- **D-3 / D-5:** yetim özellikler ve id'ler.
- **D-4:** yinelenen talimat / etkin dosya okumaları.
- **D-7:** UnreadBell simge yolu.
- **D-9:** konveyör çıplak `except`.
- **C-5:** CI skip-count kapısı.
- **C-6:** bayat test adları.
- **C-7:** F-gt izlenen kalsın.
- **C-9:** "CLI uygulama kurulmadan reddeder" sabitlemesi.
- **B-3:** genel belgelerde OS kullanıcı adı.
- **B-4:** bozuk bağ satırı klasörsüz okunur.
- **D-6 kalanı (G6H3POL):** (a) K1 metinleri app.py'de en-US altında hâlâ Türkçe, tests/test_g6h1b_ui.py ~212/~226-227 sabitler; (b) bilinmeyen klasör nedenleri ham geçer, tests/test_g6h2_folder_ui.py ~112 sabitler; (c) bağlayıcı klasör iletileri pencereye jarvis/ui/present.py'deki Core zarflarından ulaşır; (d) `_UNVERIFIED` Türkçe soneki; (e) köprü `_k3_switched_to`'nun gösterdiği E7 hata metinleri ham.
- **K3 kalanı (G6H3POL):** öneri Core'un reddinden önce değiştiyse "var olan klasörü seç" hâlâ FAILED biter (REJECTED değil); hiçbir şey oluşturulmaz; düzeltme core.py ister.
- **B-2 (dondurma sırasında K1):** bkz. aşağıdaki karar; dondurma özelliği V2 sonrası yeniden ele alınacak.
- **A-4 (W3 çalışma paneli yerleşimi):** açık dogfood kalemi; bkz. aşağıdaki karar.

### Salih kararları (2026-10-12 10:13)

Kaynak: `build/v21/PLAN_DRAFT_V21.md` (V2.1 plan taslağı, KARAR-1..5 soruları).

- KARAR-1: ek tablolar, SCHEMA_VERSION 10'da kalır.
- KARAR-2: önce yerel FTS5 + typed filtreler; embedding V2.1b'de ölçüme göre.
- KARAR-3: karma - Decision/Rule kullanıcı onaylı, Fact/Lesson otomatik + geri alınabilir.
- KARAR-4: V2.1a / V2.1b ayrımı kabul.
- KARAR-5: temizliklerin hepsi: D-1/D-2 yetim kod/testler (silme = hold'a taşıma), B-3 açık repodaki kullanıcı adı/yol, küçük temizlikler (ARCHITECTURE.md:3655 eski metin, cli.py:925 Türkçe K3 reddi, test_motion_6g settingsOrb sıra bağımlılığı, "Mark as unread").
- Font render: T varsayılan (Salih 2026-10-12 13:00, DFFONT).

---

## Dağıtım: installer + onboarding

**Durum: ONAYLI YÖN** (Salih 2026-09-30). Zamanlama: V2.1'den sonra; kesin tasarım o milestone başında yapılır.

- kullanıcı-başına (per-user) installer
- veri depodan `%LOCALAPPDATA%`'ya taşınır, bir göç (migration) ile
- ilk-çalıştırma sihirbazı: sağlayıcı CLI'larını (Claude, Codex/ChatGPT, Antigravity) tespit eder / kurar / oturum açtırır ve Chrome'u denetler; kendiliğinden hiçbir izin vermez (fail-closed başlangıç); sırlar Windows Credential Manager'da
- kod imzalama (maliyet = Salih kararı)
- imzalı güncellemeler

---

## V2.2 — Skills + Experience Learning
**Statü: KESİN**

Amaç: JARVIS tekrar eden işleri her seferinde sıfırdan çözmek yerine öğrensin.

### Skill
Bir işin **nasıl yapılacağını** temsil eder.

Skill:
- reusable
- scoped
- versioned
- provenance taşıyan
- test/health bilgisi olan
- authority olmayan prosedürel bilgi

**Skill hiçbir zaman Core authority değildir.**

### Experience
Sadece başarılar değil:
- başarılı yollar
- yanlış dönüşler
- “bir daha bunu yapma” lessons
- recovery stratejileri
- kırılmış workflow deneyimleri

saklanmalıdır.

Her görevde bütün experience yüklenmez; task-targeted retrieval yapılır.

### Skill Health / Evolution
- gerçek başarısızlıkları algılama
- kırılmış workflow tespiti
- versioning
- re-validation
- stale skill işaretleme
- gerektiğinde agent recovery

### Workflow Recording → Skill
Onaylanan yön:

1. JARVIS/agent işi ilk kez başarıyla yapar veya kullanıcı işi gösterir.
2. Trace kaydedilir.
3. Parametreler ayrıştırılır.
4. Deterministic workflow/Skill adayı oluşturulur.
5. Sonraki çalışmalarda hızlı ve kanıtlı tekrar.
6. Workflow kırılırsa agent recovery.
7. Yeni version oluşturulur.

### Grok Bot’tan alınacak zincir
**Teach a Task → Skill → Routine**

Bu fikir KESİN V2+ tasarım yönüne dahil edilmiştir.

- Kullanıcı bir işi bir kez gösterir/yaptırır.
- JARVIS reusable Skill taslağı çıkarır.
- Kullanıcı doğrular.
- Birkaç başarılı çalışmadan sonra Routine/Automation’a bağlanabilir.

### Skill akışı (Salih 2026-10-08)
V2.2 kapsamındadır; V2.0 dondurulmuş kalır, buraya hiçbir şey öne çekilmez.

1. Bir işe başlamadan önce JARVIS uygun bir skill arar. Biri uyuyorsa onu kullanır ve işin başında söyler ("Bu iş için X skill'ini kullanıyorum"); kullanıcı hayır diyebilir.
2. Uyan skill yok ama benzeri varsa JARVIS onu uyarlayıp YENİ bir skill üretir; özgün skill değişmeden kalır. Yeni skill ebeveynini (provenance) kaydeder.
3. Hiçbir şey yoksa JARVIS işi kendisi yapar.
4. Kendisi için yeni olan bir işten sonra JARVIS "Bunu skill olarak kaydedeyim mi?" diye sorar ve taslağı (adımlar, girdiler, kontroller) gösterir. Yalnız kullanıcı onaylarsa kaydedilir.
5. Yeniden kullanılabilir parçaları olan tek bir iş birden fazla skill üretebilir.
6. JARVIS yalnız tekrarlanabilir ve önemsiz olmayan işler için öneride bulunur, tek seferlik işler için değil. Kullanıcı "bu tür işler için sorma" diyebilir.
7. Bir başarısızlıktan ya da kullanıcı bir skill'in sonucunu düzelttikten sonra JARVIS güncellenmiş bir sürüm önerir. Yalnız onayla kaydedilir; eski sürümler geri alma için durur.
8. Bir skill asla sır, kimlik bilgisi ya da kişisel veri içermez. Veri sınıflandırmasına uyar; kapsamı global ya da tek bir proje olur.
9. Her skill kullanım sayısını, son başarıyı ve başarısızlık sayılarını tutar (Skill Health'i besler). Kullanıcının skill'leri görüntüleyip düzenleyebildiği, devre dışı bırakıp silebildiği bir skill listesi vardır.
10. Skill prosedürdür, otorite değildir: her adım yine Core politikasından ve onaydan geçer; skill hiçbir şeyi önceden onaylayamaz (V2.2 değişmezinin yinelenmesi).

---

## V2.3 — Deep Research
**Statü: KESİN**

V1 Web Research ile aynı şey değildir.

### V1 Web Research
- güncel soruya hızlı kamu kaynaklı cevap
- kısa süreli araştırma

### Deep Research
Dakika ölçeğinde ayrı durable iş:

`research question`
→ `scope`
→ `plan`
→ `multi-query / multi-site`
→ `source reliability`
→ `conflict comparison`
→ `citation/provenance`
→ `structured report`

### İstenen özellikler
- pause / resume
- kullanıcı planı yönlendirebilir
- Project/File kaynakları dahil edilebilir
- hariç tutulacak siteler belirlenebilir
- source publication date ve event date ayrı izlenir
- kaynaksız kesin iddia üretilmez
- normal model cevabı “Deep Research” diye etiketlenmez

---

## V2.4 — Work Mode + Subagents + Persistent Specialist Agents
**Statü: KESİN**

### Mod ayrımı
- **Chat:** soru/cevap.
- **Plan / Explore:** yan etkisiz inceleme ve plan.
- **Work:** uzun, sonuç üreten durable görev.

### Work durable state
En az:

`goal`
→ `plan`
→ `subtasks`
→ `checkpoints`
→ `artifacts`
→ `evidence`
→ `acceptance checks`
→ `final result`

Uygulama kapatılıp açıldığında durum kaybolmamalı.

### Work ilkeleri
- Gerçek olaylardan ilerleme; fake percentage yok.
- Kullanıcı yönlendirebilir.
- Kullanıcı pause/stop yapabilir.
- Sonuç ve evidence incelenebilir.
- “Agent tamamladım dedi” = completion proof değildir.
- Acceptance checks/evidence birinci sınıf olmalı.
- Proje sohbeti çalışma panelinin tam sürümü (6G W2, Salih 2026-10-05): Progress adımları, Outputs, Used-in-session; yalnız gerçek veri, sahte ilerleme yok. 6G'deki ilk sürüm bunun alt kümesidir.

### Subagents
- Default dev swarm yok.
- Görev gerektiği kadar worker.
- Research / implementation / verification farklı worker’lara ayrılabilir.
- Worker çıktısı evidence’dır, authority değildir.
- Worker’lar birbirlerine bounded iş devredebilir.

### KESİN authority kuralı
**Subagent hiçbir zaman Core authority’yi miras almaz.**

Her gerçek action yine Core’da:

`identity`
→ `capability`
→ `target`
→ `scope`
→ `approval`
→ `execution`

kontrolünden geçer.

### Named Persistent Agents / “Meslek Sahibi” JARVIS Ekibi
**Statü: KESİN ÜRÜN YÖNÜ — kullanıcı özellikle istedi.**

Uzun vadede JARVIS yalnız geçici worker spawn eden sistem olmayacak; mümkün olduğunda **isimli, kalıcı görev/meslek kimlikleri olan uzman ajanlar** da olacak.

Örnek roller:
- Researcher
- Developer / Engineer
- Reviewer
- Security Specialist
- Inbox Assistant
- Data/Document Specialist
- Automation Operator
- Project Coordinator

Exact isimler ve UI henüz belirlenmedi.

### Agent kimliği kuralı
**Agent ≠ model ≠ provider**

Bir uzman agent’ın:
- rolü
- geçmişi
- görev bağlamı
- Project ilişkisi

kalıcı olabilir.

Altında kullanılan model ise:
- task type
- difficulty
- precision requirement
- latency/cost
- verification need

gibi koşullara göre değişebilir.

Aynı agent gerektiğinde farklı provider/model kullanabilir.

### Bot-to-Bot / Agent-to-Agent Handoff
Grok Bot’tan alınacak:
- görünür handoff
- task ownership
- parallel work
- specialist delegation
- reviewer/verifier lane

Ancak bütün handoff’lar Core sınırları içinde kalır.

---

## V2.4A — Jev Decision Engine Evaluation + Decision Layer
**Statü: KESİN POC / SHADOW-MODE; production kullanımı eval sonucuna bağlı**

Jev, JARVIS’in chat/generative provider’ı veya yeni “ana beyni” olarak değil, **System-1 / bounded decision engine** olarak değerlendirilecek.

Temel ayrım:
- **Opus / Fable / Codex / diğer generative modeller:** düşünme, planlama, kodlama, sentez, açıklama ve uzun-horizon problem çözme.
- **Jev / Decision Engine:** hızlı ve tipli route / score / gate / classify kararları.
- **Core:** final authority; izin, güvenlik, approval, scope ve side-effect sınırları deterministic kalır.

### A. Development Pilot — JARVIS’i geliştirirken
İlk POC production ürün yoluna bağlanmadan, geçmiş JARVIS geliştirme kararları üzerinde benchmark/shadow-mode olarak çalışacak.

İlk decision dataset adayları:
- model/effort routing: Opus 5.5 / Fable 5.1 / Sonnet 5
- gate routing: audit / implementation / re-gate / human check
- failure routing: retry / stronger model / security re-gate / ask user / stop
- test-domain selection: browser / provider / memory / UI / packaging / release-manager
- review triage: blocker / follow-up / nit / no-action

Değerlendirme:
- aynı bounded kararlar Jev ve güçlü generative baseline (özellikle Opus 5.5) ile karşılaştırılır
- correctness / calibration / latency / cost ayrı ölçülür
- Jev sonucu hiçbir development security/release gate’ini tek başına geçiremez
- test-selection kullanımında Jev yalnız **ek test önerebilir**; deterministic minimum test setini azaltamaz

### B. Product Pilot — JARVIS’in kendi runtime’ında
Development eval yeterli kalite gösterirse ayrı `DecisionProvider` katmanı olarak shadow-mode ile başlatılır.

İlk production adayları, öncelik sırasıyla:
1. model/tool routing
2. Work/subagent loop: continue / retry / alternate-tool / ask-user / finish
3. output/tool-plan verification sinyalleri
4. `smart_auto` için advisory risk/review sınıflaması
5. memory/context relevance reranking
6. Gmail/GitHub/event triage
7. browser-agent bounded action selection

### Authority / güvenlik kuralları
- **Jev authority değildir.**
- Hard deny, capability grant, exact target/scope ve explicit approval kuralları Jev’den önce/sonra Core tarafından uygulanır.
- Jev hiçbir hard-deny kararını allow’a çeviremez.
- Secret access, destructive filesystem action, external irreversible side effect, release authorization ve final security PASS kararı Jev’e bırakılmaz.
- Confidence skoru global “güven eşiği” olarak kullanılmaz; task-class bazında calibration gerekir.
- Jev unavailable/low-confidence olduğunda deterministic fallback veya uygun generative model devreye girer; execution sessizce permissive olmaz.

### Privacy başlangıç kuralı
İlk pilotta Jev’e ham kişisel içerik verilmez.

Başlangıç payload’ı mümkün olduğunca:
- task metadata
- tool/capability isimleri
- risk metadata
- sanitized summaries
- diff/test metadata

ile sınırlıdır.

Raw Gmail body, private memory, dosya içeriği veya başka hassas kullanıcı verisi ancak ayrı privacy/data-retention gate’inden sonra değerlendirilebilir.

### Maliyet / telemetry
- Jev gerçek API harcaması olarak JARVIS’in mevcut **20 USD/ay hard-cap** muhasebesine dahil edilir.
- Decision telemetry en az: decision type, candidate set, chosen result, latency, confidence/probability, fallback, downstream outcome.
- Secret veya ham özel içerik telemetry’ye yazılmaz.

### Promotion gate
Jev production karar yoluna ancak şu aşamalardan sonra girer:

`historical eval`
→ `shadow mode`
→ `task-class calibration`
→ `bounded advisory use`
→ `measured production promotion`

Kalite avantajı kanıtlanmayan decision class deterministik/Opus tabanlı mevcut yolunda kalır.

### Zamanlama
- Mevcut Edge → Chrome güvenlik migration’ı tamamlanmadan bu özellik için implementation başlatılmaz.
- POC, büyük Work/Subagent/Automation ölçeklenmesinden önce veya onun erken safhasında yapılması tercih edilen cross-cutting V2 işi olarak tutulur.
- Exact SDK/provider/retention koşulları implementation milestone’u başında yeniden doğrulanır.

---

## V2.4B — Orchestrated Tasks / Parent–Child Task Graph
**Statü: KESİN** — Faz 1 kapsamı kesin; sonraki fazlar sırasıyla ve her biri kendi gate’iyle açılır.

Orchestrator işi **parçalar**; authority **yaratmaz**.

### KESİN authority kuralları
- Parent / orchestrator’ın blanket connector authority’si yoktur.
- Child task’lar normal Core pipeline’ından geçerek yürür.
- **Plan onayı ≠ blanket execution authority.**
- Child scope, onaylanan parent plan/scope’un içinde kalmak zorundadır.
- Plan, kanonik child scope’larını içerir; plandaki materyal bir değişiklik o planın onayını geçersiz kılar.
- Mevcut Core approval / grant / connector / identity / güvenlik gereksinimleri her child action için ayrıca ve bağımsız olarak uygulanır.
- Parent, ham child çıktısını değil, sınırlı ve yeniden doğrulanmış özetleri alır.

### Faz 1
- tek orchestration seviyesi
- sıralı (sequential) yürütme
- yalnız read-only connector’lar
- en çok 5 child task

### Sonraki fazlar
1. paralel read-only child’lar
2. verifier child
3. yazan (writing) child’lar

Yazan child’lar, etkinleştirilmeden önce ayrı bir threat model ve ayrı bir implementation milestone’u/gate’i gerektirir.

---

## V2.5 — Automation 2.0 + Routines
**Statü: KESİN**

V1 scheduler üzerine gelişecek.

### Trigger modeli
Aynı durable yapıda:
- `Time Trigger`
- `Condition Watch`
- `Event Trigger`

Ana lifecycle:

`Trigger`
→ `Job`
→ `Run`
→ `Result`
→ `Notification`

### Gerekenler
- recurring jobs
- retry/recovery
- history
- result storage
- notification
- approval-aware actions
- durable claim/fence/dedup
- UNKNOWN_OUTCOME non-replay
- Freeze safety
- proje sayfasında Zamanlanmış satırı (6G P3; V2.5'ten önce gösterilmez)

### Event örnekleri
Bunlar örnek use-case’tir, otomatik authority değildir:
- önemli yeni mail → özet
- PR açıldı → review preparation
- yeni PDF geldi → processing
- deployment error → alert
- file/process/system event → bounded action

Connector grant ve approval olmadan canlı side effect başlamaz.

### Skill vs Routine
Grok Bot referansı ile net ayrım:

- **Skill = nasıl yapılır**
- **Routine = ne zaman / hangi koşulda yapılır**

Routine bir Skill’i:
- schedule
- condition
- event

ile tetikleyebilir.

### Background execution
KESİN yön:
- JARVIS UI açık olmasa bile uygun mimari ile background işler.
- Windows scheduling / background host / ileride remote runtime değerlendirilecek.
- Core security/authority korunur.

---

## V2.6 — Browser/Desktop 2.0 + Workflow Recording
**Statü: KESİN**

### Araç seçim sırası
Canonical tercih:

1. Native Connector / API
2. Structured site/tool
3. DOM browser automation
4. Windows UIA
5. Visual / coordinate fallback

“Her seferinde ekrana bak ve tıkla” default yöntem olmayacak.

### Hedefler
- deterministic workflow replay
- robust waits
- state verification
- parameters
- versioning
- diagnostics
- agent recovery
- clipboard
- drag/drop
- screenshots
- richer field reads
- desktop UIA
- gerektiğinde vision fallback

### Authority
- exact origin / exact app scope
- step-level verification
- web page veya desktop window kendi başına executable instruction authority kazanmaz
- recorded workflow Core authority bypass edemez

---

# 2. SONRAKİ 2.x / LATER LAYERS

## P7 — Remote / Mobile JARVIS
**Statü: ONAYLI YÖN / implementation later**

İlk ürün “Windows ekranını telefona küçültmek” değil, **mobil kumanda**.

### İlk hedef mimari
`iPhone Safari / PWA`
→ private encrypted access
→ `Tailscale`
→ `Windows JARVIS Remote Gateway`
→ `JARVIS Core`

### Windows laptop
- Core authority sahibi
- secrets sahibi
- Gmail/GitHub/filesystem/shell credentials telefona taşınmaz

### Telefon
- chat/new Work başlatabilir
- Project/chat seçebilir
- running jobs görebilir
- results görebilir
- approvals görebilir
- automations/notifications görebilir
- freeze/stop isteği gönderebilir

### Remote approval
Sadece `approve=true` değildir.

Planlanan güvenlik:
- approval ID
- exact action fingerprint
- device identity
- nonce
- timestamp
- signed request
- Core-side verification
- replay rejection
- action substitution rejection

Mobil işlem desktop/Core authority’yi bypass etmez.

### İlk sürüm
- Tailscale/private network
- public endpoint yok
- port forwarding yok

### Later options
- PWA protokolünden sonra native SwiftUI iOS
- device-local Face ID gate
- belki cloud relay
- laptop kapalıyken bağımsız runtime ancak ayrı mimari sonrası

### PrivacyCurtain / Remote Desktop
Tam Windows erişimi için ayrı fallback olabilir; Remote JARVIS’in yerine geçmez.

---

## P8 — Voice
**Statü: DEFERRED / kullanıcı tekrar açıkça istemeden başlanmayacak**

- STT/TTS
- wake word
- microphone pipeline
- voice interaction

ileride mümkün.

Ancak kullanıcı tekrar istemeden aktif milestone değildir.

İleride Voice:
- ayrı input/output adapter
- approval/Work/authority yine Core’dan
- fake “listening” yok

---

## P9 — Artifact / Data / Image Engine
**Statü: LATER / DEFERRED ama roadmap’te**

Kapsam adayı:
- Word/document
- PDF
- spreadsheet/Excel
- PowerPoint/slides
- dashboards/charts
- data analysis
- interactive HTML/apps
- image create/edit

Bu sistem:
- gerçek file generation
- versioning
- preview
- provenance
- reproducibility
- calculation reliability

gerektirir.

V1’den özellikle çıkarıldı; ayrı sistem olarak tasarlanacak.

---

## P10 — Plugin / Connector Ecosystem
**Statü: LATER / roadmap’te**

Adaylar:
- Calendar
- Drive
- Slack
- yeni Gmail/GitHub write capabilities
- başka app/service connectors
- plugin registry

### Gerekli mimari
- capability contract
- Core grant
- exact destination/scope
- import
- approval
- revocation
- secret handling
- provider/app identity

**API/connector mümkünse browser fallback’ten önce gelir.**

### Ek backlog
- Gmail multi-account
- personal/work account scope
- per-account authority
- Gmail archive/label/trash/reply/forward
- GitHub issue/PR writes

Hepsi ayrı authority/approval tasarımıyla.

---

## P11 — Self-Development / Dreaming
**Statü: KESİN GELECEK YÖN, LATER**

Hedef sınırsız “kendi kendine kod yazan AI” değildir.

Planlanan güvenli lifecycle:

`capability gap detection`
→ `proposal`
→ `isolated branch/worktree`
→ `implementation`
→ `tests/evals`
→ `security gate`
→ `user approval`
→ `registry/promotion`
→ `rollout`
→ `rollback`

### Dreaming / background reflection
Aday işler:
- yeni capability önerileri
- stale/contradictory memory tespiti
- skill health
- workflow evolution
- recurring failure patterns
- proactive proposal generation

Side effect yine Core + user approval ister.

### Agency Inbox
**Statü: OPSİYON / güçlü fikir**

Proactive:
- öneri
- draft
- fix
- capability gap
- stale memory/skill warning

ana sohbeti spamlemeden ayrı inbox yüzeyinde gösterilebilir.

---

# 3. EXTERNAL PRODUCT / ARCHITECTURE REFERENCES

Bu ürünlerin mevcut özellikleri zamanla değişebilir. **İlgili milestone başında güncel sürüm tekrar araştırılacak.**

---

## 3.1 AgentSpace
**Ana eşleşme:** V2.4 Work/Subagents

### Alınacak fikirler
- leader → specialist worker delegation
- persistent workers
- terminal/evidence visibility
- task board
- task ↔ branch ↔ Project/worktree ilişkisi
- targeted memory retrieval
- budget/usage visibility
- remote approvals
- task ownership

### Alınmayacak
- pixel-office görsel metaforu
- agent workspace’i ana ürün kimliğine dönüştürme
- authority’yi worker’a verme

JARVIS conversation-first kalır.

---

## 3.2 Vi3ecode
**Ana eşleşme:** Projects / Workflow / Remote

### Alınacak fikirler
- provider-independent environment
- Flow Studio benzeri dependency/workflow düşüncesi
- review gates
- acceptance checks
- multi-agent threads
- remote E2E pairing
- host authority

### Alınmayacak
- permissive auto-approve
- blanket/full permissions
- Core authority bypass

---

## 3.3 Avenox “İkinci Beyin” / avenoxbeyin
**Ana eşleşme:** V2.1 Advanced Memory

### Alınacak fikirler
- mechanism-enforced capture
- SessionEnd capture
- PreCompact safety net
- raw/daily/episodic → consolidated knowledge lifecycle
- explicit Rules
- Memory Doctor / Health
- history import
- consolidation

### JARVIS farkı
Obsidian/Markdown vault data model birebir kopyalanmayacak.

JARVIS typed structured memory kullanacak:
- Fact
- Decision
- Rule
- Lesson
- Preference
- scope
- provenance
- confidence
- supersession
- freshness

---

## 3.4 Avenox Fable Orchestration
**Ana eşleşme:** V2.4 model/subagent routing

### Alınacak prensip
En güçlü/scarce model her işe harcanmaz.

Routing:
- task type
- difficulty
- precision
- ambiguity
- verification need
- cost/latency

ile yapılır.

### Genel lane yaklaşımı
- judgment/spec/synthesis lane
- exploration/review lane
- defined implementation lane
- independent verifier lane

Exact model isimleri zamanla değişebileceği için milestone başında güncellenir.

### Değişmeyen JARVIS kuralı
**Orchestrator/model ≠ authority.**

---

## 3.5 Grok Bot
**Ana eşleşme:** V2.2 + V2.4 + V2.5 + Remote/Browser/Desktop later

**Kullanıcı Grok Bot’taki fikirleri çok beğendi ve JARVIS’e kesin yön olarak alınmasını istedi.**

### Kesin alınacak fikirler
- named persistent agents/bots
- role/job context
- background execution
- agent-to-agent handoff
- parallel work
- Skill kavramı
- Routine kavramı
- Teach a Task
- persistent work environment
- mobile tracking/takeover
- approval handoff

### Özellikle istenen ek yön
**Ajanların meslek/görev kimlikleri olması.**

JARVIS ileride:
- isimli
- uzmanlaşmış
- kalıcı rol bağlamı olan

bir AI ekibi gibi çalışabilir.

### JARVIS farkı
Grok Bot’taki shared-computer trust modelini KOPYALAMA.

Ayrı ajan:
- ayrı authority anlamına gelmez
- aynı computer’ı paylaşsa bile otomatik yetki kazanmaz
- Core grant olmadan action yapamaz

JARVIS worker/agent security boundary:
`identity → capability → target → scope → approval → execution`

### Cloud dependence
Grok Bot’tan UX/agent modelini alabiliriz; JARVIS Core’un local/provider-independent dayanıklılığı korunur.

---

## 3.6 Hermes
**Statü: REFERENCE IMPLEMENTATION ONLY**

### Alınabilecek fikirler
- persistent memory/skills
- staged approvals
- durable cron
- UNKNOWN_OUTCOME handling
- subagents/worktree isolation
- progressive disclosure
- quarantine/hash rescan
- MCP filtering
- profile/bot mode

### Alınmayacak
JARVIS deterministic Core authority/security modelinin yerine Hermes yaklaşımı konmaz.

---

## 3.7 Claude Desktop / ChatGPT Desktop (2026-09)
**Ana eşleşme:** V2.0 Projects UX + V2.1–V2.6 + P7/P8
**Kaynak:** "Claude ve ChatGPT masaüstü analizi" araştırması (2026-09-29; resmi help sayfaları, tek/ikincil kaynaklı iddialar orada işaretli). Ürün özellikleri hızla değişir; ilgili milestone başında yeniden doğrulanır (§13).

### Alınacak fikirler (milestone eşlemesi)
- V2.0 (Gate 6/7): her zaman erişilebilen Archived bölümü + unarchive; project sayfasında konuşmalar / instruction / dosyalar; instruction editöründe karakter sayacı, sınıflandırma seçimi ve görünür precedence satırı; dosya limit sayaçları ve yükleme öncesi red; retrieval provenance yalnız Core task ve approval preview'da; retire → purge iki adım, kalan kopyaları dürüstçe söyleyen onay metni; arşivli project için salt-okunur banner; archive/unarchive akışının paketlenmiş build'de E2E testi.
- V2.1: Pause ve Reset ayrımı; Core'da deterministik "hiç saklanmaz" kategori filtresi; memory kullanımında kaynak kaydı; "bastır" işlemi silme değil supersession.
- V2.2: yeni skill/connector yetenekleri kullanıcı açana kadar kapalı; secret'lar Windows Credential Manager / DPAPI'de.
- V2.3: kaynak allowlist'i kullanıcıda; bütün web içeriği untrusted.
- V2.4: sandbox/capability sınırı ile approval politikası iki ayrı Core nesnesi; en az yetkili araç önce (connector > browser > ekran).
- V2.4A: risk sınıfları (exfiltration, credential probing, destructive) yalnız tavsiye etiketi.
- V2.5: aktif task sınırı; onay bekleyenler için kuyruk; bilgisayar kapalıyken kaçan çalıştırmalar için açık missed-run politikası; konuşarak kurulan job'ın son spec'i kullanıcıya onaylatılır.
- V2.6: moddan bağımsız hard-block listesi (satın alma, kalıcı silme, kimlik/kart vb.); site/app + oturum kapsamlı izin; global hotkey Alt+Space olmaz; yakalanan ekran/pencere metni untrusted; kullanıcı devraldığında yakalama durur.
- P7: push onayında tam action fingerprint'i ve imzalı onay; QR ile eşleştirme.
- P8: Voice gelse de sesle approval verilmez.

### Alınmayacak
- AI reviewer'ın kullanıcı adına onay verdiği modlar (Claude "Automatically approve", ChatGPT "Approve for me").
- Onayı tamamen kaldıran modlar (Claude "Skip all approvals", ChatGPT "Full access").
- Connector tool'larının onaysız çağrılması (Claude Research modu).
- Uygulama çapında kalıcı "Always allow".
- Diff'in onaysız otomatik uygulanması (auto-apply).
- Egress kuralının web fetch/search/MCP'yi kapsamaması.
- Chat taşınınca instruction/dosya context'inin sessizce devralınması.
- Cascade eden kalıcı project delete; geri dönüşü olmayan archive.

### Salih kararları — V2.0 Gate 6 (2026-09-29/30)
- **Gate 6B ek kapsam (2026-09-30):** üst çubukta çekirdeğin soluna proje seçici + "+ Yeni sohbet" butonu. Fare öncelikli: Salih kısayol kullanmaz, hiçbir özellik kısayola bağlı olamaz. Proje seçmek onun en son arşivlenmemiş sohbetini açar (yoksa yeni sohbet); "Tüm projeler" girdisi Projects sayfasını açar; arşivli projeler seçicide gizlenir. 6B'nin geri kalanı planlandığı gibi: instruction editörü (x/2000 sayaç, PUBLIC/INTERNAL sınıflandırma, görünür precedence satırı); dosya paneli (Active x/20, Retained x/40, karakter x/2.000.000, FileDialog ile ekleme, retire/restore, purge yalnız retired satırlarda, dosya adı yazarak onay + dürüst kalıntı metni).
- **Gate 6C "Projeye bağla" (2026-09-29):** bir Global sohbeti projeye taşı; yalnız tek yönlü; hedef mevcut proje ya da akış içinde yeni oluşturulan proje; önizleme + açık onay. Gate 0 "owner sabit / V2.0'da taşıma yok" kuralına dar, adlandırılmış istisnadır; kural aksi halde geçerli kalır. V2.1 "tüm projelerin hafızası"nı kullanmış sohbetler için guard içerir (kesin guard 6C'de tasarlanır).
- **Kalan V2.0 sırası:** 6B → 6C → Gate 7 (E2E + paketlenmiş dogfood + rollback; archive/unarchive E2E dahil). Gate 6 UX tamamlanmadan release yok. **SUPERSEDED (2026-10-05):** güncel sıra aşağıda "Gate 6G / 6H planı"nda.
- **Purge kalıntısı (2026-09-30):** V2.0'da purge ARCHITECTURE §4.24'te uygulandığı gibi kalır: kanonik project-dosya deposu temizlenir; ona ulaşmayan uygulama düzeyi kopyalar (`pending_approvals` içindeki onay önizlemesi + `approval_requested` audit payload'ı, model yanıtları/sonuç snapshot'ları, konuşma geçmişi, sağlayıcılara zaten gönderilmiş içerik, WAL/yedekler) kalır ve purge diyaloğunda dürüstçe belirtilir. Bu karar Gate 7'yi bloklamaz.
- **Ertelenen (V2.0 gate'i değil):** purge'ün yukarıdaki uygulama düzeyi kopyaları da temizlemesi — ayrı, sonraki bir tasarım (audit append-only bütünlüğüne dokunur).

### Salih kararları — V2.0 Gate 6E görsel cila (2026-10-03)
- **Q5:** liste satırı ekleme/silme anlık kalır (satır animasyonu ve ListModel diff adaptörü yok).
- **Q6:** Projects yeniden adlandırma alanı yalnız "Yeniden adlandır"a basılınca görünür.
- **Q1–Q4 ve Q7–Q9:** preflight'ın önerdiği gibi kabul edildi (Q1 başlıksız sohbet adı için ayrı "New Chat" kaynağı; Q2 canlı dil değişiminde Bridge etiketlerinin yenilenmesi, ayrı commit C7; Q3 etkinlik notlarında çoğul düzeltmesi ve tr "mesaj"; Q4 6A–6D yüzeylerinde "sohbet"; Q7 klavye yolu; Q8 tr efor etiketleri; Q9 History arşiv bölümü kapalı başlamaz).
- **Dilimler:** C1 metin/çoğul/katalog, C2 token ve ortak parçalar, C3 üst çubuk kapsam grubu, C4 diyaloglar, C5 History + Projects listeleri, C6 model paneli, C7 canlı dil yenileme. Plan: `build/6e/PREFLIGHT.txt` (COMMIT SLICING).

### Salih kararları — V2.0 Gate 6F sol kenar çubuğu (2026-10-04)
- **L1:** üst çubuktaki kapsam grubu (proje seçici, sohbet değiştirici, "+ Yeni sohbet") kaldırılır. Üst çubukta logo + wordmark + slogan, çekirdek, model seçici, güç/dondur ve pencere düğmeleri kalır.
- **L2:** sol sütun (SummaryRail kartları + saat kartı) tek bir kenar çubuğu olur, yukarıdan aşağı: "Genel" satırı (sohbet sayısı); PROJELER başlığı + küçük "+" (yeni proje), etkin projeler satır olarak (ad kısaltılır + ipucu, sayı rozeti; 1920'de ~5, 1100'de 3 satır, sonra kaydırma) ve "Arşivlenmiş projeler (n)"; ayraç; SOHBETLER başlığı + seçili kapsamın adı + küçük yuvarlak "+" (seçili kapsamda yeni sohbet; tam genişlikte satır yok); seçili kapsamın sohbetleri Bugün / Dün / Daha eski gruplarında, açık olan kenar işareti + dolguyla, başlıksız olan "Yeni Sohbet" sönük, ve "Arşiv (n)"; 2x2 küçük kart (Görevler, Odak, Plan, Hafıza; bugünkü kartlarla aynı veri ve tıklama hedefi, satır asla kesilmez); en altta ince saat şeridi (saat, tarih, saat dilimi/hava).
- **L3:** sohbet alanına başlık satırı: kapsam çipi > sohbet adı; "Projeye bağla" düğmesi (yalnız Genel sohbetlerde); "…" menüsü = Yeniden adlandır / Arşivle / Kalıcı sil… (bugünkü değiştirici bağlam menüsüyle aynı eylemler ve diyaloglar). Kenar çubuğundaki sohbet satırına sağ tık aynı menüyü açar.
- **L4:** davranış değişmez: proje seçmek 6B kuralını korur (en son arşivlenmemiş sohbeti, yoksa yeni sohbet); sohbet seçmek bugünkü değiştirici seçimidir; aynı bridge çağrıları ve diyaloglar; tek otorite Core; fare öncelikli (hiçbir özellik kısayola bağlı değil).
- **L5:** değişmeyen yüzeyler: sağ sütun, History sayfası, Projects sayfası, diyaloglar, model paneli.
- **Yerine geçtiği kararlar:** 6B/6D'nin üst çubuk yerleşimi (proje seçici + "+ Yeni sohbet" çekirdeğin solunda, 6D-a sohbet değiştirici) ve 6C'nin "giriş noktası yalnız üst çubuk seçici menüsü" kuralı; "Projeye bağla" girişi sohbet başlık satırına taşınır. 6B/6C/6D'nin davranış kuralları (seçimin sahibi değiştirmemesi, tek yönlü taşıma, önizleme + açık onay, arşiv/sil koşulları) aynen geçerlidir.
- **Plan:** `build/6f/PREFLIGHT.txt` (açık sorular Salih'te; dilimler COMMIT SLICING).

### Salih kararları — V2.0 Gate 6G / 6H planı (2026-10-05/06)

Kaynak: Salih kararları 2026-10-05/06 (G0 ile buraya taşındı; çalışma notu `build/6g/PLAN_DRAFT.txt` gitignored). Yalnız SON hâl yazılır: bir revizyonun yerine geçtiği madde, kimliği ve tarihiyle, yalnız geçerli kuralıyla durur. Referans: Claude desktop ekran görüntüleri; uyarlanır, birebir kopyalanmaz. Fare öncelikli: hiçbir özellik kısayola bağlı olamaz.

- **Zamanlama ve sıra (Salih 2026-10-05):** 6G ve 6H V2.0 içindedir, dogfood'dan ÖNCE. F8 (arama) ve F9 (başlık/menü) 6G'ye katlanmıştır (aynı yüzeyler; kenar çubuğu ve başlık iki kez yapılmaz). Sıra: F7 → CI gate6f (yeşil, run 37425225354) → 6G dilimleri (ilk K1 simge seti, hemen ardından K2) → 6H (otorite incelemesi) → CI → kurulum → dogfood. Yukarıdaki "Kalan V2.0 sırası" (2026-09-30) bunun yerine geçmiştir.
- **Mockup (Salih onayı 2026-10-05 18:10):** Design artifact v16 https://claude.ai/artifact/Nku1wTKQeX8SLRLcGM5UZ7 (panolar 1–13). Aynı mockup ile onaylananlar: Projects sayfası kenar çubuğundan açılır; proje sohbetinin sağ sütunu = "BU SOHBETTE" çalışma paneli üstte + kompakt Sistem durumu ("AKTİF İŞ AKIŞI" orada gizli); arama girişi kenar çubuğunun en üstünde.
- **SIZE (Salih 2026-10-05):** Salih JARVIS'i yalnız tam ekran (1920x1080) kullanır; tasarım/kabul hedefi tam ekrandır. Küçük boyutlar bozulmamalı (denetim örtüşmesi/kırpılma yok) ama cila gerekmez.
- **Efekt planı (Salih onayı 2026-10-05 19:42):** hareket yalnız gerçek durum/geçişte; 120–220 ms, OutCubic, sekme yok, <=4 px yol; ambient kapalı → hareket yok, renkler kalır. Efektsiz: onay kartı dikkat çekme, yıkıcı diyaloglar, sahte yazma, her tuşta arama sonucu, sayarak artan sayılar, hata metni, seçim onayları, sahte etkinlik. Tam tablo: claude.ai Doc "JARVIS 6F-F6 sonrası plan özeti", "Efekt planı" bölümü.

**Kenar çubuğu v2 (6G; mockup panoları 7–10)**
- **V1:** "Genel" kapsamı arayüzden kalkar. Projesiz sohbetler sabit bir listede, tarih gruplarıyla (Bugün / Dün / Daha eski; F6 daraltma kuralları) durur; liste seçili projeyle değişmez. Core/depo modeli değişmez (global/projesiz sahip) — yalnız arayüz adlandırması. **L3:** "SOHBETLER" başlığı yoktur; liste yalnız sabit ayırıcı ("Sabitlenenler") ve tarih ayırıcılarıyla bölünür.
- **N1 (V4/V4' yerine):** "Yeni sohbet" tam genişlikte bir satırdır, arama girişinin hemen altında, PROJELER'in üstünde; simgesi küçük, sade, soluk "+" (parıltı/daire yok). Boş yeni-sohbet ekranını açar (selamlama + büyük besteleyici) ve besteleyicinin altında proje seçici ("Proje seç ⌄": arama, "Proje yok", proje listesi, "Yeni proje…"). Varsayılan: kullanıcı bir proje sohbetinde/sayfasındaysa o proje, değilse yok. Sahip ilk mesaj gönderilince sabitlenir (sonra taşıma yok; projesiz sohbet için 6C tek yönlü bağlama geçerli). Projesiz sohbete ayrı klasör yok.
- **L2 (V2 yerine):** "PROJELER" metni Projects sayfasına bağlantıdır; satırın geri kalanına tıklamak proje listesini daraltır/açar (sağda chevron, kapalıyken sayı). O satırda "+" yok: yeni proje yalnız Projects sayfasındaki "Yeni proje" ya da yeni-sohbet seçicisindeki "Yeni proje…" ile oluşur.
- **T1 (V2 yerine):** proje satırları AĞAÇtır: proje satırına tıklamak O projenin sohbet listesini açar/kapar (satırlar, sağda zaman, üstüne gelince "⋯"); varsayılan hepsi açık; daraltma proje başına hatırlanır. Proje satırına tıklamak proje sayfasını AÇMAZ. Sabitlenmiş proje sohbetleri yalnız Sabitlenenler altında durur.
- **T2 (V2 yerine):** proje sayfasına proje satırının SAĞ TIK menüsünden ("Proje sayfasını aç", "Bu projede yeni sohbet", "Sabitle", "Arşivle") ve Projects sayfası kartlarından ulaşılır. Proje satırında üstüne gelince "⋯" yok (F7 kuralı).
- **T3:** proje ağacı ile Sabitlenenler/tarih listesi kenar çubuğu yüksekliğini paylaşır; her biri kendi içinde kayar.
- **T4:** proje satırlarında chevron ve klasör simgesi yok; proje adları ve sohbet adları sola hizalı başlar (girinti yok); proje satırları yarı kalın; kapalı satır sayıyı gösterir.
- **DRAG (Salih 2026-10-05):** sabit sırası F6 olduğu gibi. Kenar çubuğundaki projeler = fareyle sürükleyerek elle sıra (bırakma çizgisi, sıra prefs'te sabitler gibi; yeni proje en üste). Proje sohbetleri = elle sıra YOK (etkinliğe göre en yeni üstte; üstte tutmak için sabitle).
- **V3:** SABİTLENENLER (eski SABİTLENDİ): projesiz ve proje sohbetlerinin tek ortak sabit listesi; F6 sabitleme kuralları (en çok 10, sıra, sürükleme) değişmez. **V10 (F6'nın soluk proje etiketinin yerine):** satırlarda varsayılan proje etiketi yok; baştaki simge türü söyler (klasör = proje sohbeti, konuşma balonu = projesiz sohbet; sabit glifinin yerine). Üstüne gelip ~0,5 s beklemek satırın SAĞINDA küçük bir bilgi kartı açar: tür + proje adı, başlık, proje talimatı özeti (proje sohbeti) ya da ilk kullanıcı mesajı (projesiz), proje dosya adları/sayısı, son etkinlik. Yalnız gerçek veri; bu gate'te model üretimi özet yok (o bir model çağrısıdır, ayrı karar).
- **V9:** arşivlenmiş projeler/sohbetler kenar çubuğunda GÖSTERİLMEZ; yalnız Projects sayfasında (Arşiv sekmesi). Kenar çubuğu proje listesinin altında "Tüm projeler"i tutar.
- **P1 (kenar çubuğu çubuğu, 2026-10-05 16:54; aşağıdaki 6G P1'den ayrı):** Sabitlenenler'in altında, projesiz sohbetlerin üstünde saydam, etiketsiz bir çubuk: tıklamak tüm projesiz sohbetleri gizler/gösterir (üstüne gelince chevron; gizliyken "n sohbet"); durum hatırlanır.
- **L4 (V5/V5'/V5'' yerine):** kenar çubuğunun İÇİNDE, aramanın üstünde bir üst satır: solda Geri / İleri (gezinme geçmişi: önceki görünüme dön, geri gittikten sonra ileri; mümkün değilse kapalı); sağda iki kenar çubuğu düğmesi — "gizle, sohbet genişlesin" (kenar çubuğu gizli, sohbet alanı sola büyür) ve "gizle, yer boş kalsın" (kenar çubuğu gizli, yeri boş kalır). Kenar çubuğu gizliyken aynı dört düğme içerik satırının sol üstünde küçük bir çubukta durur ("sohbet genişlesin": orta panel o çubuğun hemen altından başlar; "yer boş kalsın": çubuk boş alanda durur). Çubuğun üstüne gelmek kenar çubuğunu kaplama olarak gösterir (tıklamadan kullanılabilir; işaretçi ayrılınca kısa bir gecikmeyle gizlenir). Etkin düğmeye tıklamak yerleşik kenar çubuğunu geri getirir. Durum hatırlanır.
- **V6:** döşeme kipi: kenar çubuğu YARI genişliğe iner, listeler gizlenir, 4 döşeme tek sütunda yüksekliği doldurur; tekrar tıklamak geri getirir; durum hatırlanır. **K1 (saat satırı, 2026-10-05 17:57):** saat satırı geri gelir (saat, tarih, "UTC+03:00 · Hava yok"); döşeme kipi düğmesi sütundan çıkar ve sol sütunun ALTINDA küçük bir düğme olur (alt bilgi satırı, solda; slogan onun sağına kayar).
- **D1:** her sohbet satırı (Sabitlenenler, projesiz liste, proje ağaçları) küçük (6 px) bir durum noktasıyla başlar: boşta = sönük (zor görünen halka), ÇALIŞIYOR = mor ve yanıp sönen, kullanıcı başka sohbetteyken BİTTİ = o sohbet açılana kadar yeşil. Yalnız gerçek çalışma durumu (çalışan çağrının/görevin sahibi olan sohbet); sahte durum yok. (V2.1 "okunmadı" fikrinin yalnız tamamlanma alt kümesi.) **D2:** BİTMİŞ (yeşil) satır ayrıca yumuşakça parlar (yeşil ton + yavaş nabız); satıra tıklanınca/açılınca parıltı ve yeşil nokta kaybolur.
- **Arama (F8, Salih 2026-10-05; 6G'ye katlandı):** S1 giriş = kenar çubuğunun en üstündeki satır ("Sohbetlerde ara" düğmesi, büyüteç); bir tık satır içi alana çevirir, X kapatır ve normal listeyi geri getirir; hiçbir şey kısayola bağlı değildir (Esc/Enter yalnız ek). S2 sorgu etkinken iki bölüm: "BAŞLIKLAR" (ad eşleşmeleri, kesin gereklilik) ve "İÇERİKLER" (mesaj metni eşleşmeleri, ~80 karakterlik özet, vurgulu); başlıkla eşleşen İÇERİKLER'de tekrarlanmaz. S3 kapsam: tüm projeler + projesiz sohbetler; her sonuç soluk kapsam etiketiyle; tıklamak sabit gibi kapsam + sohbet seçer; silinmiş/mezar taşlı ASLA görünmez; arşivli yalnız mevcut arşiv yolu açabiliyorsa "Arşivde" işaretiyle (preflight karar verir). S4 eşleşme büyük/küçük harf duyarsız ve Türkçe-bilinçli (I/ı/İ/i; tek başına `str.lower()`/`casefold()` yetmez, testle kanıtlanır); en az 2 karakter, yazarken debounce; sonuç sınırı (örn. 50) ve dürüst "daha fazla sonuç var". S5 içerik araması = mevcut saklı sohbet durumunun salt-okunur, istek üzerine taranması (indeks yok, şema değişikliği yok, yeni tablo/yazıcı yok, `SCHEMA_VERSION` 10); UI iş parçacığı dışında, arayüz donmaz. S6 depo gerçeği dürüst: son 160 tur tam metin, eski sıkıştırılmış turlar 240 karakterlik özet (özet eşleşmesi "eski özet" işaretli), atlanmış turlar aranamaz. S7 İÇERİKLER sonucu sohbeti açar (tam mesaja kaydırma sonra). S8 arama salt okunurdur; kapsam bağlamdır, yetki değildir; testler yalnız geçici depo. S9 boş durumlar: "Sonuç yok"; tarama sürerken sessiz "Aranıyor…" (yalnız gerçek durum). Seviye: app/bridge okuma yolu → tam suite. Kapsam dışı: FTS5 indeksi, proje dosyalarında arama, sonuca kaydırma.

**Düzen ve düzenleme kipi (6G)**
- **L1 (V8 yerine):** sol sütun, orta panel ve sağ sütun TEK bir üst çizgiyi paylaşır, üst çubuğun izin verdiği kadar yukarıda (marka/denetimlerin hemen altında; 1920'de ~104 px). Orta panelin üstü çekirdek sanatına yumuşakça geçer (sert üst kenar yok). Sohbet başlığı + "⋯" panelin üstünde; İLK MESAJ çekirdek sanatının altından başlar.
- **Besteleyici alt satırı (Salih 2026-10-05 19:19, seçenek A):** besteleyicinin alt satırı sohbet panelinin İÇİNDE kalır; sohbet panelinin alt kenarı yan sütunların altından ekranın altına uzanır (1920'de ~30 px), boş bant yok. Tüm görünümlerde geçerli.
- **E1' (E1 yerine):** mevcut düzenleme kipinde kullanıcı sol ve sağ sütunun iç kenarını sürükleyerek genişliği ayarlar (canlı px etiketi); genişlik yalnız DARALABİLİR: bugünkü varsayılan en büyüktür (1920'de sol 340 / sağ 386), en küçük sol 240, sağ 300; genişlikler prefs'te kalır. Döşeme kipi genişliği her zaman VARSAYILAN sol genişliğin yarısıdır (özel genişliği yok sayar).
- **E2 (E1 yerine):** TEK düzenleme kipi düğmesi: mevcut TopBar "editLayoutTrigger" (kaydırıcı simgesi) Ayarlar paneline (sol üst logodan açılır) taşınır, başlığında X kapatma düğmesinin hemen SOLUNA. TopBar kopyası ve Ayarlar içindeki yinelenen "Edit panels" eylemi kalkar. Düzenleme kipi açıkken küçük bir "Düzenleme modu · Bitti" hapı görünür kalır (sol üst, markanın yanında); Ayarlar'ı yeniden açmadan kip bitirilebilir.

**Sohbet başlığı ve menü (F9 6G'ye katlandı)**
- **H1:** "Projeye bağla" düğmesi sohbet başlığından kalkar. Bağlama YALNIZ sohbet menüsünden (sağ tık / satır "⋯"), yalnız projesiz sohbetlerde. 6C kuralları değişmez (tek yönlü, önizleme + açık onay, global-hafıza-anahtarı guard'ı).
- **H2 + V7:** projesiz sohbetin başlığında kapsam satırı yoktur. Proje sohbetinde sol üstteki proje adı daha yukarıda ve daha soluk (ikincil) durur; tıklamak o projenin sayfasını açar.
- **H3:** ConversationMenu referansa göre yeniden biçimlenir: her öğede simge, ayırıcılar, yıkıcı "Sil" en sonda kırmızı. Referanstan ALINMAYANLAR: "Change project", "Remove from project" (Gate 0 owner-sabit kuralıyla çelişir; 6C dar, tek yönlü istisnadır), "Open in new window" (çoklu pencere kapsam dışı); "Mark as unread" V2.1'de kalır.
- **M1:** başlık "⋯" ile kenar çubuğu satırının "⋯"/sağ tıkı AYNI ConversationMenu içeriğini açar, ama yalnız tıklanan açılır: satır menüsü satıra bağlanır (sohbeti açmaz/değiştirmez, F7 Q8), başlık menüsü başlığa; biri açılınca diğeri kapanır. **Mevcut hata (F7 doğrulaması 2026-10-06), bu dilimde düzeltilir:** açık sohbetin kendi satırından menüyü açmak başlıktaki "…"yi de yakıyor.
- **N2:** ekranda açık olan sohbet silinir ya da arşivlenirse (kendi menüsünden, kenar çubuğundan veya başka yerden) görünüm Yeni sohbet ekranına (boş sohbet + proje seçici, pano 7) geçer; asla ölü/boş bir sohbet görünümünde kalmaz. 6G kenar çubuğu/menü dilimi.

**Besteleyici, model ve kullanım (6G)**
- **M2:** gönder tuşu üzerine gelme/basma efekti alır (parıltı, kalkma, halka). JARVIS çalışırken aynı yerde mevcut DURDUR denetimine dönüşür (amber kare + dönen halka, "Durdur" = çalışan çağrının bugünkü iptali); besteleyicinin altında sessiz bir "JARVIS çalışıyor…" satırı. Sahte ilerleme yok.
- **C1:** ekleme "+" ve izin kipi seçicisi besteleyici çubuğundan ÇIKAR, altındaki satırın SOLUNA gider: önce "+", sonra kip çipi ("Akıllı ⌄", mevcut üç kip ve metinleri). Model / Effort / kullanım aynı satırın sağında. Her besteleyicide (sohbet, yeni sohbet, proje sayfası).
- **U1 (M3 yerine):** üst çubuk kartı YALNIZ markayı gösterir (sağlayıcı soketi + "ANTHROPIC"/"OPENAI"); model adı yok, kullanım yok, tıklanmaz.
- **U2 + M3 çipleri:** besteleyicinin altında sağda iki mini çip "Model ⌄" ve "Effort ⌄" (sohbet ve yeni-sohbet besteleyicisi). Model mini menüsü: ayrı sağlayıcı/marka adımı yok; en üstte seçili model, sonra marka başlıkları altında modeller (ANTHROPIC, OPENAI · CODEX) — tek tık modeli VE sağlayıcıyı değiştirir; "Varsayılan" etiketi; seçilende onay işareti; "Daha fazla model ›" alt menüsü eski modelleri (bugünkü `showOldModels` kümesi) markaya göre gruplu, seçilebilir listeler. Model listeleri CLI'ların bildirdiği gibi kalır (sabit katalog yok). Effort mini açılır penceresi (Claude tarzı): başlık "Effort <seviye>", Daha hızlı ↔ Daha akıllı izi SÜRÜKLENEBİLİR bir sürgüdür (Salih kararı, 2026-10-06: tutamacı fareyle sürüklenir ve seçili modelin desteklediği duraklara oturur; bir durağa ya da ize tıklamak oraya atlar; "Effort <seviye>" başlığı canlı güncellenir; "Önerilen" işareti kalır) — seçili modelin desteklediği her seviye için bir durak (Claude low..max, Codex modele göre ultra dahil), bilinen önerilen/varsayılan seviyenin altında "Önerilen"; sağlayıcı başına, bugünkü gibi kalıcı. Mevcut sağlayıcı/model/effort anlamı, göçü ve kalıcılığı değişmez; bu bir arayüz yeniden yerleşimidir. Kullanım düğmesi Model ve Effort çiplerinin sağında: yay ve ortadaki metni 5 saatlik pencerenin KULLANILAN yüzdesini gösteren yuvarlak halka (tek renk, değerle değişmez); hafif hareketli hale. Sağlayıcı bir şey bildirmediyse halka boş, "–".
- **U3:** kullanım düğmesi Claude'unkine benzer bir açılır pencere açar: "Plan kullanım limitleri · <sağlayıcı>": 5 saatlik ve haftalık satırlar (kullanılan %, sıfırlanma zamanı, çubuk). Yalnız sağlayıcının bildirdiği değerler; tahmin yok; bulut kredisi / döküm satırı yok. Bağlam penceresi bloğu V2.1'dedir (Salih 2026-10-05; aşağıda V2.1).

**Projects UX (6G; mockup önce, onaylandı)**
- **P1 (yeni proje):** yeni proje bir açılır pencerede oluşur (sayfa değil), Projects sayfasındaki "Yeni proje"den ya da yeni-sohbet seçicisindeki "Yeni proje…"den (L2): ad (zorunlu) + isteğe bağlı "Ne yapmak istiyorsun?" metni projenin talimatı olur (mevcut alan, x/2000). Klasör bölümü 6H'ye göre. Proje her zaman klasörsüz oluşturulabilir.
- **P2:** Projects sayfası kart ızgarası: sekmeler Projelerin / Arşiv, arama, sıralama, "Yeni proje"; kartta ad, sabit işareti, talimatın ilk satırı, son etkinlik; kart "⋮": Sabitle/Kaldır, Düzenle, Arşivle, Sil (yalnız 6D-b'nin izin verdiği yerde). Proje sabitleri: sohbet sabitleri gibi prefs deposu.
- **P3:** proje iç sayfası: ad başlığı, "Bu projede yeni sohbet" besteleyicisi, Sabitlenen + Son sohbetler listesi; sağ sütun satırları kipler açar: Talimatlar (Düzenle — mevcut editör), Dosyalar/Bağlam (mevcut dosya paneli), Klasör (6H). Hafıza satırı V2.1'de, Zamanlanmış satırı V2.5'te gelir — öncesinde sahte/boş gösterilmez.
- **W1/W3 çalışma paneli (ilk sürüm 6G):** düz/projesiz sohbette hiç görünmez; yalnız PROJE sohbetlerinde, yalnız gerçek veri: kullanılan proje talimatı, kullanılan proje dosyaları, bağlı klasör (6H sonrası), bu sohbette gerçekten kullanılan connector/yetenekler. Daraltılabilir; V2.4'e kadar Progress/Outputs bölümü yok. (W2 tam sürüm V2.4'te.)

**Alıntılama v2 — kart akışı (Salih kararı, 2026-10-06; 6G sohbet alanı dilimi; pano 13 "Alıntılama", Güncel plan artifact v12). Gönderildi: G6Q1 (+ G6Q1T tr-TR kataloğu) `main`de.**
Salih kararı (2026-10-06): chip/açılır-liste öğeleri (Q3/Q4/Q7) ve Q2'deki İptal/Kaydet satırı YERİNE kart akışı. Nihai küme:
- **Q1:** alıntılanabilir bir mesajda metin seçmek seçimde TEK yüzen "Alıntıla" düğmesi gösterir (bugün "Quote selection"); bütün mesaj için üstüne gelince "Alıntıla" eylemi kalır. (değişmedi)
- **Q2' (alıntı açılır penceresi):** üstte sönük alıntı önizlemesi (solda çubuk, en çok 2 satır), TEK yorum satırı, sağında ataç + ekle simgesi; Enter ekler, Esc kapatır; resim küçük görselleri girişin altında. İptal/Kaydet satırı yok.
- **Q3' (besteleyici kartları):** alıntı başına BİR kart: harf + yorum (yorum yoksa ilk alıntı satırı, sönük), varsa görsel küçük resmi, üzerine gelince X; karta tıklamak ayrıntı penceresini açar; kartlar sarılır ve YUKARI doğru büyür; 2+ alıntıda "Birlikte / Ayrı yanıt" anahtarı.
- **Q5 (işaretler):** değişmedi — kaynak mesajda her alıntılanmış aralığın sonunda harf işareti + ince noktalı alt çizgi (yalnız taslak; mesaj gönderilince ya da alıntı kaldırılınca temizlenir).
- **Q8 (gönderilen mesaj):** alıntı başına harf, sönük alıntı parçası (solda çubuk; uzunsa daraltılır, "Devamını göster / Daha az"), altında normal metin yorum ve görselleri; en sonda ortak mesaj.
- **Q9:** karta ya da işarete tıklamak ayrıntı penceresi açar (başlık türe göre, X; solda çubuklu tam alıntı metni, altında yorum, alıntının görselleri/dosyaları küçük resim + önizleme); düzenleme (kalem) ve "Alıntıyı sil" burada (eski Q6 düzenleme kipi buna katıldı).
- **Q10:** tür etiketi: yorumlu kart "Yorum" (kart yorumu gösterir), yorumsuz kart "Alıntı" (kart alıntı metnini sönük, çubuklu, kısaltılmış gösterir); ayrıntı penceresinin başlığı türü izler.
- **Q11:** alıntı seçimi tam sözcüklere oturur (yarım seçilen sözcük bütün alınır).
- Eski Q2 (İptal/Kaydet satırı), Q3 (tek "N alıntı" çipi), Q4 + Q7 (çip üstü ayrıntı listesi) ve Q6 yerine geçildi; geçersizdir.
- JARVIS model yükü harfli, etiketli gruplarını korur (alıntı / yorum / görseller / ortak mesaj ayrı parçalar). Claude Code'un düz metin biçimi incelendi, BENİMSENMEDİ.
- Korunanlar: harfler A, B, C (mevcut grup etiketleri, arka uç etiket değişikliği yok); yorum = mevcut grup notu; görseller = mevcut grup ekleri; alıntılar yalnız bağlamdır, asla yetki değildir; gönderilen mesaj harfli dosyaları tutar; fare öncelikli.

**Uzun yapıştırma (Salih 2026-10-06; 6G sohbet alanı dilimi, alıntılama v2'nin yanında). Gönderildi: G6LP `main`de.**
- **LP1:** besteleyiciye çok uzun metin yapıştırmak girişi doldurmaz: varsayılan olarak girişin üstünde bir "yapıştırılmış metin" kartı olur (.txt eki gibi): ilk satırlar soluk önizleme, "YAPIŞTIRILDI" etiketi, sağ altta kalem düğmesi. Birden çok uzun yapıştırma = birden çok kart. Yazmak asla tetiklemez, yalnız yapıştırma.
- **LP2:** kalem kartı girişin içinde (imleçte) normal metne çevirir; kart kaybolur.
- **LP3:** eşik (varsayılan, ayarlanabilir, tek sabit): >= 2.000 karakter YA DA >= 30 satır. Daha kısalar bugünkü gibi.
- **LP4:** gönderince yapıştırılan metin mesajla mesaj düzeyi bağlam olarak gider (eklerle aynı yol, yazılan istekten ayrı, asla yetki değil); gönderilmiş mesaj aynı kartı gösterir, tıklamak salt-okunur önizleme açar. Mevcut yapıştırma sırası korunur: panodaki dosya/görseller önce eklenir (`bridge.attachClipboard`), sonra bu kural.

**Üst çubuk: dondurma tuşu (6G)**
- **S2 + dondurma kaplamaları (Salih 2026-10-05/06):** dondurma denetimi kaplanabilir olur (yeni ayar, durum göstergesi gibi). V2.0'da: kaplama 1 = bugünkü PowerControl; kaplama 2 = onaylı yeniden tasarlanmış tuş (Design pano 11 "Mechanism"; tuşun altındaki "Dondur" yazısı kalkar; hale statik, K2). Görsel durum yalnız `bridge.executionState`'i izler (asla tıklamayı değil): running = açık, freeze_requested = hareket/yarım, frozen = kapalı; devam ettirme yine onay ister. Yetki/anlam bugünküyle aynı. Salih kararı (2026-10-06): VARSAYILAN kaplama 1'dir (bugünkü PowerControl görünümü); kaplama 2 V2.0'da isteğe bağlıdır. Kaplama 3–4 de V2.0'da gönderildi (Salih 2026-10-11 A-2/A-3 KABUL; önceki plan V2.1'di).

**Uygulama taraması kalite dilimleri (2026-10-05, 63 QML dosyası salt-okunur; tam liste Doc "Uygulama taraması: eksikler ve düzeltmeler")**
- **K1:** her unicode glif simgesinin yerine çizilmiş simge seti (bugün tofu: pencere denetimleri, panel ✕, PanelColumn satır simgeleri ✦ ➶ ◷ ⁘ ◈ ⚑ ⟳, NavMenu, Safety/Approval/Attention glifleri, IconBox). 6G'nin İLK dilimi (yeni arayüz bunu kullanır).
- **K2:** hareket sözleşmesi: her süre `Theme.dur()` kapısından (sayfa geçişi, sütun genişlikleri, basma ölçekleri, onay giriş/çıkış, akış kaydırma, Collapsed chevron…), yol <=4 px (Main sayfası 8 px, PanelColumn 10 px, FeedView 8 px düzelir), Freeze halesi statik, ayarlar küresi sonsuz dönmez, yalnız girişi olan yerlere çıkış geçişi, her yerde tek HoverTip (PowerControl stok ToolTip, IconButton/TopBar satır içi ipuçları). K1'in hemen ardından. JarvisCore boşta dalgası (23 s'de bir) ve boşta yörünge paketi kalır ama AYRI, daha sakin bir görünüm alır (daha soluk, daha yavaş): boşta yaşam asla gerçek bir "gönderildi/başarılı" olayı gibi okunmaz; gerçek olaylar belirgin kalır (Salih 2026-10-05).
- **K3:** dürüst göstergeler: CANLI rozeti yalnız gerçek yakın olay/çalışmada; "0/4" görevsiz gizli ya da kaldırılır; ham kodlar → sözcük (kesin alan yanında kalır); Türkçe başlıklar; insan tarihleri. 6G dilimleriyle birlikte.
- **K4:** tek MenuSurface/MenuRow + diyalog stili (yarıçaplar, satır yüksekliği 36, opak menüler, `Theme.scrimOpacity`, odak, Esc). F9/M1 menü işiyle birlikte.
- **K5:** akış: akış imleci (yalnız gerçek akışta), kopyalama onayı, tek JarvisMark avatarı (akış/son hâl uyuşmazlığı), doğrula/düzelt'te yeni girişte bütün geçmişin yeniden solması, sakin onay girişi + makbuz geçişi.
- **K6:** sayfalar: CommandPreset dolgusu ("Agent capacity Unavailable", "Voice/Calendar N/A", sahte dolu halkalar) kalkar; Tasks/Memory/Automations/History ham metin + boş durumlar; SummaryPeek düzeltmeleri (besteleyiciyi örter, Odak gözatmasında yanlış sayı, ölü görünen satırlar); selamlama yenilemesi; komut paleti düzeltmeleri. 6G'nin sonunda, 6H'den önce.

**6H — Proje klasör bağı (otoriteye duyarlı; ek tablo; tam suite + ChatGPT otorite incelemesi)**
- **K1 (6H):** açılır penceredeki klasör bölümü: "Klasöre bağla" → Göz at (var olan klasör) YA DA [ ] Klasör oluştur; oluştur işaretliyse etkin bir [ ] Masaüstünde kutusu: işaretli → `Desktop\<proje adı>` (gerçek Desktop known-folder çözülür, OneDrive yönlendirmesine duyarlı); işaretsiz → varsayılan ortak yer `Documents\JARVIS Projeleri\<proje adı>` (Salih bunu Claude'a bıraktı). Klasörsüz geçerli bir seçimdir.
- **K2:** klasör bağı BAĞLAMdır, asla yetki değildir: her dosya okuma/yazma/oluşturma yine Core politikası ve onayından geçer. Bağlamak tek başına hiçbir yeteneği genişletmez.
- **K3:** klasörsüz proje + klasör gerektiren bir sohbet: JARVIS onay kartıyla sorar (oluşturacağı tam yolu gösterir), yalnız onaydan sonra oluşturur, sonra bağlar. Mockup v16'daki kart: "Masaüstünde oluştur" anahtarı + "Var olan klasörü seç…"; dosya kaydetme ayrı bir onay olarak kalır.
- **K4:** depo: yalnız ek tablo (`SCHEMA_VERSION` 10'da kalır, ALTER yok); bağı kaldırmak klasörü asla silmez.

**F7 doğrulama takipleri (2026-10-06):** (1) ARCHITECTURE.md 6D-b silme metni F7'ye göre güncellendi (G0). (2) Menü hatası yukarıda M1 altında.

**[GEÇERSİZ 2026-10-11: mekanik lamba ve dondurma 3–4 V2.0'da gönderildi, A-2/A-3 KABUL] V2.1'e taşınanlar (Salih 2026-10-06):** mekanik lamba (üçüncü durum göstergesi kaplaması) ve dondurma tuşu kaplamaları 3–4; kullanım penceresinin bağlam penceresi bloğu zaten V2.1. Ayrıntı: V2.1 bölümü, "V2.0'dan taşınan arayüz kalemleri".

---

# 4. MASTER DESIGN PRINCIPLES — DEĞİŞMEMESİ GEREKENLER

1. **Core final authority’dir.**
2. Model, provider, agent, skill veya workflow authority değildir.
3. **Project ≠ agent ≠ provider ≠ model.**
4. Provider switch Project/history/memory kimliğini bozmaz.
5. Subagent authority miras almaz.
6. Skill authority değildir.
7. Workflow authority değildir.
8. Imported/web/file content trusted instruction değildir.
9. Approval exact action/scope/destination üzerinden verilir.
10. UNKNOWN_OUTCOME success/failure diye uydurulmaz.
11. Work completion = agent claim değil, evidence + acceptance checks.
12. Memory capture mekanizma olmalı.
13. Retrieval selective/task-targeted olmalı.
14. Memory supersession/freshness birinci sınıf olmalı.
15. Background execution durable ve restart-safe olmalı.
16. UI fake telemetry/fake progress/fake activity göstermemeli.
17. Native API/connector mümkünse visual automation’dan önce gelir.
18. Browser/Desktop workflow deterministic replay + verification hedeflemeli.
19. Named agents uzman olabilir ama Core’un altında kalır.
20. Kullanıcı en yeni açık kararı eski roadmap kuralını supersede eder.

---

# 5. ROUTING / AGENT MODELİ — HEDEF TASARIM

Uzun vadede:

```text
User
  ↓
JARVIS Conversation / Project
  ↓
Planner / Orchestrator
  ↓
Core authority check
  ↓
Named Specialist Agent veya ephemeral worker
  ├─ Researcher
  ├─ Engineer
  ├─ Reviewer
  ├─ Security Specialist
  ├─ Inbox Assistant
  └─ başka bounded specialist
  ↓
Skill / Tool / Connector / Browser / Desktop
  ↓
Evidence
  ↓
Verifier / Acceptance checks
  ↓
Core
  ↓
Result / Approval / Notification
```

### Decision Engine / Jev katmanı
JARVIS’in uzun vadeli routing mimarisinde bounded semantic kararlar için ayrı bir `DecisionProvider` katmanı planlanır.

Canonical ilişki:

```text
Deterministic Core hard rules
        ↓
Bounded candidate set
        ↓
DecisionProvider (Jev pilotu)
        ↓
route / score / classify / continue-retry-stop
        ↓
Generative planner / specialist / tool path
        ↓
Core authority + execution checks
```

Kurallar:
- Decision engine, planner veya agent kimliği değildir.
- Decision engine, Core authority’yi genişletemez.
- Generative model çağrısını azaltabilir/yönlendirebilir fakat doğrulama zorunluluğunu ortadan kaldırmaz.
- Model/tool routing ve Work loop kararları birinci sınıf hedeflerdir.
- Security/release authorization bounded decision modeline devredilmez.
- Jev ilk adaydır; mimari `DecisionProvider` soyutlaması vendor lock-in yaratmayacak şekilde tasarlanır.

### Agent tipi
İki tür bir arada olabilir:

#### Persistent Specialist Agent
- isim/rol
- görev domain’i
- bounded long-term context
- Project bağlantısı
- history/experience
- model-provider bağımsız kimlik

#### Ephemeral Worker
- tek bounded subtask
- kısa ömürlü
- sonuç/evidence döndürür
- kalıcı identity gerektirmez

Bu ayrım exact implementation aşamasında tasarlanacak.

---

# 6. “TEACH → SKILL → ROUTINE → AGENT” UZUN VADELİ MODELİ

Önerilen gelişim hattı:

```text
Kullanıcı işi yapar/gösterir
        ↓
Teach a Task
        ↓
Trace + evidence
        ↓
Reusable Skill
        ↓
Successful repetitions + Experience
        ↓
Routine / Automation
        ↓
Gerekirse persistent Specialist Agent ownership
```

Örnek:
- kullanıcı bir iş portalındaki günlük süreci gösterir
- JARVIS Skill üretir
- birkaç kez bounded/manual çalışır
- güvenilir olursa Routine’e bağlanır
- ileride belirli bir “Operations Agent” bu Routine’in sahibi olabilir

Her aşamada Core authority korunur.

---

# 7. REMOTE + BACKGROUND + PERSISTENT WORK ENVIRONMENT

Grok Bot ve Vi3ecode referanslarından alınan birleşik yön:

### Hedef
- Work laptop açık/kapalı durumlarına göre truthful state taşır
- mümkün olduğunda background runtime
- remote monitoring
- remote approvals
- mobile takeover
- persistent task environment

### Ancak
İlk Remote mimarisi Windows Core’a bağlıdır.

“Laptop kapalıyken her şey çalışır” sözü verilmez.

Cloud/remote worker eklenirse:
- identity
- authority
- secrets
- provenance
- action scope
- isolation
- audit

ayrıca tasarlanır.

---

# 8. OPTIONAL / OPEN ITEMS

Bunlar roadmap’e not edildi fakat exact ürün commitment değildir:

- native SwiftUI iOS client (PWA sonrası)
- cloud relay
- Face ID device-local approval gate
- Agency Inbox
- additional Reactor/UI skins
- Dark preset / Command Center V2 redesign
- advanced cloud worker runtime
- Google Web Research ayrı provider/tool hattı
- başka provider worker’ları
- deeper artifact/app generation UX

Milestone başlamadan önce yeniden değerlendirilir.

---

# 9. PROVIDER / MODEL / DEVELOPMENT BACKLOG

Bu bölüm ürün özelliği ile development agent stratejisini karıştırmamak için ayrıdır.

## Genel ürün prensibi
- Agent identity model identity değildir.
- Routing difficulty/precision/task type’a göre.
- Exact model isimleri kalıcı mimari kararı değildir.

## Daha önce konuşulan deneysel lane’ler
### GLM-5.3
**Statü: planlanmış ikinci engineering-worker deneyi; ürün dependency’si değil.**

Koşullar:
- orchestrator/transport alignment sonrası
- önce gerçek smoke task
- separate worktrees
- shared mutation yok
- 2–3 gerçek görevde test
- reliable ise promote

### DeepSeek vision
Later QA/review adayı; ana development lane değil.

### Kimi/NVIDIA ve DeepSeek V4 Pro
Önceki değerlendirmede OUT / aktif plan değil.

### Qwen/Alibaba / başka provider
Backlog/research lane; kullanmadan önce güncel doğrulama.

---

# 10. RELEASE / REVIEW GOVERNANCE FOR V2

### `/ultrareview`
- **#1:** V1 release candidate sonunda.
- **#2:** Projects + Memory + Skills + Work/Automation gibi V2 mimarileri oturduğunda, **V2 bitiş broad review**.
- **#3:** reserve; kritik refactor/security ihtiyacı dışında harcanmaz.

Bu hakların ürün sürümü/araç davranışı zamanla değişebileceği için kullanmadan önce güncel durum doğrulanır.

### Superpowers
Gerekli olduğunda özellikle:
- major architecture planning
- code review
- tricky debugging
- pre-commit/release verification

için kullanılacak; her küçük işte gereksiz yere kullanılmayacak.

---

# 11. EXPLICITLY DEFERRED / DO NOT SILENTLY REINTRODUCE

Aşağıdakiler zamanı gelmeden scope’a sessizce eklenmez:

- Voice
- Artifact/Data/Image engine
- Calendar
- Drive
- Slack
- yeni connector ecosystem
- self-development/dreaming
- remote/mobile
- cloud worker runtime
- major V2 visual redesign
- full autonomous agent authority

Bunların çoğu roadmap’te vardır; **deferred = cancelled değildir.**

---

# 12. REJECTED / DO NOT COPY

- AgentSpace pixel-office’ı JARVIS ana UI kimliği yapmak.
- Vi3ecode benzeri blanket/full permission veya auto-approve yaklaşımı.
- Grok Bot shared-computer trust boundary’sini security modeli olarak kopyalamak.
- Obsidian/Markdown-vault-first memory’yi JARVIS’in canonical memory modeli yapmak.
- Agent/model/provider’ı aynı kavram kabul etmek.
- Subagent’a inherited authority vermek.
- Skill/recorded workflow ile Core approval bypass etmek.
- Bütün memory’yi her prompt’a dökmek.
- “Agent done dedi” diye işi tamamlanmış saymak.
- Fake progress / fake telemetry / fake background activity.
- Browser/desktop içeriğini executable authority kabul etmek.
- Default olarak büyük bir agent swarm açmak.
- AI reviewer'ın kullanıcı adına approval vermesi (auto-approve / "Approve for me").
- Onay katmanını kaldıran "Skip all approvals" / "Full access" modları.
- Kapsamı action/destination'a bağlı olmayan kalıcı "Always allow".
- Bazı çıkış yollarını (web fetch/search, MCP) egress politikası dışında bırakmak.
- Project/chat taşımada context'i kullanıcıya göstermeden devretmek.

---

# 13. MILESTONE BAŞINDA YAPILACAK ARAŞTIRMA

Her ilgili milestone başlamadan önce referans ürünler yeniden araştırılacak:

- ChatGPT Projects / Memory / Work / Plugins
- Claude Projects / current agent capabilities
- AgentSpace
- Vi3ecode
- Avenox / avenoxbeyin
- Avenox Fable Orchestration
- Grok Bot
- Hermes
- ilgili provider/model/tool ekosistemi

Sebep: bu ürünlerin özellikleri ve API/plan davranışları hızlı değişebilir.

Referansların eski özelliklerini current fact gibi kabul etmeme.

---

# 14. MASTER PRIORITY SUMMARY

## Kesin ana sıra

1. **V2.0 Projects**
2. **V2.1 Advanced Memory + official daily-driver**
3. **V2.2 Skills + Experience + Teach/Workflow Learning**
4. **V2.3 Deep Research**
5. **V2.4 Work + Subagents + Persistent Specialist Agents**
6. **V2.5 Automation 2.0 + Routines**
7. **V2.6 Browser/Desktop 2.0 + Workflow Recording**
8. **Remote/Mobile**
9. **Artifact/Data/Image**
10. **Plugins/Connectors**
11. **Self-Development/Dreaming**

### Voice
Master listte tarihsel P8 olarak kalabilir fakat **kullanıcı yeniden istemeden aktif sıraya alınmaz.**

### Engineering order
Yukarıdaki ürün sırası master direction’dır. Milestone içindeki implementation sırası:
- dependency
- security
- risk
- value
- testability

nedeniyle optimize edilebilir.

---

# 15. EN ÖNEMLİ YENİ KOMBİNE VİZYON

JARVIS’in uzun vadeli yönü yalnız “iyi chatbot” değildir.

Hedef:

> **Project-aware, provider-independent, güçlü hafızalı, Skills/Experience ile öğrenen, Work içinde kanıt üreten, isimli uzman ajanları yönetebilen, güvenli Automation ve Browser/Desktop katmanlarına sahip, Core-authority kontrollü kişisel AI işletim sistemi.**

Ve ajan katmanının hedef vizyonu:

> **JARVIS’in altında isimli, uzmanlaşmış, meslek/görev kimlikleri olan kalıcı AI ekip üyeleri bulunabilir; fakat hiçbir ajan, model veya skill Core authority’nin yerine geçmez.**

Bu, Grok Bot’taki persistent coworkers fikri ile JARVIS’in mevcut güvenlik/authority mimarisinin birleşimidir.

---

# 16. SOURCE / CONTINUITY NOTE

Bu belge:
- önceki JARVIS roadmap/handoff kararları,
- 18 Eylül 2026 V2 referans kararları,
- AgentSpace / Vi3ecode / Avenox / Fable Orchestration kararları,
- 19 Eylül 2026 Grok Bot kararı,
- kullanıcının persistent “meslek/görev sahibi ajanlar” isteği

birleştirilerek hazırlanmıştır.

Bu belge **ürün karar defteridir**; canlı implementation state değildir.

Bir gün bu dosya yeniden ChatGPT/Claude/JARVIS’e verildiğinde:
1. önce bu kararları continuity source olarak kullan,
2. güncel ürün/repo/tool gerçeklerini yeniden doğrula,
3. daha yeni açık kullanıcı kararı varsa onu üstün tut,
4. eski roadmap’i sıfırdan yeniden tartışmaya açma,
5. ACTIVE_STATE/commit ilerlemesini bu dosyadan tahmin etme.

---

**Dosya sonu**
