# JARVIS Project Context

## Kaynak ve süreklilik

Bu proje, ChatGPT'deki **JARVIS PLAN** konuşmasının devamıdır.

- Kaynak conversation/thread ID: `6a7fb31b-1aec-83eb-876a-2d0a43460d36`
- Bu dosya ve `ARCHITECTURE.md`, yeni oturumların eski konuşmayı baştan okumadan güvenle devam edebilmesi için kalıcı proje hafızasıdır.
- Mimari kararlarda çelişki olursa en son kullanıcı onayı esas alınır; belirsiz veya yalnızca öneri olarak kalmış maddeler kesin karar sayılmaz.

## Kullanıcının amacı

Kullanıcı, ChatGPT ile doğal biçimde fikir alışverişi yapabildiği; aynı zamanda Claude Code, Codex, terminal, dosya sistemi, tarayıcı ve ileride bilgisayar kontrolü gibi araçları koordine ederek gerçek iş yapabilen kişisel bir JARVIS istiyor.

Öncelik yazılım geliştirmedir; uzun vadeli kapsam bilgisayardaki genel işleri de içerir. Deneyim tek komut verip sonucu saatler sonra alan kara kutu otomasyon değil, kullanıcıyla saatlerce birlikte çalışan, değişen isteklere uyum sağlayan ve önemli adımlarda kontrolü kullanıcıda bırakan bir çalışma ortaklığıdır.

## Kullanıcı tercihleri

- Açıklamalar doğal, net ve gerektiğinde adım adım olmalı; gereksiz teknik karmaşa kullanıcıya yüklenmemeli.
- Kullanıcı süreç boyunca ne olduğunu bilmek, seçenekleri görmek ve gerektiğinde yön değiştirmek istiyor.
- Sistem emin olmadığı şeyi biliyormuş gibi söylememeli.
- Varsayılan davranış yeni araç yazmak değil, mevcut araçları birleştirmek olmalı.
- Tek seferlik işler yeni kalıcı yetenek üretmemeli. Tekrarlanan görevler ikinci/üçüncü kullanımda yetenekleştirme önerisine dönüşebilir; son karar kullanıcıdadır.
- Yazılı sohbet merkezde olmalı; ses isteğe bağlı giriş kolaylığıdır.
- Maliyet/fiyat-performans çok önemlidir. Kaliteyi bozmayacak ölçüde token ve çağrı sayısı azaltılmalı, ay sonunda sürpriz fatura oluşmamalıdır.
- Ücretli API bağlantısı, para harcama, dış yayınlama ve geri alınması zor işlemler açık onay olmadan yapılmamalıdır.

## Mevcut çalışma ortamı

- Platform: Windows 11
- Proje kökü: `C:\Users\realk\Masaüstü\JARVIS`
- Proje bir Git deposudur; ilk başarılı live smoke checkpoint'i `main` dalında `bef77e7` (`fix: preserve approved clarification in Claude payload`) üzerinden alınmıştır.
- İlk iskelette kullanıcı tarafından oluşturulan ve korunmaya devam eden klasör/dosyalar:
  - `Agent Core/agent_core.py`
  - `Connectors/base_connector.py`
  - `Memory/memory_store.py`
  - `Tools/tool_registry.py`
  - `Config/settings.py`
- Bu beş dosya başlangıçta boştu; artık standart `jarvis/` paketine yönlendiren uyumluluk sarmalayıcıları veya çalışan giriş dosyalarıdır. Kullanıcı tarafından oluşturulan yollar silinmemiştir.
- `ARCHITECTURE.md` ve `PROJECT_CONTEXT.md` bu devam oturumunda eklenmiştir.
- Başlangıç dosyaları artık ilk çekirdek uygulamayla doldurulmuştur. Ayrıca `contracts.py`, `Connectors/claude_code_connector.py`, paket `__init__.py` dosyaları ve konu bazında bölünmüş `tests/test_permissions.py`, `tests/test_cost_verification.py`, `tests/test_registry_persistence.py`, `tests/test_claude_connector.py`, `tests/test_storage_abstraction.py` ve `tests/test_windows_scripts.py` testleri eklenmiştir.
- B1 ile `jarvis/app.py` (composition root ve task girdi sınırı), yürütülebilir `jarvis/cli.py` ve `tests/test_app.py` ile `tests/test_cli.py` testleri eklenmiştir.

