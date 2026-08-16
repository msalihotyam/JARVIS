# JARVIS Architecture

Bu belge, `JARVIS PLAN` konuşmasında kesinleşen teknik kararların kalıcı kaydıdır. Yeni fikirler ancak kullanıcı onayından sonra "Kesin kararlar" bölümüne taşınır.

## 1. Ürün tanımı

JARVIS, tek kullanıcılı ve öncelikle Windows 11 üzerinde çalışan yerel bir kişisel çalışma merkezidir. Bir sohbet botu veya yalnızca kod ajanı değildir. Kullanıcının niyetini anlayan, ilgili bağlamı ve hafızayı getiren, işi planlayan, doğru araçları seçen, gerekli izinleri alan, sonucu doğrulayan ve süreci kullanıcıyla birlikte yürüten bir orkestratördür.

JARVIS'in çekirdeği işi doğrudan yapmak yerine işi araçlara dağıtır. Kod ajanları, terminal, dosya sistemi, tarayıcı ve gelecekte eklenecek yetenekler ortak bir araç/connector katmanının arkasında çalışır.

## 2. Kesin mimari ilkeler

1. **Yerel ve tek kullanıcılı:** İlk hedef yalnızca kullanıcının kendi laptopunda çalışan bir masaüstü uygulamasıdır. Kullanıcı hesabı, giriş ekranı, domain, SaaS, çoklu kullanıcı ve zorunlu bulut sunucusu yoktur.
2. **Modelden bağımsız çekirdek:** JARVIS'in kimliği, hafızası, ayarları, izinleri ve araç yönetimi belirli bir modele bağlı olmayacaktır. Sağlayıcı ve model adaptörlerle değiştirilebilir olacaktır.
3. **Varsayılan motor kararı:** Planlama konuşmasında JARVIS v1 için varsayılan konuşma/orkestrasyon motoru olarak GPT-5.6 Sol seçildi. Bu seçim yapılandırmadır; mimari bağımlılık değildir. Gerçek model adı, erişilebilirlik ve güncel fiyatlar API bağlantısından önce yeniden doğrulanacaktır.
4. **Claude Code öncelikli kod aracı:** Kod geliştirme işleri için ilk gerçek connector, mevcut Claude Max oturumundan resmi ve desteklenen biçimde yararlanması hedeflenen Claude Code CLI connector'ıdır. Önce küçük bir teknik prototiple doğrulanacaktır. Anthropic API kullanımı varsayılmaz.
5. **Mevcut yetenekleri öncele:** JARVIS bilmediği bir görevde biliyormuş gibi davranmaz. Önce mevcut araçların birleşimiyle çözmeyi dener; yeni bir yetenek veya connector yazmayı kendiliğinden başlatmaz.
6. **Kullanıcı süreçte kalır:** Sistem kara kutu biçiminde saatlerce çalışıp yalnızca sonuç dönmez. Planı, önemli ilerlemeyi, seçenekleri ve riskleri kullanıcıya görünür kılar; kullanıcı çalışma sırasında yön değiştirebilir, iptal edebilir veya eski bir karar noktasına dönebilir.
7. **Çekirdek sabit, yetenekler genişleyebilir:** Yeni ihtiyaçlar mümkün olduğunca yeni connector/skill eklenerek karşılanır. Agent Core nadiren değiştirilir.
8. **Yazı merkezli deneyim:** Ana deneyim yazılı sohbet olacaktır. Ses, ilk anlatımı veya seçilen mesajları metne çevirmek için isteğe bağlı giriş olarak sonradan eklenebilir; sürekli canlı ses v1 gereksinimi değildir.

## 3. Sabit Agent Loop

Her görev aynı üst düzey, yeniden başlatılabilir durum makinesini izler:

1. Kullanıcının niyetini ve aktif çalışma durumunu anla.
2. İlgili proje, oturum, karar ve hafıza kayıtlarını getir.
3. Sonucu gerçekten etkileyen bilgi eksikse sor; güvenli bir varsayımla ilerlenebiliyorsa varsayımı görünür kıl.
4. Uygulanabilir bir plan oluştur.
5. Risk, yan etki ve izin kontrolü yap.
6. Görev öncesi maliyet tahmini ve aylık sert sınır kontrolü yap.
7. Priority, suitability, availability ve maliyete göre tek bir aracı deterministik seç; eşitlikte sessizce ilkini alma.
8. Aracı normalize edilmiş kapsam içinde çalıştır.
9. Sonucu bağımsız doğrulayıcı veya connector'a özgü açık yöntem ve kanıtla doğrula.
10. Kullanıcıya sonucu ve anlamlı sonraki adımı sun.
11. Çalışma durumunu, alınan kararı, gerekçesini, kullanım kaydını ve sonraki adımı atomik olarak hafızaya yaz.

