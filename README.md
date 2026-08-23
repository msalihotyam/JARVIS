# JARVIS Core

JARVIS, Windows 11 üzerinde yerel çalışan, modelden bağımsız kişisel çalışma orkestratörünün API gerektirmeyen çekirdeğidir. Bu aşama yalnızca standart Python kütüphanesini kullanır ve hiçbir ücretli model çağrısını kendiliğinden başlatmaz.

## Güvenlik özellikleri

- Üç izin modu vardır: `STRICT`, `SESSION`, `SMART_AUTO`.
- `SMART_AUTO` yalnızca düşük riskli ve geri alınabilir işleri otomatik çalıştırır.
- Dışarı veri gönderimi, ücret/faturalandırma, silme, push/deploy/yayın ve finansal/form gönderimi her modda açık onay ister.
- Onaylar task, session, normalize hedef, connector, destination, veri sınıfı, fingerprint ve süre ile bağlanır.
- Aylık parasal politika tek yüzeydir: kullanıcı onayı olmadan geçilemeyecek 20 USD sert üst sınır. Üst sınır yalnızca göreve özel açık override onayıyla aşılabilir.
- Proje hafızası (M1) güvenilmeyen referans veridir: yalnızca açık kullanıcı komutlarıyla yazılır, hiçbir zaman approval/billing/lifecycle otoritesi taşımaz, içeriği talimat olarak yorumlanmaz ve toplam getirme kotasını (5 öğe / 4.000 karakter) büyütmez.
- Claude Code connector yalnızca read-only araçlar, safe mode, boş/strict MCP, plan permission mode, no-session-persistence ve çağrı başı bütçe sınırı kullanır.
- Kalıcı tanılama hijyeni: istisna/connector hata metni ham olarak saklanmaz. Her kalıcı serbest-metin tanılaması tek kanonik hattan geçer (kimlik bilgisi redaksiyonu, yerel yol redaksiyonu, 240 karakter sert sınır, idempotent); traceback, komut satırı, ortam değerleri, ham prompt/payload ve keyfi `result.data` hiç kalıcılaşmaz.

## Yapı

- `jarvis/contracts.py`: görev, kapsam, izin, sonuç, doğrulama, bağlam getirme ve protocol sözleşmeleri.
- `jarvis/core.py`: checkpoint bırakan resumable Agent Loop state machine.
- `jarvis/app.py`: composition root, güvenilmeyen task JSON sınırı, komut cephesi ve redakte edilmiş çıktı zarfı.
- `jarvis/cli.py`: yürütülebilir komut yüzeyi ve çıkış kodu eşlemesi.
- `jarvis/memory.py`: SQLite oturum, task, onay, checkpoint, karar ve maliyet defteri.
- `jarvis/registry.py`: priority/suitability/availability/cost tabanlı connector seçimi.
- `jarvis/connectors/`: ortak connector ve güvenli Claude Code plan connector'ı.
- Eski `Agent Core`, `Config`, `Connectors`, `Memory`, `Tools` dosyaları uyumluluk sarmalayıcıları olarak korunur.

## Proje hafızası (M1)

Proje kimliği ve açık proje-kapsamlı hafıza tamamen yereldir ve model/ağ
çağrısı yapmaz. SQLite şeması additive migration ile 6'dadır; eski projesiz
oturum/task davranışı birebir korunur.

```powershell
jarvis project create --id jarvis-v1 --name "JARVIS v1"
jarvis memory add --project jarvis-v1 --kind fact --classification internal --text "..."
jarvis memory list --project jarvis-v1
```