## Güncel ilerleme ve kaldığımız nokta

Bu devam oturumunda aşağıdakiler tamamlandı:

1. Kesin kararlar `ARCHITECTURE.md` dosyasına, proje bağlamı bu dosyaya kaydedildi.
2. Provider bağımsız Task, kapsam, Result, Permission, Session, ModelAdapter, MemoryStore, kullanım, iptal ve doğrulama sözleşmeleri standart `jarvis/` paketine taşındı.
3. Agent Loop'un tüm aşamaları SQLite checkpoint bırakan resumable state machine olarak uygulandı.
4. İzinler normalize hedef, işlem, connector/destination, veri sınıfı, task, session, fingerprint ve expiry ile bağlandı. SMART_AUTO yalnızca LOW + reversible işler için otomatik çalışır.
5. Task, pending approval, grant, checkpoint, karar, event ve aylık kullanım defteri şema sürümü/migration ve foreign key'lerle SQLite'a eklendi.
6. Aylık hedef ve sert üst sınır 20 USD'ye indirildi; preflight tahmini, hard-cap block ve açık override modeli uygulandı.
7. Claude Code connector allowed roots, derin secret redaction, outbound metadata, `--safe-mode`, strict/empty MCP, açık read-only tool listesi, no-session-persistence ve çağrı başı bütçe korumasıyla güçlendirildi. `--bare` ve permission bypass kullanılmaz.
8. Eski kullanıcı dosyaları silinmeden compatibility wrapper'a dönüştürüldü; `pyproject.toml`, README, CLI entrypoint ve Windows test betiği eklendi.
9. Toplam 81 yerel/mock test izin, maliyet, iptal, doğrulama, registry, migration, restart/persistence, storage abstraction, loop giriş aşaması koruması, Windows Python sürüm koruması, faturalandırma sınıfı politikası, approval-payload bütünlüğü, symbolic destination normalizasyonu, clarification egress sınırı ve Claude güvenlik davranışlarını kapsar. Hem doğrudan unittest komutuyla hem `scripts/test.ps1` üzerinden başarıyla geçmiştir.
10. Sağlayıcıdan bağımsız faturalandırma sınıfı (`BillingClass`) ve parasal gözlem durumu (`MonetaryState`) sözleşmeye eklendi; usage ledger şeması 3'e additive migration ile yükseltildi. Claude connector, harici model çağrısından önce yerel ve modelsiz `claude auth status --json` yoklamasıyla Claude Max/OAuth yolunu doğrular, metered kimlik bilgisi ortam değişkeni **varsa (boş değerle bile)** veya yol belirlenemezse çağrıyı başlatmadan durur ve model alt sürecine temizlenmiş ortam verir.
11. Bağımsız inceleme sonrası dört düzeltme uygulandı: (a) kalıcılaştırılmış geç aşama devamı artık faturalandırma ön kontrolünü atlayamaz, (b) ücretli API onayı aylık sert sınır kapısını atlamaz, (c) sınıflandırma bildirmeyen connector `UNKNOWN` kalır (fail closed), (d) yasaklı kimlik bilgisi ortam değişkeni boş değerle de engeller.
12. Permission scope alanları field-aware normalize edilir: filesystem/URL `target` ile symbolic `action`, `connector` ve symbolic/URL `destination` ayrılmıştır. `anthropic/claude-code` artık çalışma dizinine bağlı yerel path'e dönüşmez; approval scope ile outbound preview aynı destination'a bağlanır.
13. Claude outbound serialization default-deny hale getirildi. Yalnızca açık external preference allowlist'i serialize edilir; billing/runtime/control marker'ları local kalır. Canonical JSON encoding approval-time ve execute-time payload'ı persistence key-order farklarından bağımsız, byte-stable tutar. Kullanıcı clarification'ı ham preference olarak değil, redacted top-level `visible_assumption` alanıyla approval öncesinde görünür ve execute-time payload'da birebir aynı taşınır.
14. İlk gerçek, tamamen read-only Claude Code smoke çağrısı 16 Ağustos 2026'da başarıyla tamamlandı. Canonical kayıtlar: session `62f6940b-bdda-4787-a9a9-b8fcd631b1b2`, task `30c1b3fa-b0b5-4d93-abb3-e5bb59ec07b1`, permission request `f707aec1-12dd-5a01-80df-9d7df8ec59be`, grant `69aacf3f-0683-479f-8494-532da9c5ba92`. Task `COMPLETED`, verification `PASSED`; tek model çağrısı fixture'dan `LYRA-11` değerini doğru döndürdü. Grant session'a hatırlanmadı ve retry yapılmadı.
15. Smoke çağrısında approved/executed canonical payload SHA-256 `4a4f69785ead2a5b16a3447390261afc739881a616b6024a872468e2b85d2d12`, full prompt SHA-256 `8ef0c0046e5bfebeb4e2bedfeff9e0f5a13df6e59090dcdc3e15c2da4d63b74d` olarak eşleşti. Fixture SHA-256 `205847b7a4045a25e6f0883768c81e536d3884e611671098f190381515266a9c` ve tracked repository değişmedi. Billing `SUBSCRIPTION_INCLUDED`; 6 input token, 1464 output token, 1 call ve yaklaşık `0.0998965 USD` non-monetary reference cost kaydedildi. `monetary_cost_usd` null, monetary state `NOT_APPLICABLE` ve aylık parasal harcama `0 USD` kaldı.
16. Post-call çıktısını yazdıran geçici, repo-dışı smoke harness'i Windows CP1254 konsolunda Unicode `—`/`→` karakterlerini `ensure_ascii=False` ile yazarken hata verdi. Hata task tamamlanıp canonical sonuç kaydedildikten sonra yalnızca geçici reporter'ın console-print adımında oluştu; connector, Agent Core, audit veya ürün CLI davranışı etkilenmedi ve model retry edilmedi. Tracked `jarvis` kodunda bu reporter bulunmadığı için ürün koduna yapay bir düzeltme eklenmedi.