Her aşama SQLite'a task durumu, pending approval ve loop checkpoint olarak kaydedilir. Uygulama kapanırsa bilinmeyen yeni bir oturum açılmaz; aynı session/task kimliğiyle son güvenli checkpoint'ten devam edilir.

Bu döngü kod, araştırma, dosya, tarayıcı ve bilgisayar kontrolü görevlerinde değişmez; yalnızca kullanılan araçlar değişir.

## 4. Çekirdek sözleşmeler

İlk uygulama aşağıdaki model ve arayüzleri tanımlayacaktır:

- **Task:** Amaç, bağlam, normalize edilebilir açık kapsam, durum, risk, yan etki, maliyet tahmini ve kullanıcı tercihleri.
- **Tool/Connector:** Ad, yetenekler, availability/health, suitability, priority, timeout, maliyet tahmini, yan etkiler, çalıştırma, iptal ve kanıtlı doğrulama davranışı.
- **Result:** Başarı boolean'ı yerine yapılandırılmış outcome, özet, veri, hata kodu, doğrulama durumu/yöntemi/kanıtı ve kaybolmayan kullanım kaydı.
- **Permission:** İşlem, normalize hedef/path, connector, destination, veri sınıfı, session, task, fingerprint, süre sonu ve kullanıcı kararı.
- **Session/Work State:** Aktif hedef, plan, ilerleme, kararlar, vazgeçilen alternatifler, dosya değişiklikleri ve sonraki adım.
- **Model Adapter:** Sağlayıcıdan bağımsız availability, tamamlama, iptal ve kullanım/maliyet arayüzü.
- **Memory Store:** SQLite'a bağlı olmayan session, task, approval, checkpoint, karar ve maliyet defteri protokolü.

Connector'lar ortak bir arayüz kullanacaktır. Sağlayıcıya veya belirli bir CLI'a özgü ayrıntılar Agent Core'a sızmayacaktır.

## 5. İzin ve güvenlik modeli

Üç kullanıcı modu kesinleşmiştir:

- **Sıkı onay:** Eyleme dönüşen her önemli adımda onay ister.
- **Oturumluk onay (varsayılan):** Belirli bir eylem türü ve kapsamı için oturum boyunca bir kez izin alır.
- **Akıllı otomatik:** Yalnızca LOW ve geri alınabilir işlemleri otomatik yapar. MEDIUM/HIGH/CRITICAL veya geri alınamaz işlemlerde durur.

Her modda aşağıdakiler açık onay gerektirir:

- Para harcama, ücretli API/abonelik açma veya faturalandırılabilir kaynağı etkinleştirme.
- Dosya silme, geniş kapsamlı üzerine yazma veya geri dönüşü zor veri değişikliği.
- Dışarı veri gönderme, gizli bilgi veya kimlik bilgisi paylaşma.
- Git push, yayınlama, dağıtım veya başkalarını etkileyen dış sistem işlemleri.
- Finansal işlem, form gönderimi veya benzeri gerçek dünya etkisi olan son adımlar.

Dosya okuma, yerel analiz, özetleme ve normal yerel testler düşük riskli kabul edilebilir. İzinler capability ile değil; normalize hedef/path, işlem, connector/destination, veri sınıfı, task ve session ile sınırlandırılır. Süresi dolmuş, başka task/session'a ait veya fingerprint'i değişmiş grant hiçbir işi çalıştırmaz. Dışarı gönderilecek verinin yalnızca redacted önizleme/metadata bilgisi onay ekranında gösterilir.

## 6. Hafıza ve çalışma durumu

Hafıza yalnızca sohbet geçmişi değildir. En az iki katman olacaktır:

- **Ham kayıt:** Mesajlar, araç olayları ve değişiklik günlüğü.
- **Yapılandırılmış kalıcı bilgi:** Proje gerçekleri, kullanıcı tercihleri, kararlar ve karar gerekçeleri.