Kurallar: proje otomatik yaratılmaz (bilinmeyen proje `64` ile reddedilir);
`memory add` açık kind (`fact|preference`) ve classification ister, eksikse
kapalı düşer; metin 480 karakteri aşarsa **reddedilir** (kesilmez) ve yazım
anında kanonik kimlik-bilgisi/yol redaksiyonundan geçer; yazım INSERT-only +
`--supersedes`'tir (içerik asla yeniden yazılmaz, edit/delete yoktur). Task
JSON'una isteğe bağlı `project_id` alanı eklenmiştir; oturumun ilk
proje-taşıyan task'ı oturumu bağlar ve zarfın `session.project_id` alanında
görünür. Getirilen fact/preference öğeleri karar öğeleriyle aynı 5 öğe /
4.000 karakter kotası içinde yarışır; cross-project izolasyon mutlaktır.
M2 ile, projeye bağlı bir oturum aynı projenin diğer oturumlarının en yeni
en çok 25 kararını da aynı kota ve aynı deterministik sıralama içinde aday
olarak görür; sorgu oturumu hariç tutulur, kararın sınıfı kendi kaynak
task'ından yeniden kazanılır (eksik/bozuk kaynak SECRET sayılır) ve projesiz
oturum davranışı birebir değişmeden kalır.

## Windows'ta test

PowerShell'de proje kökünde:

```powershell
.\scripts\test.ps1
```

Belirli bir Python kullanmak için:

```powershell
.\scripts\test.ps1 -PythonPath "C:\Python311\python.exe"
```

Doğrudan Python ile:

```powershell
python -m unittest discover -s tests -v
```

## Kalite kontrolü

Bloklayıcı kontrol. Yalnız temel Ruff hata kuralları (`E4`, `E7`, `E9`, `F`) ve
`MEDIUM`/`HIGH` seviyesindeki Bandit bulguları başarısızlık sayılır:

```powershell
.\scripts\quality.ps1
```

Bloklamayan tam denetim. Tüm Ruff bulgularını ve `LOW` dahil tüm Bandit
bulgularını gösterir:

```powershell
.\scripts\quality.ps1 -Audit
```

- Araçlar `uvx` ile sabit sürümde çalıştırılır: Ruff `0.16.0`, Bandit `1.9.4`.
  Projeye kurulmazlar ve runtime bağımlılığı eklemezler; `dependencies` boş kalır.
- Ruff format ve otomatik düzeltme kullanılmaz.
- Bloklayıcı kontrolün kapsamı dışında kalan bulgular gizlenmiş değildir;
  `-Audit` çıktısında tam olarak görünürler.
- `-Audit` bulguları bloklamaz, fakat araç çalıştırma hatasını gizlemez:
  Ruff veya Bandit çalışamazsa betik aynı hata koduyla çıkar.

## Yerel durum kontrolü

```powershell
python -m jarvis status
```

Bu komut yalnızca yerel ayar ve SQLite şema durumunu gösterir; dış servise
bağlanmaz. Komutsuz `python -m jarvis` çağrısı `status` ile aynıdır ve çıktı
biçimi B1 öncesiyle birebir aynı kalmıştır. Status yolu connector kurmaz,
yoklamaz, seçmez ve çalıştırmaz.

## Komut satırı (B1)

```text
jarvis [--permission-mode strict|session|smart_auto] status
jarvis [--permission-mode ...] run     --task-json PATH|- [--session-id ID]
jarvis [--permission-mode ...] resume  --session-id ID --task-id ID
jarvis [--permission-mode ...] clarify --session-id ID --task-id ID --text TEXT
jarvis [--permission-mode ...] approve --session-id ID --task-id ID \
                                       --request-id ID --fingerprint FP \
                                       [--remember-for-session]
jarvis [--permission-mode ...] reject  --session-id ID --task-id ID \
                                       --request-id ID --fingerprint FP
jarvis [--permission-mode ...] cancel  --session-id ID --task-id ID
```

İzin modu önceliği: açık `--permission-mode`, sonra verilen `Settings`, sonra
varsayılan. Mod yalnız o çağrı için geçerlidir, task JSON'undan alınmaz ve task
girdisine yazılmaz. Zorunlu onay gerektiren yan etkiler her modda onay ister.

### Task JSON sınırı