17. **B1 — yürütülebilir CLI yüzeyi ve composition root tamamlandı.** `jarvis/app.py` composition root'u (`create_app` enjeksiyon, `build_default_app` üretim), default-deny task JSON sınırı, komut cephesi, tam kalıcı onay/ret işleme ve deterministik redakte JSON zarfı eklendi; `jarvis/cli.py` yedi komutu, izin modu önceliğini, geriye dönük uyumlu status yolunu, sınırlı dosya/stdin okumasını ve çıkış kodu eşlemesini yürütür hale getirildi. Ayrıntılı karar kaydı `ARCHITECTURE.md` §4.1'dedir. 85 yeni test (`tests/test_app.py`, `tests/test_cli.py`) eklendi; toplam yerel/mock test sayısı 166'ya çıktı ve tamamı geçmektedir. Sunum katmanı redaksiyon politikası `jarvis/app.py` içinde tanımlıdır (connector'ın iç yardımcılarından bağımsız) ve serbest metne gömülü etiketli kimlik bilgilerini de kapsar; `json` ayrıştırıcısının kendi reddettiği girdiler (tamsayı basamak sınırı, aşırı iç içelik) geçersiz girdi olarak sınıflandırılır. Yürütülebilir komut akışları yalnızca sahte connector kullanır; gerçek bir `ClaudeCodeConnector` nesnesi yalnızca composition root'u doğrulayan kurulum testinde kurulur ve bu kurulum availability çağırmaz, auth/faturalandırma yoklaması yapmaz, model süreci başlatmaz. Her durumda `subprocess.run`, `subprocess.Popen` ve `shutil.which` sınırları dokunulursa hata verecek biçimde patch'lenir. B1 sırasında hiçbir canlı model, connector veya auth çağrısı yapılmamıştır; şema veya migration değişmemiştir.