Her proje ayrı bağlam ve oturumlara sahip olabilir. Bir çalışma oturumu hedefi, mevcut planı, tamamlanan adımları, açık sorunları, değişen dosyaları ve sonraki adımı saklar. "Baştan yapalım" geçmişi silmez; yeni bir alternatif/karar dalı açar. Eski karar noktalarına geri dönme desteklenecek şekilde veri modeli tasarlanır.

İlk yerel saklama çözümü olarak SQLite uygundur; ancak depolama arayüzü daha sonra başka bir sisteme geçişi engellemeyecektir.

## 7. Maliyet yönetimi

- Tüm faturalandırılabilir connector/model kullanımı için aylık hedef **20 USD**, kullanıcı onayı olmadan geçilemeyecek sert üst sınır da **20 USD**'dir.
- Her ücretli görevden önce mümkünse model, tahmini token, tahmini maliyet aralığı ve belirsizlik gösterilir.
- Çalışma sırasında gerçek token ve yaklaşık dolar karşılığı izlenebilir; görev sonunda gerçek kullanım kaydedilir.
- Aylık gerçek kullanım SQLite usage ledger'da toplanır. Görev öncesi maksimum tahmin mevcut aylık harcamayla birlikte 20 USD'yi aşarsa görev durur ve yalnızca o göreve bağlı açık cost-override grant'iyle devam eder.
- `paid_api_enabled` varsayılan olarak false kalır; kalıcı olarak açılması kaydedilmiş açık kullanıcı karar kimliği gerektirir. Kapalıyken ücretli bir görev ayrıca göreve özel billing onayı ister.
- Yerel işlem, ilgili bağlamı seçerek küçültme, özetleme ve gereksiz model çağrılarını önleme temel optimizasyonlardır.
- Aynı işi iki büyük modele gereksiz yere yaptırmak varsayılan değildir.

## 8. V1 geliştirme sırası

1. Mimari ve proje bağlamını kalıcılaştır.
2. Task, Result, Permission, Session ve Connector sözleşmelerini oluştur.
3. API kullanmayan Agent Core ve araç kayıt defteri iskeletini kur.
4. Yerel bellek deposunun ilk sürümünü kur ve test et.
5. Claude Code CLI için küçük, resmi-yol doğrulama prototipi yap.
6. Prototip başarılıysa connector'a dönüştür ve ilk uçtan uca görevi çalıştır.
7. OpenAI API bağlantısını yalnızca kullanıcı açıkça onayladıktan, model/fiyat bilgisi yeniden doğrulandıktan ve harcama limiti yapılandırıldıktan sonra ekle.
8. Masaüstü arayüzünü çekirdek akış kanıtlandıktan sonra geliştir.

## 9. Planlanan, isteğe bağlı entegrasyonlar

Aşağıdaki entegrasyonların kullanılması planlanmaktadır; ancak hiçbiri çekirdek bağımlılık değildir:

- **Graphify:** Kod grafiği, bağımlılık ve etki analizi yardımcısı. Agent Core veya iç durum hafızasının yerine geçmez.
- **Context7:** Güncel kütüphane dokümantasyonu ve görev bağlamı yardımcısı. Sağlayıcı bağımsız connector arkasında kullanılacaktır.
- **Obsidian:** Kullanıcının isteğe bağlı bilgi yüzeyi/not deposu. JARVIS'in iç session, task, karar gerekçesi ve izin hafızasının yerine geçmez ve tek doğruluk kaynağı değildir.

## 10. Henüz kesinleşmemiş veya ertelenmiş konular

Aşağıdakiler konuşmada değerlendirildi fakat çekirdeğin zorunlu bağımlılıkları olarak kesinleştirilmedi:

- Electron + React + TypeScript masaüstü teknoloji yığını.
- Playwright, GitHub MCP, Figma, Supabase, Sentry ve benzeri diğer connector'ların kesin kapsamı.
- `agent-memory`, Context Mode, Headroom ve benzeri üçüncü taraf hafıza/bağlam projeleri.
- Codex'in ikinci geliştirici veya kod inceleyici olarak eklenmesi.
- Otomatik model yönlendirme, derin görev modeli ve yerel modeller.
- Tam bilgisayar kontrolü ve banka/ekspertiz sistemi keşif otomasyonu.

Bu maddeler ihtiyaç doğduğunda güvenlik, lisans, bakım maliyeti ve gerçek fayda açısından ayrıca değerlendirilecektir.