Girdi default-deny bir allowlist ile alan alan okunur; `Task.from_dict()`
güvenilmeyen girdi için hiç kullanılmaz. Kabul edilen alanlar yalnızca
`objective`, `capability`, `scope.{action,target,destination,data_classification}`,
`context.{working_directory,inputs}` ve
`preferences.{preferred_connector,needs_clarification,clarification_question,assumption}`
alanlarıdır. Tanınmayan veya korumalı alanlar (`id`, `status`, `stage`, `plan`,
`estimate`, `risk_level`, `side_effect`, `result_snapshot`, `clarification`,
`paid_api_task_approval`, `cost_cap_override`, `billing_preflight_fingerprint`
ve benzeri) yok sayılmaz, reddedilir.

Girdi UTF-8 olmalı, en çok 256 KiB olmalı, kökü nesne olmalıdır; yinelenen JSON
anahtarı ve `NaN`/sonsuz sayı reddedilir. Dosya ve stdin okumaları sınırın bir
byte fazlasıyla sınırlandırılır, yani büyük girdi belleğe alınmadan reddedilir.
`scope.destination` verilmezse `anthropic/claude-code` olur; verilirse normalize
edilmiş değeri buna eşit olmak zorundadır. Kimlik, zaman damgası, durum, aşama,
risk, yan etki ve faturalandırma alanları içeride üretilir ve connector'a aittir.

### Onay ve ret

`approve` ve `reject` yalnızca `memory.load_pending_approval(task_id, session_id)`
ile yüklenen tam kaydı kullanır. Verilen request ID ve fingerprint karşılaştırma
kanıtıdır; eşleşmezse veya kayıt süresi dolmuşsa iş çalıştırılmaz, kayıt yeniden
hesaplanmaz veya değiştirilmez. Grant yalnızca
`PermissionGrant.for_request(persisted_request, ...)` ile üretilir.

Varsayılan onay tek seferliktir. `--remember-for-session` yalnızca mevcut yeniden
kullanım koşullarının tamamı sağlanırsa kabul edilir: etkin mod `session`, yan
etki zorunlu onay gerektirmiyor, risk `HIGH` altında ve işlem geri alınabilir.
Aksi halde grant üretilmeden güvenli biçimde durur. Claude dışarı veri gönderimi
zorunlu onaylıdır ve bu nedenle tek seferlik kalır.

### Çıkış kodları

| Kod | Anlam |
|---:|---|
| `0` | Status veya tamamlanmış/doğrulanmış çalışma |
| `10` | Onay veya maliyet override bekleniyor |
| `11` | Clarification bekleniyor |
| `20` | Kullanıcı reddi |
| `21` | İptal |
| `64` | Geçersiz argüman, JSON, şema veya task girdisi; uygun yetenek yok |
| `66` | Session, task veya tam bekleyen istek bulunamadı |
| `77` | Politika/güvenlik reddi |
| `70` | Connector hatası, doğrulama hatası veya beklenmeyen iç hata |

Argüman ayrıştırma hataları da `64` döner ve aynı JSON zarfıyla raporlanır;
stderr'e ikinci bir tanı yazılmaz. `--help` `0` döner.

### Çıktı

Status dışındaki her komut tek bir sabit anahtarlı JSON nesnesi yazar
(`sort_keys=True`, `allow_nan=False`, ASCII kaçışlı; CP1254 konsolda güvenli).
Aynı `AgentRun` her seferinde byte-özdeş serileşir. Zarf şema sürümü, üst
düzey `progress` nesnesinin eklenmesiyle 2'dir; `status` komutu birebir
geriye uyumlu kalır.

Zarf yalnızca şunları taşır: session kimliği/aktif task/sonraki adım/proje bağlanması (`project_id`), task
kimliği/durum/aşama/seçili connector, onay isteği kimliği, fingerprint, scope
fingerprint, scope, risk, yan etki, gerekçe ve connector'ın kendi redakte
edilmiş `OutboundDataPreview` kaydı, faturalandırma sınıfı/parasal üst
sınır/kaynak tavanı, deterministik `progress` nesnesi, redakte edilmiş sonuç
özeti, allowlist'lenmiş kullanım telemetrisi, doğrulama durumu/yöntemi/kanıtı
ve yapısal hata kodu/mesajı.