18. **Context Retrieval milestone'u tamamlandı (yalnızca aynı oturumdaki tamamlanmış kararlar).** `MemoryRetrievalQuery`/`RetrievedMemoryItem`/`RetrievedContext` tipli sözleşmeleri ve `MemoryStore.retrieve_context()` eklendi; `Task.retrieved_context` tipli anlık görüntüsü `CONTEXT_RETRIEVAL` aşamasında bir kez alınır, mevcut task JSON'u ile kalıcılaşır, eski task JSON'u boş görüntüyle yüklenir ve onay sonrası resume yeniden sorgulamaz. Deterministik NFKC+casefold token kesişim skorlaması, 50 kayıtlık aday penceresi, skor→tarih→ID sıralaması, sınıflandırma uygunluk matrisi (kaynak task'tan geri kazanım, okunamayanda SECRET fail-closed) ve 480/240/80 alan tavanlarıyla 5 öğe / 4.000 karakter sert içerik sınırı uygulandı. Claude connector yalnızca `kind`/`summary`/`rationale`/`next_step`/`classification` alanlarını sabit "untrusted historical reference data" çerçeve notuyla serileştirir; kayıt/task ID'leri, skorlar, sayaçlar ve kesme bayrakları dışarı çıkmaz, boş görüntü payload şeklini değiştirmez, onaylanan ve yürütülen payload hash'leri getirilen hafıza varken de birebir kalır. CLI sınırı `retrieved_context` ve getirme metadata'sını reddeder; CLI zarfı getirilen içeriği göstermez. SQL şeması ve migration değişmedi. 42 yeni test eklendi (`tests/test_context_retrieval.py` ve mevcut sınır/depo/CLI testlerine odaklı eklemeler); toplam yerel/mock test sayısı 208'e çıktı ve tamamı geçmektedir. Hiçbir canlı model, connector, auth veya ağ çağrısı yapılmamıştır.

19. **Task-Aware PLAN milestone'u tamamlandı.** Statik altı satırlık plan, yalnızca güvenli yapısal task durumundan üretilen deterministik altı adımlı görev-farkında planla değiştirildi (`jarvis/planning.py`). Objective plana yalnızca normalize edilmiş metnin SHA-256'sının ilk 16 hex karakteri (`objective_ref=`) olarak girer; capability/action 64 kod noktasıyla, her adım 240 kod noktasıyla sınırlı normalize edilir. Getirilen hafıza planı yalnızca uygun `DECISION` öğe sayısı (en çok 5, mevcut sınıflandırma uygunluk matrisiyle) üzerinden etkiler; getirilen düz metin, ham objective/target/destination, context, preferences, clarification/assumption metni, ID'ler, zaman ve rastgelelik plana giremez. AgentCore `PLAN` aşamasında kanonik planı her zaman yeniden kurar ve kalıcılaştırır; PLAN'ı geçmiş task'lar kalıcı planlarını korur. Claude connector SECRET olmayan payload'a üst düzey `plan` alanını her zaman güvenli durumdan yeniden kurarak ekler; kalıcı/tahrif edilmiş `task.plan` outbound byte'ları değiştiremez, plan mevcut derin redaksiyondan geçer, onay önizlemesi plan alan adını içerir ve onay/yürütme byte-özdeşliği getirilen hafıza + plan varken de korunur. SECRET payload plan içermez; CLI plan girdisini reddetmeye ve planı yazdırmamaya devam eder. Ayrıntılı sözleşme `ARCHITECTURE.md` §4.3'tedir. 38 yeni test eklendi (`tests/test_task_aware_plan.py`; `tests/test_context_retrieval.py` içindeki plan ve payload-şekli testleri kanonik plana güncellendi); toplam yerel/mock test sayısı 259'a çıktı. SQL şeması ve migration değişmedi; hiçbir canlı model, connector, auth veya ağ çağrısı yapılmamıştır.

