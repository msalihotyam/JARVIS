# JARVIS V2+ — Roadmap, Decisions & Reference Architecture

**Belge tarihi:** 2026-09-19  
**Son güncelleme:** 2026-09-29 — Claude / ChatGPT desktop 2026-09 referansı (§3.7) ve yeni DO NOT COPY maddeleri (§12) eklendi.
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
- **Kalan V2.0 sırası:** 6B → 6C → Gate 7 (E2E + paketlenmiş dogfood + rollback; archive/unarchive E2E dahil). Purge kalıntı gizliliğini Gate 7'den önce Salih karara bağlar. Gate 6 UX tamamlanmadan release yok.

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