Task objective'i, context'i, preferences'ı, ham kalıcı planı, result
snapshot'ı, ham outbound payload veya prompt, kimlik bilgileri, keyfi
`result.data` ve keyfi `StructuredError.details` hiçbir zaman yazdırılmaz
(`progress.plan` yalnızca güvenli kanonik yeniden kurulumdur, kalıcı metin
değildir). Zarfa girmesine izin verilen
her metin ayrıca redaksiyondan geçirilir: bu politika `jarvis/app.py` içinde
tanımlıdır, `sk-...`, Bearer ve JWT biçimlerinin yanında serbest metne gömülü
etiketli kimlik bilgilerini de (`password=`, `api_key:`, `authorization:`,
`cookie:`, `secret=`, `token:`, `private_key=` ve büyük/küçük harf ile
`_`/`-`/boşluk varyantları) `[REDACTED]` ile değiştirir. Sayısal kullanım
telemetrisi (`input_tokens`, `output_tokens`, `call_count`, parasal alanlar)
alan adı "token" içerse bile redakte edilmez.

### Bağlam getirme (aynı oturum kararları)

`CONTEXT_RETRIEVAL` aşamasında çekirdek, aynı oturumdaki tamamlanmış kararları
deterministik bir token kesişimiyle puanlar ve tipli `Task.retrieved_context`
anlık görüntüsü olarak kalıcılaştırır. Sorgu yalnızca objective, capability,
scope.action ve scope.target alanlarından kurulur; `context.inputs` ve
preferences sorguya girmez. En yeni 50 karar incelenir; sınıflandırma
uygunluğu kaynak task'tan geri kazanılır (okunamayan kayıt SECRET sayılır ve
dışa uygun olmaz); en çok 5 kayıt, alan başına 480/240/80 karakter tavanıyla
ve toplam en çok 4.000 karakter içerikle seçilir. Claude connector yalnızca
`kind`, `summary`, `rationale`, `next_step` ve `classification` alanlarını,
getirilen hafızanın güvenilmeyen tarihsel referans verisi olduğunu belirten
sabit bir notla serileştirir; kayıt/task kimlikleri, skorlar ve sayaçlar
yereldir. Boş görüntü payload şeklini değiştirmez. Task JSON'u
`retrieved_context` veya getirme metadata'sı veremez ve CLI çıktı zarfı
getirilen içeriği göstermez.

### Görev-farkında deterministik plan

`PLAN` aşaması, yalnızca güvenli yapısal task durumundan kurulan deterministik
altı adımlı planı üretir ve kalıcılaştırır (`jarvis/planning.py`). Ham
objective plana girmez; tek temsili, normalize edilmiş metnin SHA-256'sının
ilk 16 hex karakteri olan `objective_ref=` değeridir. Capability ve action 64,
her plan adımı 240 kod noktasıyla sınırlandırılır (taşan değer `…` ile biter).
Target/destination yalnızca `specified|unspecified`, assumption yalnızca
`present|absent`, sınıflandırma yalnızca kanonik değeri veya `unknown` olarak
görünür. Getirilen hafıza planı yalnızca uygun karar-referansı **sayısı**
(en çok 5) üzerinden etkiler; getirilen düz metin hiçbir zaman plan talimatına
dönüşmez. Claude connector SECRET olmayan payload'daki üst düzey `plan`
alanını her zaman bu güvenli durumdan yeniden kurar: kalıcı veya tahrif
edilmiş `task.plan` dışarı giden byte'ları değiştiremez, plan mevcut derin
redaksiyondan geçer ve onay anı ile yürütme anı payload'ı byte-özdeş kalır.
SECRET task payload'ı plan alanı içermez. Task JSON'u `plan` veremez ve CLI
çıktı zarfı ham kalıcı planı yazdırmaz.