20. **SessionState / Progress Enrichment milestone'u tamamlandı.** Task kapsamlı, tipli ve yalnızca bilgilendirici `Task.progress` (`TaskProgress`: `completed_stages` + en çok 8 `ProgressIssue` + en çok 20 `ProgressArtifact`) ve `SessionState.latest_task_id` gezinme işaretçisi eklendi; her ikisi mevcut task/session JSON kolonlarında kalıcılaşır, SQL şeması 3'te kalır ve `MemoryStore` protokolü değişmez. `current_stage` otoriter `Task.stage`'dir; `next_stage` yalnız bilgilendirici deterministik doğrusal ardıldır ve hiçbir engeli atlamaz. Tamamlanan aşamalar `jarvis/progress.py` merkezî yardımcılarıyla, yalnız başarıyla biten aşama ardılına geçerken ve checkpoint'ten önce, sırayla/kopyasız kaydedilir; bekleme, hata, ret, iptal ve doğrulama hatası aşama tamamlamaz; terminal durumlar listeye girmez. Issue'lar sabit altı türle deterministik eşlenir; serbest metin taşımaz. Artifact sözleşmesi gelecekteki kullanım içindir (üretimde boş), etiketler gerçek `str` + NFKC + 240 kod noktası + kimlik-bilgisi elemesi + tekleme + 20 tavanıyla normalize edilir. `SessionState.next_step` sınırlı ve kimlik-bilgisi-güvenli hale getirildi (`str` değilse veya kimlik bilgisi taşıyorsa `Review the verified result.`). Eski task/session JSON'u güvenle yüklenir (doğrusal öncül türetimi; `FAILED/CANCELLED` hiçbir şey uydurmaz; bozuk değerler kapalı düşer). Status dışı CLI zarfı `schema_version=2` ile deterministik, redakte üst-düzey `progress` nesnesi taşır; plan alanı her zaman `build_task_plan(task)` kanonik yeniden kurulumudur ve kalıcı `task.plan` asla yazdırılmaz; `status` komutu birebir geriye uyumlu kaldı. Onay sonrası progress/plan mutasyonunun outbound byte'ları, hash'leri, preview'u ve fingerprint'leri değiştiremediği kanıtlandı. Ayrıntılı sözleşme `ARCHITECTURE.md` §4.4'tedir (bu düzenleme sırasında §4.3 eklenirken yanlışlıkla düşen `## 5` başlığı da geri getirildi). 55 yeni test eklendi (`tests/test_session_progress.py`; `tests/test_app.py`, `tests/test_cli.py`, `tests/test_registry_persistence.py`, `tests/test_storage_abstraction.py`, `tests/test_task_aware_plan.py` odaklı güncellendi); toplam yerel/mock test sayısı 316'ya çıktı ve tamamı geçmektedir. Hiçbir canlı model, connector, auth veya ağ çağrısı yapılmamıştır.

