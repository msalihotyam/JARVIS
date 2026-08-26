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
- Yerel yazma (M5) her izin modunda açık onay ister ve geri alınamaz olarak beyan edilir: `SideEffect.LOCAL_WRITE` zorunlu-onay etkisidir, hatırlanan bir SESSION grant'i bir yazmayı asla yetkilendiremez ve "yalnızca oluşturma"ya otomatik onay yoktur. Onay, yazılacak **tam byte'lara** ve hedefin **önceki durumuna** bağlanır; ikisinden biri değişirse fingerprint geçersizleşir. Yazma atomiktir ve otorite mutasyon anında yeniden kanıtlanır (kaynak nesne kimliği, `create` için kernel tarafından zorlanan hedef yokluğu, `overwrite` için onaylanan ön-durum); hedef hiçbir zaman yazma için açılmaz ve JARVIS hiçbir pre-image saklamadığı için **rollback vaat etmez**.
- Açık model cevabı aktarımı (M7): create-only bir `file_write` görevi, aynı oturumdaki tamamlanmış bir model görevinin cevabını `write.model_answer_task_id` ile tam byte kaynağı olarak **açıkça** referans verebilir. Cevap güvenilmeyen veridir; tek yetki sınırı yine zorunlu `LOCAL_WRITE` onayıdır ve byte'lar onaydan önce görevin içinde dondurulur, kaynak bir daha okunmaz. Bozuk veya tutarsız kalıcı aktarım kimliği asla direct yazmaya dönüşmez: `invalid` yüklenir ve kapalı düşer.
- Kalıcı tanılama hijyeni: istisna/connector hata metni ham olarak saklanmaz. Her kalıcı serbest-metin tanılaması tek kanonik hattan geçer (kimlik bilgisi redaksiyonu, yerel yol redaksiyonu, 240 karakter sert sınır, idempotent); traceback, komut satırı, ortam değerleri, ham prompt/payload ve keyfi `result.data` hiç kalıcılaşmaz.
- Sınırlı çok dosyalı okuma bağlamı (M8): bir `code_analysis` / `code_planning` görevi `context.local_read_task_ids` ile aynı oturumdaki **en fazla 3** tamamlanmış `file_read` görevini açıkça referans verebilir. Kaynaklar CONTEXT_RETRIEVAL'da **hep-ya-hiç** çözümlenir, sıralı tek bir demet olarak dondurulur ve mevcut H1-bağlı egress yolundan gönderilir. Tekil `local_read_task_id` ile birlikte kullanılamaz; referanslar `task.context` içinde **kalmaz**. Bozuk veya eksik demet asla bağlamsız bir model isteğine dönüşmez: `invalid` yüklenir ve kapalı düşer.
- Sınırlı bölge okuma (M9): `file_read` görevi isteğe bağlı `read` bölümüyle (`start_line`, `max_lines`) bir dosyanın **satır aralığını** okuyabilir. Bölüm yoksa davranış eskisiyle **birebir aynıdır**. Bölge okuma, aynı kanıtlanmış dosya tanıtıcısı üzerinde akış hâlinde çalışır: satır aranırken baytlar sayılıp **atılır**, istenen bölge yakalanır ve bölge 64 KiB yakalama tavanı içinde **kapanmazsa istek reddedilir** — kısmi bir bölge asla başarı olarak dönmez. Böylece 64 KiB üstü olduğu için bugüne kadar hiç okunamayan büyük dosyalar (ör. `jarvis/contracts.py`) satır aralığıyla okunabilir hâle gelir.

## Yapı

