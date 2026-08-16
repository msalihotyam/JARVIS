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
- Proje şu anda Git deposu değildir.
- İlk iskelette kullanıcı tarafından oluşturulan ve korunmaya devam eden klasör/dosyalar:
  - `Agent Core/agent_core.py`
  - `Connectors/base_connector.py`
  - `Memory/memory_store.py`
  - `Tools/tool_registry.py`
  - `Config/settings.py`
- Bu beş dosya başlangıçta boştu; artık standart `jarvis/` paketine yönlendiren uyumluluk sarmalayıcıları veya çalışan giriş dosyalarıdır. Kullanıcı tarafından oluşturulan yollar silinmemiştir.
- `ARCHITECTURE.md` ve `PROJECT_CONTEXT.md` bu devam oturumunda eklenmiştir.
- Başlangıç dosyaları artık ilk çekirdek uygulamayla doldurulmuştur. Ayrıca `contracts.py`, `Connectors/claude_code_connector.py`, paket `__init__.py` dosyaları ve konu bazında bölünmüş `tests/test_permissions.py`, `tests/test_cost_verification.py`, `tests/test_registry_persistence.py`, `tests/test_claude_connector.py`, `tests/test_storage_abstraction.py` ve `tests/test_windows_scripts.py` testleri eklenmiştir.

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
9. Toplam 63 yerel/mock test izin, maliyet, iptal, doğrulama, registry, migration, restart/persistence, storage abstraction, loop giriş aşaması koruması, Windows Python sürüm koruması, faturalandırma sınıfı politikası ve Claude güvenlik davranışlarını kapsar. Hem doğrudan unittest komutuyla hem `scripts/test.ps1` üzerinden başarıyla geçmiştir.
10. Sağlayıcıdan bağımsız faturalandırma sınıfı (`BillingClass`) ve parasal gözlem durumu (`MonetaryState`) sözleşmeye eklendi; usage ledger şeması 3'e additive migration ile yükseltildi. Claude connector, harici model çağrısından önce yerel ve modelsiz `claude auth status --json` yoklamasıyla Claude Max/OAuth yolunu doğrular, metered kimlik bilgisi ortam değişkeni **varsa (boş değerle bile)** veya yol belirlenemezse çağrıyı başlatmadan durur ve model alt sürecine temizlenmiş ortam verir.
11. Bağımsız inceleme sonrası dört düzeltme uygulandı: (a) kalıcılaştırılmış geç aşama devamı artık faturalandırma ön kontrolünü atlayamaz, (b) ücretli API onayı aylık sert sınır kapısını atlamaz, (c) sınıflandırma bildirmeyen connector `UNKNOWN` kalır (fail closed), (d) yasaklı kimlik bilgisi ortam değişkeni boş değerle de engeller.

Claude Code CLI sürüm `2.1.232` olarak kurulu bulundu. Resmi, etkileşimsiz JSON çağrısı denendi; hesap Max aboneliğiyle tanınıyor fakat OAuth erişim belirteci süresi dolduğu için sunucu `401` döndürdü. Denemenin raporlanan maliyeti `0 USD` oldu ve hiçbir dosya değiştirilmedi.

Sıradaki adımlar:

1. Kullanıcı isterse kendi Claude hesabında `claude auth login` ile yeniden giriş yapar.
2. Canlı çağrı ancak kullanıcı ayrıca istediğinde, aynı tek dönüşlü güvenli plan modunda tekrar çalıştırılır.
3. Başarılı olursa Claude connector, Agent Core üzerinden ilk uçtan uca kod planlama görevinde doğrulanır.
4. Yazma yeteneği ayrı bir açık izin kapsamıyla ve geri alınabilir test projesinde eklenir.
5. OpenAI API ancak kullanıcı açıkça onayladıktan, güncel model/fiyat bilgisi doğrulandıktan ve harcama korumaları hazırlandıktan sonra bağlanır.

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