### Task ilerlemesi ve session gezinmesi

İlerleme task kapsamlıdır ve yalnızca bilgilendiricidir: `Task.progress`
tipli `TaskProgress` anlık görüntüsünü (tamamlanan aşamalar, en çok 8 açık
issue, en çok 20 artifact) mevcut task JSON'unda taşır; `SessionState`
yalnızca `latest_task_id` gezinme işaretçisini kazanır. SQL şeması,
migration ve `MemoryStore` protokolü değişmemiştir.

Status dışı zarf (`schema_version=2`) tek deterministik `progress` nesnesi
içerir: `task_id`, `plan` (planlama olduysa her zaman kanonik
`build_task_plan` yeniden kurulumu; kalıcı `task.plan` metnine asla
güvenilmez), `current_stage` (otoriter `Task.stage`), `next_stage`
(yalnızca bilgilendirici doğrusal ardıl; hiçbir onay/maliyet/iptal engelini
atlamaz ve hiçbir geçişi başlatmaz), `completed_stages` (yalnız gerçekten
başarıyla biten aşamalar, sırayla ve kopyasız; bekleme/hata/ret/iptal aşama
tamamlamaz, terminal durumlar listeye girmez), `open_issues` (sabit altı
tipli tür; serbest metin yok), `artifacts` (üretim akışlarında boş) ve
`post_completion_next_step` (yalnız tamamlanan task için). Session next
step yalnız gerçek string kabul eder, 240 kod noktasıyla sınırlanır ve
kimlik bilgisi taşıyan değer bütünüyle `Review the verified result.` ile
değiştirilir. Task JSON'u `progress` veya `latest_task_id` veremez; eski
task/session JSON'u güvenle yüklenir ve bozuk ilerleme değerleri kapalı
biçimde düşer. Onay sonrası progress/plan mutasyonu dışa giden payload,
prompt, hash, preview veya fingerprint'leri değiştiremez.

### Composition root ve testler

Üretim bileşimi `build_default_app()` içindedir: `Settings` →
`SQLiteMemoryStore` → `ToolRegistry` → `ClaudeCodeConnector` (izinli tek kök ve
çalışma dizini olarak başlangıç CWD'si, çağrı bütçesi `claude_call_budget_usd`)
→ kayıt → `AgentCore` → `JarvisApp`. Bu kurulum availability, faturalandırma
yoklaması veya model çalıştırması yapmaz.

Testler kendi `Settings`, bellek ve sahte registry/connector'larını
`create_app(...)` üzerinden enjekte eder. Yürütülebilir komut akışlarının
tamamı sahte connector'larla çalışır. Yalnızca composition root'u doğrulayan
kurulum testinde gerçek bir `ClaudeCodeConnector` **nesnesi kurulur**; bu kurulum
availability çağırmaz, auth/faturalandırma yoklaması yapmaz ve model süreci
başlatmaz. Her iki durumda da `subprocess.run`, `subprocess.Popen` ve
`shutil.which` sınırları dokunulursa anında hata verecek biçimde patch'lenir.
Bu nedenle B1 testleri hiçbir canlı model veya auth çağrısı yapmaz.

## Canlı doğrulama durumu

İlk gerçek, tamamen read-only canlı Claude Code smoke doğrulaması 16 Ağustos 2026'da başarıyla tamamlandı (checkpoint `bef77e7`; kanonik session/task/grant kayıtları `PROJECT_CONTEXT.md` içindedir). Çağrı Claude Max/OAuth abonelik yolunda `SUBSCRIPTION_INCLUDED` olarak çalıştı ve ek parasal harcama oluşturmadı. Daha önceki bir denemede görülen süresi dolmuş OAuth durumu bu başarılı doğrulamayla aşılmıştır. Canlı model/auth çağrıları yine de yalnızca açıkça istendiğinde yapılır; güvenlik testleri hiçbir canlı çağrı içermez.