- `jarvis/contracts.py`: görev, kapsam, izin, sonuç, doğrulama, bağlam getirme ve protocol sözleşmeleri.
- `jarvis/core.py`: checkpoint bırakan resumable Agent Loop state machine.
- `jarvis/app.py`: composition root, güvenilmeyen task JSON sınırı, komut cephesi ve redakte edilmiş çıktı zarfı.
- `jarvis/cli.py`: yürütülebilir komut yüzeyi ve çıkış kodu eşlemesi.
- `jarvis/memory.py`: SQLite oturum, task, onay, checkpoint, karar ve maliyet defteri.
- `jarvis/registry.py`: priority/suitability/availability/cost tabanlı connector seçimi.
- `jarvis/connectors/`: ortak connector, güvenli Claude Code plan connector'ı ve tek boğaz noktalı containment üzerine kurulu yerel okuma/yazma connector'ları.
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

## Yerel dosya yazma (M5)

`capability: "file_write"` ile izinli kökler içindeki **tek** bir UTF-8 metin
dosyası JARVIS'in kendisi tarafından yazılır. Modele hiçbir filesystem tool
grant'i verilmez.

```json
{
  "objective": "notu güncelle",
  "capability": "file_write",
  "scope": {"action": "write", "target": "notes.txt",
            "data_classification": "internal"},
  "write": {"content": "yeni içerik\n", "mode": "overwrite"}
}
```