21. **H1 — Onay/Egress Attestation sağlamlaştırması tamamlandı** (commit `beb5e41`). Sağlayıcıdan bağımsız `EgressAttestation` (kanonik egress digest + kapsam + risk + connector-sahipli yan-etki profili + destination + faturalandırma sınıfı + cost gate + yerel kaynak sınırı + tool-grant manifesti) izin fingerprint'ine bağlandı; onay anı ve yürütme anı byte-özdeşliği zorunludur. Bağımsız Codex incelemesinin dört P1 bulgusu düzeltildi: (A) Claude yürütme yolundan geniş dosya-okuma araçları kaldırıldı (attest edilemeyen sınır yerine yalnız attested prompt byte'ları), (B) istek kimliği artık her ihraçta benzersizdir (uuid4, semantik fingerprint'ten türetilmez) ve `has_approved_grant` süre kontrolü yapar, (C) onay gösterimi (reason + outbound preview) fingerprint'e bağlandı ve güvenli attestation manifesti onay JSON'unda gösterilir, (D) EXTERNAL_EGRESS bildiren ama canonical outbound uygulamayan connector kapalı biçimde başarısız olur. SECRET dış yürütme yasağı, kimlik-bilgisi redaksiyonu ve connector yan-etki alt sınırı korunur.

22. **H2 — Yerel yürütme günlüğü ve yaşam döngüsü tahkimi tamamlandı.** Dayanıklı `ExecutionAttempt` günlüğü (`PREPARED→IN_FLIGHT→FINISHED`, `UNKNOWN_OUTCOME`) `execution_attempts` tablosunda (şema 3→4 ekleyici migration) ve `permission_grants.consumed_by_attempt` kolonu eklendi. Atomik tek-kazanan sahiplik (`open_execution_attempt` + `begin_execution` CAS + tek-sefer grant tüketimi, `BEGIN IMMEDIATE`), yeniden başlatmada IN_FLIGHT/UNKNOWN'ın yeniden oynatılmaması, FINISHED'ten yürütmeden ilerleme, PREPARED'ın güvenli devamı; terminal durum değişmezliği ve connector'sız raporlanabilirlik (`jarvis/lifecycle.py`); iptal tahkimi (yürüten sahip otoriter iptali yeniden yükler, bayat başarı CANCELLED'ı ezemez, bekleyen iptal `RunStatus.CANCELLING`); kullanım-önce sıralama (H2-H). Ayrıntılı sözleşme `ARCHITECTURE.md` §4.7'dedir. 26 yeni test (`tests/test_execution_journal.py`; `tests/test_session_progress.py`, `tests/test_storage_abstraction.py`, `tests/test_cli.py`, `tests/test_registry_persistence.py` odaklı güncellendi); toplam yerel/mock test 401'e çıktı. `Claude connector davranışı H2'de değişmedi`. Hiçbir canlı model/connector/auth/ağ çağrısı yapılmadı.

23. **H3 — Faturalandırma/defter bütünlüğü tamamlandı.** Sağlayıcıdan bağımsız kanonik parasal doğrulama (`canonical_usd`/`nano_usd`, tam dokuz ondalık basamak, tam sayı nano-USD kalıcılık, bool/NaN/sonsuz/negatif reddi, `ROUND_CEILING`), tipli `LedgerSnapshot` (ölçülmüş harcama + açık rezerve maruziyet + geçerlilik/gerekçe; bozuk satır kapalı biçimde başarısız olur), ekleyici şema 4→5 migration'ı ile `attempt_billing` tablosu (girişim başına en fazla bir kayıt, depo sahipli `usage_id`, sabit defter ayı, `reserved_nano_usd`, `active`/`reconciled`/`unresolved`), tipli `ExecutionClaimOutcome` (`CLAIMED`/`LOST`/`BILLING_RECHECK_REQUIRED`/`LEDGER_INVALID`) ve H2 talep işleminin içinde otoriter üst sınır kontrolü + girişime bağlı rezervasyon; girişime bağlı otoriter kullanım mutabakatı (`reconcile_usage`, `INSERT OR REPLACE` kaldırıldı, connector kimliği/zamanı defter otoritesi değil; otoriter defter ayı talep işleminde sabitlenir ve ay sınırını aşan yürütme kendi ayında mutabık kılınır), onaylı maksimumu aşan gerçek maliyetin tam kaydı + `BILLING_INTEGRITY_FAILED` ile başarısızlık, `Settings` NaN/sonsuz doğrulaması ve Claude telemetri dayanıklılığı eklendi. Bağımsız incelemenin beş P1 bulgusu kapatıldı: connector faturalandırma verisi otorite değildir (sınıf/durum/para matrisi, depo-sahipli connector kimliği/kanıt/`call_count`), defter geçerliliği ayın tüm kullanım ve rezervasyon satırlarıyla belirlenir, `BillingClaim` zorunludur ve 20 USD tavanı depoya aittir (çağıranın tavanına güvenilmez, cost-override kalıcı `override_monthly_cost_cap` isteğine karşı doğrulanır), sahipliği kaybeden `LOST` döner, ve çalışma zamanı enum tamlığı + SQLite tam sayı sınırları uygulanır. Dördüncü incelemenin üç P1 bulgusu da kapatıldı: `BillingClaim` zorunludur (sentetik yedek talep kaldırıldı, faturalandırma yokluktan çıkarsanmaz), kayıtlı metered defter satırı yalnızca `MEASURED` olabilir (ayrı ve daha katı `validate_ledger_usage`; çözülmemiş rezervasyon semantiği korunur) ve `CostEstimate.billing_class` en erken sınırda tam `BillingClass` üyesi olmak zorundadır. Yeni tek yapısal hata kodu `BILLING_INTEGRITY_FAILED` → CLI çıkış kodu 77. Ayrıntılı sözleşme `ARCHITECTURE.md` §4.8'dedir. 52 yeni test (`tests/test_billing_integrity.py`; `tests/test_app.py`, `tests/test_storage_abstraction.py`, `tests/test_billing_policy.py`, `tests/test_cost_verification.py`, `tests/test_execution_journal.py` odaklı güncellendi); toplam yerel/mock test 533'e çıktı. Claude faturalandırma yolu (`SUBSCRIPTION_INCLUDED`) ve yasak API kimlik bilgisi ortam politikası değişmedi; ücretli API fallback'i eklenmedi. Hiçbir canlı model/connector/auth/ağ çağrısı yapılmadı.

Önceki bir erken denemede görülen süresi dolmuş OAuth/401 durumu, daha sonraki local `claude auth status --json` kontrolü ve başarılı live smoke ile aşılmıştır. Başarılı çağrı Claude Max/OAuth abonelik yolunda çalışmış, ek parasal harcama oluşturmamış ve hiçbir tracked dosyayı değiştirmemiştir.

Sıradaki adımlar:

1. Bu final smoke checkpoint'i ayrı ve dar kapsamlı bir dokümantasyon commit'i olarak kaydet.
2. Yeni live model çağrısı yapmadan önce yeni task/session/request ve ayrı açık kullanıcı onayı oluştur.
3. Yazma yeteneğini yalnızca ayrı bir açık izin kapsamıyla, geri alınabilir test projesinde ve yeni güvenlik incelemesiyle değerlendir.
4. OpenAI API'yi yalnızca kullanıcı açıkça onayladıktan, güncel model/fiyat bilgisi doğrulandıktan ve harcama korumaları hazırlandıktan sonra bağla.

## Development Tooling — non-product

Bu bölüm yalnızca geliştirme ortamını ve kalite araçlarını kaydeder. Buradaki
hiçbir madde ürün mimarisi, çalışma zamanı bağımlılığı veya JARVIS davranışı
değildir; `ARCHITECTURE.md` kapsamına girmez.

- **Claude HUD:** Kullanıcı kapsamında kurulu ve aktif. JARVIS reposuna dosya eklemez.
- **CodeBurn 0.9.20:** Bağımsız global CLI; projeye bağımlı değildir. Gösterdiği
  USD değerleri API fiyat karşılığı **tahminidir**; Claude Max üzerinden oluşan
  gerçek ek harcama değildir ve JARVIS'in parasal defterine girmez.
- **Ruff 0.16.0 baseline:** `jarvis` üzerinde geniş denetimde 59 bulgu (H3
  öncesi 57). Artan iki bulgu bilinçli tutarlılık tercihidir: `UP017`
  (`jarvis/contracts.py` dosya genelinde `timezone.utc` kullanır) ve `TRY004`
  (`Settings` sözleşmesi her geçersiz değeri `ValueError` ile reddeder; tip
  hatası da politika hatasıdır). Blocking kural kümesi (E4,E7,E9,F) temizdir.
- **Bandit 1.9.4 baseline:** 6 Low, 0 Medium, 0 High (H3 ile değişmedi).
- **Bilinçli tasarım olarak korunan bulgular:** `RUF009` (frozen `SideEffectProfile`
  varsayılanı), `PLW1510` (`returncode` zaten elle kontrol ediliyor) ve iki
  `BLE001` (`jarvis/core.py` içindeki sistem sınırı hata dönüşümleri; ikincisi
  connector hatasında kullanım/muhasebe kaydının yazılmasını garanti eder).
  Bunlar düzeltilmemiş borç değildir; otomatik düzeltme uygulanmamalıdır.
- **Temizlenen test artıkları:** İki kullanılmayan test bağlaması `13c2a73`
  commit'inde kaldırıldı.
- **Blocking kalite kapısı:** `scripts/quality.ps1`. Tam denetim için
  `scripts/quality.ps1 -Audit`. Araçlar `uvx` ile sabit sürümde çalışır;
  runtime `dependencies` boş kalır.
- **Ertelendi:** `pip-audit` ve `uv.lock`. Tetikleyici: ilk gerçek üçüncü taraf
  runtime bağımlılığı, ikinci bir makine veya CI ihtiyacı. Bunlardan biri
  oluşana kadar eklenmez.

### Deferred candidates — not installed

Aşağıdakilerin **hiçbiri kurulu değildir, denenmemiştir ve seçilmemiştir.** Bu
liste yalnızca ileride değerlendirilebilecek adayları ve hangi koşulda
değerlendirileceklerini kaydeder; bir karar, taahhüt veya yol haritası değildir.
Hiçbiri ürün mimarisinin parçası değildir ve `ARCHITECTURE.md` kapsamına girmez.
Her biri, sırası geldiğinde ayrı ve açık kullanıcı onayıyla ele alınır.

- **Anthropic Frontend Design:** Gerçek UI geliştirmesi başladığında değerlendirilecek.
- **Context7:** Üçüncü taraf framework/API kullanımı arttığında, yalnız ihtiyaç
  halinde ve proje kapsamında değerlendirilecek.
- **CodeGraph vs Serena:** Repo büyüyüp yerleşik kod keşfi yetersiz kaldığında
  kontrollü karşılaştırma yapılacak; ikisi aynı anda kullanılmayacak.
- **Playwright MCP:** UI testi veya tarayıcı otomasyonu aşamasına gelindiğinde,
  önce yalnız geliştirme/test ortamında değerlendirilecek.
- **Crawl4AI:** İleride web içerik çıkarma connector'ı olarak; domain allowlist,
  prompt-injection savunması ve external-egress onayıyla değerlendirilecek.
- **OpenViking:** Mevcut sade/deterministik hafıza gerçekten yetersiz kaldığında
  değerlendirilecek.
- **Addy Osmani agent-skills:** Tam paket yerine yalnız ihtiyaç duyulan beceriler
  seçilecek.
- **Ponytail:** Güvenlik kritik olmayan tek bir görevde kontrollü pilot
  yapılmadan global kullanılmayacak.
- **Obsidian Skills:** Obsidian açıkça bilgi sistemi olarak seçilirse
  değerlendirilecek.

## İlk kabul ölçütleri

API anahtarı olmadan çalışan ilk çekirdek sürüm:

- Yapılandırılmış bir görev kabul edebilmeli.
- Kayıtlı connector'lar arasından görevi yapabilecek olanı bulabilmeli.
- Risk/izin gereksinimini belirleyebilmeli ve izinsiz yüksek riskli işi çalıştırmamalı.
- Sonucu yapılandırılmış biçimde döndürmeli.
- Oturum/karar kaydını yerel depoya yazıp tekrar okuyabilmeli.
- Harici model ve ücretli servis olmadan otomatik test edilebilmelidir.

## Maliyet ve onay sınırları

- **Kullanıcı kararı (yeni):** Claude Code, kullanıcının mevcut Claude Max aboneliği/OAuth oturumu üzerinden çalıştığında ve ek ölçülen parasal ücret doğurmadığında bu kullanım JARVIS'in 20 USD aylık parasal bütçesini tüketmez, ücretli API kullanımı sayılmaz ve uydurma USD harcaması üretilmez. Güvenilir biçimde elde edilebilen token/model/çağrı sayısı/sağlayıcı telemetrisi kaydedilebilir; dışarı veri gönderme onayı ve tüm güvenlik/izin politikası aynen geçerlidir.
- **Token telemetrisi durumu:** Parasal tarafta bilinmeyen değer ölçülmüş sıfıra dönüştürülmez. Ancak `input_tokens`/`output_tokens` kullanıcı kararıyla şimdilik eski, null olamayan tamsayı alanlar olarak bırakılmıştır; telemetri yoksa eski sıfır gösterimi kullanılır ve bu sıfır ölçülmüş sıfır token kanıtı sayılmamalıdır. Null yapılabilir token telemetrisi ayrı bir migration'da ele alınacaktır.
- Gerçek artımlı harcama doğuran yollar (Anthropic API anahtarı, kullandıkça öde, kredi vb.) için olağan parasal politika sürer: gerekli yerde maliyet onayı, parasal kullanım defteri ve 20 USD sert sınırı.
- Faturalandırma yolu güvenilir biçimde belirlenemiyorsa ücretsiz varsayılmaz, sıfır maliyet uydurulmaz; görev harici çağrıdan önce kapalı biçimde durur.
- Faturalandırılabilir model/connector kullanımı için aylık hedef: 20 USD.
- Kullanıcı onayı olmadan geçilemeyecek sert üst sınır: 20 USD. Bu sınıra yalnızca gerçek, ölçülmüş `METERED_MONETARY` harcama girer.
- Henüz hiçbir ücretli API anahtarı eklenmemiştir ve eklenmeyecektir.
- Claude Max aboneliği şirket tarafından karşılandığı için, resmi biçimde mümkünse Claude Code CLI üzerinden bundan yararlanmak önceliklidir; Anthropic API'ye otomatik geçiş yapılmaz.
- ChatGPT Plus ve diğer aboneliklerin kapatılması bu geliştirme aşamasının parçası değildir; ancak JARVIS olgunlaştıktan ve gerçek kullanım/maliyet ölçüldükten sonra kullanıcı tarafından değerlendirilir.

## Proje hafızasını güncelleme kuralı

- Yeni ve kalıcı teknik kararlar `ARCHITECTURE.md` içine eklenir.
- Güncel ilerleme, açık işler, kullanıcı tercihleri veya ortam değişiklikleri bu dosyada güncellenir.
- Deneysel fikirler kesin karar gibi yazılmaz; durumları açıkça "aday", "deney" veya "ertelendi" diye belirtilir.
- Hassas bilgiler, API anahtarları ve kimlik bilgileri bu dosyalara yazılmaz.