Kurallar: M5 **yalnızca `create`** yapar. `overwrite` tanınır fakat
**desteklenmez ve kapalı düşer**: güvenli bir overwrite "bu hedefi ancak hâlâ
onaylanan nesne ise değiştir" anlamına gelen bir mutasyon gerektirir ve ne
Windows'ta ne Linux'ta buradan erişilebilen bir primitif bunu ifade eder;
kontrol + koşulsuz replace bir zorlama değil, arkasında pencere olan bir
kontroldür. Var olan bir dosyaya `create` de reddedilir. `content` tam `str`, katı UTF-8
(lone surrogate reddedilir), NUL'suz ve en çok `LOCAL_WRITE_MAX_BYTES = 64
KiB` olmalıdır; sessiz kesme yoktur. Yazılan byte'lar birebir
`content.encode("utf-8")`'tir — newline normalizasyonu, redaksiyon veya
dönüşüm uygulanmaz.

Yazma her modda onay ister ve onay **tam byte'lara + hedefin önceki
durumuna** bağlanır. Kurulumun iki ucu da addır değil **nesnedir**: dosya
kendi açık handle'ı üzerinden yerine konur ve hedef kökü, doğrulanmış parent
dizinin kendisidir — altına yalnızca göreli yaprak ad verilir (Windows'ta
`NtSetInformationFile`/`FileRenameInformation` + `RootDirectory` = parent
handle, geçici dosya da aynı handle altında `NtCreateFile`/`FILE_CREATE`;
Linux'ta pinlenmiş parent descriptor üzerinden `/proc/self/fd/N` link'i).
Böylece ne geçici adın değiştirilmesi kurulanı değiştirebilir, ne de
doğrulamadan sonra bir üst dizinin junction ile yeniden yönlendirilmesi
byte'ları onaylanan kökün dışına taşıyabilir. Hedefin yokluğu kontrol
edilmez, **kernel tarafından zorlanır**. Bu primitifi sunamayan bir platformda yazma
kapalı düşer. Ayrıca yürütme, kalıcı `prepared` otorite kaydıyla
karşılaştırılır ve ayrışırsa mutasyondan önce reddedilir. `mkdir`, silme,
rename, glob, çok dosyalı yazma ve `file_restore` yoktur. Var olan hedef normal dosya olmalıdır
ve `st_nlink > 1` (hard link) reddedilir.

**Geri alma yoktur.** JARVIS pre-image saklamaz, bu yüzden hiçbir şey önceki
dosyayı geri koyamaz. Yazılacak ham byte'lar, `resume` task'ı yalnızca depodan
yeniden kurduğu için, task'ın **çalıştırılabilir ömrü boyunca**
`tasks.state_json` içinde birebir saklanır ve terminal durumda aynı korumalı
işlem içinde **mantıksal olarak** silinir. Bu fiziksel/adli bir silme
**değildir**: `secure_delete` kapalı, `auto_vacuum` NONE ve `VACUUM` yoktur,
bu yüzden eski byte'lar serbest bırakılmış SQLite sayfalarında, WAL'da veya
dosya sistemi artığında kalabilir. Veritabanı yerel, tek kullanıcılı ve
şifrelenmemiştir.

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
`objective`, `capability`, `project_id`,
`scope.{action,target,destination,data_classification}`,
`context.{working_directory,inputs,local_read_task_id}`,
`preferences.{preferred_connector,needs_clarification,clarification_question,assumption}`
ve `write.{content,mode,model_answer_task_id}` (`content` XOR
`model_answer_task_id`; `write_source` girdiden asla alınmaz) alanlarıdır. Tanınmayan veya korumalı alanlar (`id`, `status`, `stage`, `plan`,
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

### Yerel dosya okuma (görev kapsamlı)

`capability: "file_read"` ve `scope.target` ile bir task, izin verilen kökler
içindeki **tek** bir UTF-8 metin dosyasını okutur. Byte'ları JARVIS okur;
Claude alt sürecine `Read`/`Glob`/`Grep` veya doğrudan filesystem erişimi
verilmez (`--tools ""` boş kalır). Böylece modele ulaşan her şey, onayın
attest ettiği kanonik payload byte'larının içindedir.

Sıra zorunludur: kanonik `resolve` → kök içi containment → secret-file deny
→ temsil edilebilir root-göreceli ad → normal dosya → onaylanan dizin zinciri
üzerinden açma ve açılan **nesnenin** hâlâ kök içinde olduğunun kanıtı →
açılan tanıtıcının aynı dosya olduğunun `st_dev`/`st_ino` kanıtı → 64 KiB
fiziksel tavan → NUL/UTF-8 kontrolü → newline normalizasyonu →
`redact_credentials` + `redact_local_paths` → 4.000 karakter kesme.
**64 KiB'ı aşan dosya reddedilir, sessizce kesilmez.** 4.000 karakterlik
temsil tavanı ayrı bir kavramdır ve kesme redaksiyondan *sonra* uygulanır,
böylece redaksiyonun metni büyütmesi tavanı aşamaz.

Zincir, başladığı kök kadar güvenilirdir: her izinli kökün
`(st_dev, st_ino)` kimliği otorite kümesi kurulurken kaydedilir ve açma
anında yeniden kanıtlanır, böylece kökün kendisinin yeniden
yönlendirilmesi de sıfır byte verir.

Açma öncesi `resolve()` bir yol adını kanıtlar, bir nesneyi değil: doğrulama
ile `open` arasında bir **üst dizin** symlink/junction ile değiştirilirse
açılan dosya onaylanan zinciri hiç kat etmemiş olur. POSIX'te zincir dizin
tanıtıcılarıyla `O_NOFOLLOW` yürünür (kök dışına geçici olarak bile hiçbir şey
açılmaz); Windows'ta zincir önce no-follow taranır ve açılan tanıtıcıya kendi
authoritative son yolu (`GetFinalPathNameByHandleW`) sorulur. Kök dışına düşen
her sonuç reddedilir ve sıfır byte verir.

Hedef, yazımı ne olursa olsun (`README.md`, `.\README.md`, `./docs/../docs/x`,
symlink) yürütmeden önce tek bir yerde `canonical_path`'ten geçirilir; scope
otoritesi, izin fingerprint'i, attestation ve yürütme aynı kanonik kimliği
görür. Onaydan sonra ad başka bir dosyaya yönlendirilirse fingerprint değişir
ve eski grant hiçbir şeyi yetkilendirmez. Bir **hard link** bir link değil, aynı dosyanın ikinci adıdır: çözülecek
bir şey yoktur ve authoritative son yol handle'ın açıldığı kök-içi addır,
yani kök dışındaki bir dosyanın kök-içi adı tüm kontrolleri geçerdi. M3
bu yüzden açılan normal dosyanın `st_nlink > 1` olmasını reddeder;
kontrol tek `read` çağrısından önce yapılır, dizin ele alışı değişmez ve
meşru hardlink'li dosyaların okunamaması şimdilik kabul edilmiş bir
uyumluluk maliyetidir.

Gömülü NUL taşıyan bir hedef - hiçbir platformun
adlandıramayacağı tek yapısal durum - döngü içinde `INVALID_TASK` ile
reddedilir; `canonical_path` totaldir, yani çözülemeyen bir hedef asla
döngünün dışına istisna fırlatıp kayıtlı bir task'ı terminal durumsuz
bırakamaz. `api_key=...` gibi kimlik-bilgisi
taşıyan bir dosya **adı**, onay isteği oluşmadan önce reddedilir (redakte
edilmez): yol bir kaynak tanımlayıcısıdır ve onaylanan kimlik ile görüntülenen
kimlik ayrışamaz. `apikey.md`, `tokenizer.py`, `secret_handling.md` okunabilir
kalır.

Secret-file deny politikası bilinçli olarak dardır ve glob kullanmaz: tam
adlar (`.env` ve `.env.*`, `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519`,
`.npmrc`, `.pypirc`, `.netrc`, `_netrc`), tam uzantılar (`.pem`, `.key`,
`.pfx`, `.p12`) ve iki dizin kuralı (`.ssh/**`, `.aws/credentials`).
`id_generator.py`, `id_map.json`, `id_rsa.pub`, `key.py`, `keys.json`,
`apikey.md` ve `environment.py` okunabilir kalır. Politika kanonik yol
üzerinde çalışır, yani sır dosyasına işaret eden symlink de reddedilir;
deny edilen dosya hiç okunmaz.

Sonuç, tipli ve görev kapsamlı bir alanda (`Task.local_read`) yaşar: kalıcı
proje hafızası değildir, hiçbir memory/decision satırı üretmez ve getirmenin
5 öğe / 4.000 karakter kotasını tüketmez. Snapshot'ın yapısal sözleşmesi tek
bir yerde tanımlıdır (`local_read_snapshot_refusal`) ve yükleme, yürütme,
doğrulama, zarf ve Claude egress sınırlarının hepsi aynı fonksiyonu çağırır;
kalıcı satırın `classification` alanı yalnızca tam tanınan bir
`DataClassification` dizesi olduğunda kabul edilir (geçerli SECRET dahil);
okunamayan değer SECRET'e düşürülmez, snapshot tamamen atılır.
Tamamlanan bir `file_read` görevi **decision üretmez**: `finalize_task`
`decision=None` ile çağrılır ve satır hiç yazılmaz, finalizasyonun geri
kalanı (terminal task, session temizliği, usage, terminal event,
atomiklik) aynı kalır - böylece sonraki görevler okumadan hiçbir şey
miras almaz. Bir NTFS **named stream** (`.env:named`, `secret.pem:s:$DATA`)
onay öncesinde ve handle'dan türetilen authoritative yolda reddedilir;
normal sürücü harfi (`C:\...`) etkilenmez.
Sözleşmeyi ihlal eden kalıcı satır boş snapshot'a düşer, tek onarım aşırı
büyük excerpt'in yeniden sınırlanmasıdır. Excerpt ayrıca **kanonik olmak
zorundadır**: `bound_local_text` bir sabit nokta olduğu için
`bound_local_text(excerpt) == excerpt` değilse satır gerçek bir okumadan
gelmiş olamaz ve yeniden redakte edilmek yerine reddedilir. Kimlik bilgisi
politikası `secret_access_key`/`access_key_id` gibi bileşik anahtar
adlarını ve etiketsiz AWS erişim anahtarı kimliklerini de kapsar. Sınıflandırma connector metadata'sı
değildir — çekirdek onu okuyan task'ın kendi `scope.data_classification`
değerinden koşulsuz yeniden bağlar. Yan etki
profili LOCAL_READ / LOW / reversible'dır, yani STRICT onay ister, SESSION ve
SMART_AUTO otomatik geçirir; rota `NO_ADDITIONAL_CHARGE` olarak açıkça beyan
edilir. SQLite şeması değişmez (v6).

Sonraki bir Claude task'ı `context.local_read_task_id` ile bu okumaya
**referans** verebilir; içerik hiçbir zaman girdiden kabul edilmez. Referans
yalnız aynı oturumdaki, COMPLETED, yapısal olarak geçerli ve sınıflandırma
merdiveninin izin verdiği bir kaynağa çözülür, aksi halde `INVALID_TASK`
(çıkış 64) ile kapalı düşer. Merdivenin kaynak tarafı **kaynak task'ın kendi
`scope.data_classification` değeridir**; kalıcı snapshot'ın classification'ı
otorite değildir ve tampered bir satır otoriteyi ne düşürebilir ne
yükseltebilir. Dışa çıkan blok tam olarak
`{notice, path, byte_size, truncated, content}` alanlarını taşır; `sha256`,
sınıflandırma, okuma zamanı ve her türlü mutlak yol yerelde kalır. Zarf,
okunan dosyanın sınırlanmış görünümünü `local_read` bloğunda gösterir.

### Tipli model cevabı (M6)

Harici modelin cevabı artık kullanılabilir biçimde geri gelir. Daha önce
kullanıcının gördüğü tek temsil, `AgentCore`'un `ToolResult`'ı H4 tanılama
hattından geçirmesiyle oluşan özetti: satırlar tek satıra daraltılmış,
mutlak yollar `[PATH]`'e indirilmiş ve metin 240 karakterde kesilmiş
oluyordu. Bu bir tanılama için doğrudur, bir teslimat için yıkıcıdır.

M6, cevabı tipli ve sınırlı bir `Task.model_answer` kaydında taşır ve zarfa
sabit bir `model_answer` bloğu ekler:
`{content, character_count, sha256, truncated, produced_at}`. İçerik
**yapısını korur** — satır sonları, girinti, boş satırlar, sondaki satır sonu
ve metinde geçen mutlak dosya yolları olduğu gibi kalır — en çok
`MODEL_ANSWER_MAX_CHARS = 8.000` karakterdir ve kalıcılaşmadan önce
paylaşılan kimlik bilgisi politikasıyla redakte edilir (redaksiyon kesmeden
**önce** çalışır). Kesme olduysa bant içi bir işaret eklenmez; bilgi tipli
`truncated` bayrağıyla taşınır. `result.summary` ve karar/geçmiş kaydı
**değişmeden** eski 240 karakterlik tanılama temsili olarak kalır.

Cevap **güvenilmeyen gelen model verisidir**: gösterim içindir, otorite
değildir. Onayı, attestation'ı, fingerprint'i, faturalandırmayı, planı,
connector seçimini, yürütmeyi veya doğrulama otoritesini etkilemez; proje
hafızasına yazılmaz, getirmeye girmez ve Claude'a geri gönderilmez
(`EGRESS_TOOL_GRANTS` boş kalır, giden payload birebir aynıdır). `sha256`
yalnızca kalıcılaşan metnin bütünlük kanıtıdır — köken kanıtı veya
attestation değildir. `truncated`, iki sınırlama aşamasından (64.000
karakterlik kaynak penceresi veya 8.000 karakterlik çıktı tavanı) herhangi
birinin içerik attığını söyleyen tek genel bayraktır; üretici tarafından
atanır ve **kesme tarihi atılan metin gittikten sonra yeniden kurulamaz**, bu
yüzden kesilmiş bir cevap meşru olarak tavandan çok kısa olabilir. Doğrulama
yalnızca yapısaldır: kaydın geçerliliği, metnin boş olmaması ve digest/sayaç
uyumu denetlenir; cevabın **doğruluğu, isabeti veya halüsinasyon içermediği
doğrulanmaz**.
Prompt injection çözülmüş değildir: zehirlenmiş yerel bir dosya modelin ne
söyleyeceğini etkileyebilir, fakat cevap eylemsiz gösterim verisi olarak
kalır ve ondan türeyen her eylem yeni ve ayrıca incelenen bir yetki
sınırından geçmek zorundadır. SECRET bir görev modeli hiç çalıştırmadığı
için başarılı bir model cevabı da üretmez.

### Açık model cevabı aktarımı (M7)

Tamamlanmış bir model görevinin cevabı, aynı oturumdaki **create-only** bir
`file_write` görevinin tam byte kaynağı olarak açıkça seçilebilir:

```json
{
  "objective": "önerilen testi oluştur",
  "capability": "file_write",
  "scope": {"action": "write", "target": "tests/test_new.py",
            "data_classification": "internal"},
  "write": {"mode": "create", "model_answer_task_id": "<tamamlanmış model task id>"}
}
```

Kurallar: `write.content` **XOR** `write.model_answer_task_id` zorunludur;
referans tam `str`, boş değil, NUL'suz, çevre boşluksuz (kanonikleştirilmez,
reddedilir), en çok 64 karakterdir ve yalnız `file_write` görevinde geçerlidir;
`context` altında verilemez. Cevap **güvenilmeyen veridir**; tek yetki sınırı
mevcut zorunlu `LOCAL_WRITE` onayıdır ve o onay tam byte'lara, kullanıcının
verdiği hedefe, `create` moduna ve ön-duruma bağlanır — hiçbir model çıktısı
hedefi, modu veya onayı seçemez. İçerik **birebir** yazılır
(`model_answer.content.encode("utf-8")`): strip, code-fence ayrıştırma,
newline/girinti dönüşümü veya kimlik bilgisi restorasyonu yoktur; M6'nın
`[REDACTED]` çıktısı dosyaya olduğu gibi girer. Truncated, boş/boşluk, NUL'lu,
bozuk, tamamlanmamış, başka oturumdaki veya harici-model olmayan bir
üreticiden gelen cevaplar CONTEXT_RETRIEVAL'da, plan ve onay öncesinde
reddedilir. Sınıflandırma yalnız yukarı/eşit yönde akar (kaynak SECRET asla;
SECRET hedef izinli, çünkü yerel yazma egress değildir).

Kaynak CONTEXT_RETRIEVAL'da bir kez çözülür ve `context_retrieved`
checkpoint'i byte'ları görevin `write_content` alanında dondurur; onay
dondurulmuş byte'ları bağlar, kaynak sonrasında hiç okunmaz. Görevin dayanıklı
yazma-kaynağı kimliği (`write_source`: `direct` | `model_answer` | `invalid`,
`write_source_task_id`) terminal durumda silinmez; bozuk veya tutarsız kalıcı
kimlik `invalid` yüklenir ve terminal `INVALID_TASK` ile kapanır — bir aktarım
asla sessizce direct yazmaya dönüşmez. Onay gerekçesi kaynak task id'sini,
byte sayısını ve dondurulmuş içeriğin sha256'sını gösterir; bu digest kaynak
zarfındaki `model_answer.sha256` ve tamamlanan yazmanın
`local_write.intended_sha256` değerine eşittir (yalnız içerik sürekliliği;
doğruluk veya provenance kanıtı değildir). Zarf şeması 5, SQLite şeması 6
değişmez. Ayrıntılar `ARCHITECTURE.md` §4.14'tedir.

### Sınırlı çok dosyalı okuma bağlamı (M8)

Bir harici-model görevi, aynı oturumda tamamlanmış **1..3** `file_read`
görevini tek bir sınırlı demet olarak referans verebilir:

```json
{
  "objective": "iki modülü karşılaştır",
  "capability": "code_analysis",
  "scope": {"action": "analyse", "target": "repo"},
  "context": {
    "local_read_task_ids": ["<task-a>", "<task-b>", "<task-c>"]
  }
}
```

Kurallar: liste **tam** `list` olmalı, 1..3 öğe taşımalı, her öğe tam
`str`, boş değil, NUL içermez, baş/son boşluk taşımaz (kırpılmaz, **reddedilir**)
ve en fazla 64 karakterdir. Tekrar eden id reddedilir (sessiz tekilleştirme
yoktur). `local_read_task_id` (M3) ile `local_read_task_ids` (M8) **aynı anda
verilemez**; tekil davranış hiç değişmemiştir. Tek elemanlı liste de çoğuldur
ve yeni blok üzerinden gider.

Kullanıcının verdiği **sıra korunur**: `[A,B]` ile `[B,A]` farklı görevlerdir
ve farklı bir onay fingerprint'i üretir. Referanslar tipli `Task` alanlarına
çözülür ve `task.context` içinde **tutulmaz** — `context` Claude yükünde
birebir serileşir, dolayısıyla orada tutmak kaynak id'lerini dışarı sızdırırdı.

Çözümleme CONTEXT_RETRIEVAL'da bir kez çalışır ve **hep-ya-hiç**'tir: her
kaynak için aynı-oturum, COMPLETED, `capability == "file_read"`, kayıtlı
üretici connector, üreticinin manifest'inde `file_read`, geçerli snapshot ve
sınıflandırma merdiveni kapıları geçilir; sınıflandırma daima kaynak görevin
kendi `scope`'undan yeniden bağlanır (kalıcı etikete asla güvenilmez). Üçüncü
kaynak düşerse demet **boş kalır**, görev terminal `INVALID_TASK` olur ve
hiçbir connector seçimi, onay, girişim veya model çağrısı oluşmaz. Kısmi bir
alt küme asla gönderilmez.

Bütçe: en fazla 3 kaynak ve toplam **12000 Python karakteri** (`len(str)`,
UTF-8 byte değil). İkinci bir kırpma katmanı yoktur; aşım kırpma değil
**ret**tir. Onay gerekçesi kaynak sayısını, toplam karakteri ve sıralı kaynak
kümesinin sha256'sını taşır — ham task id'leri **taşımaz** — ve
`display_digest` üzerinden fingerprint'e bağlanır, böylece id veya sıra
değişimi eski onayı geçersizleştirir. Dondurulmuş içerik zaten
`outbound_digest` üzerinden bağlıdır.

Claude yüküne yalnız `local_read_context` bloğu eklenir: `notice` ve sıralı
`sources` (her biri `path`, `byte_size`, `truncated`, `content`). Kaynak task
id'leri, mutlak yollar, sha256, `read_at` ve sınıflandırma gönderilmez.
Dayanıklı kimlik (`local_read_context_kind`: `none` | `references` | `invalid`)
terminal durumda silinmez; yalnız **tüm** M8 alanları eksik olan eski satır
`none` yüklenir, kısmi/bozuk durum `invalid` olur. Beklenen demet bozuksa
connector sınırı isteği sessizce bloksuz göndermez, `EGRESS_REFUSED` ile
kapalı düşer. Zarf şeması 5, SQLite şeması 6 değişmez. Ayrıntılar
`ARCHITECTURE.md` §4.15'tedir.

### Sınırlı bölge okuma (M9)

Bir `file_read` görevi, dosyanın tamamı yerine bir **satır aralığını** okuyabilir:

```json
{
  "objective": "sözleşme bölümünü oku",
  "capability": "file_read",
  "scope": {"action": "read", "target": "C:/proje/jarvis/contracts.py"},
  "read": {"start_line": 1900, "max_lines": 90}
}
```

`read` bölümü **yoksa** davranış M3'teki HEAD okumasıyla birebir aynıdır;
64 KiB üstü bir dosya yine sessizce kırpılmadan reddedilir. Bölüm varsa
`start_line` (1..1.000.000) ve `max_lines` (1..200) **zorunludur**, ikisi de
tam `int` olmalıdır (`bool` kabul edilmez), boş `"read": {}` reddedilir ve
bölüm yalnız `file_read` için geçerlidir.

Fiziksel okuma iki fazlıdır ve mevcut açık tanıtıcının tüm kanıtlarını
(izinli kök, symlink/reparse reddi, dev/inode yeniden bağlama, hardlink
reddi, ADS reddi, gizli-dosya politikası) aynen kullanır: **SEEK** fazında
sabit parçalar okunur, ham `0x0A` sayılır, her parçada NUL reddedilir ve
baytlar **atılır** (4 MiB tarama tavanı); **CAPTURE** fazında istenen satırlar
ham olarak biriktirilir. Bölge yalnızca 64 KiB yakalama tavanı içinde
**kapanırsa** başarılıdır — ya `max_lines` satır tamamlanır ya da EOF'a
ulaşılır. Kapanmazsa istek reddedilir ve gerekçe daha az satır istenmesini
söyler. Çözümleme (decode) yalnızca bölge kapandıktan sonra yapılır; bu
yüzden başarılı bir bölge daima satır sonunda veya EOF'ta biter ve rastgele
bir tavan baytı hiçbir zaman kod noktası ortasından kesilmiş hâlde
çözülmez.

Snapshot iki yeni alan kazanır: `start_line` ve `end_line`. Bunlar **sınırlı
excerpt'in** temsil ettiği kaynak satır aralığını tanımlar — ham yakalamanın
değil. Karakter tavanı bir satırı ortadan keserse o satır `end_line`'a
**dâhildir** (okuyucu onun bir kısmını görebiliyor). `byte_size`/`sha256` ise
**tam ham yakalamayı** tanımlar; bu, HEAD okumasındaki ilişkinin aynısıdır
(orada yakalama tüm dosyadır). `truncated` tek bir anlam taşır: **görünen
kısım yakalamanın tamamı değildir** — istenen bölgenin kendisinin kesildiği
anlamına asla gelmez. Dolayısıyla `truncated == false` iken dönen satır
sayısı `max_lines`'tan azsa bu **EOF** demektir.

Dayanıklı kimlik `LocalReadKind {head, region, invalid}` (`local_read_kind`,
`read_start_line`, `read_max_lines`) fail-closed yüklenir: yalnız **tüm** M9
alanları eksik olan eski satır `head` olur; kind silinip pencere kalırsa,
`head` bir pencere taşırsa veya değerler kanonik değilse `invalid` olur ve
connector reddeder — bir bölge okuması asla sessizce HEAD okumasına dönüşüp
dosyanın başını "1900. satır" etiketiyle geri veremez. M3 tekil hop ve M8
çoklu bağlam bölge snapshot'larını hiçbir ek üretici/tüketici mantığı
olmadan tüketir; Claude yüküne `start_line`/`end_line` yalnız bölge
snapshot'ları için eklenir, HEAD yükü byte-uyumlu kalır. SQLite şeması 6,
çıktı zarfı 5 değişmez. Ayrıntılar `ARCHITECTURE.md` §4.16'dadır.

**Kabul edilen artık:** 64 KiB'den büyük **tek bir satır** ne HEAD ne de
REGION okumasıyla okunabilir (HEAD dosya tavanını aşar, REGION satır
kapanmadığı için reddeder). Küçültülmüş/minified tek satırlık büyük dosyalar
bu yüzden okunamaz kalır; bu M9 kapsamı dışındadır ve farklı bir ilkel
gerektirir.

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

Status dışı zarf (`schema_version=5`) tek deterministik `progress` nesnesi
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
+ `LocalFileReadConnector` (aynı tek kök; açıkça boş bir kök kümesi geçersiz
yapılandırmadır ve çalışma dizinine düşmez) → kayıt → `AgentCore` →
`JarvisApp`.
Bu kurulum availability, faturalandırma yoklaması, dosya erişimi veya model
çalıştırması yapmaz.

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
