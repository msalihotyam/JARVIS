# JARVIS Core

JARVIS, Windows 11 üzerinde yerel çalışan, modelden bağımsız kişisel çalışma orkestratörünün API gerektirmeyen çekirdeğidir. Bu aşama yalnızca standart Python kütüphanesini kullanır ve hiçbir ücretli model çağrısını kendiliğinden başlatmaz.

## Güvenlik özellikleri

- Üç izin modu vardır: `STRICT`, `SESSION`, `SMART_AUTO`.
- `SMART_AUTO` yalnızca düşük riskli ve geri alınabilir işleri otomatik çalıştırır.
- Dışarı veri gönderimi, ücret/faturalandırma, silme, push/deploy/yayın ve finansal/form gönderimi her modda açık onay ister.
- Onaylar task, session, normalize hedef, connector, destination, veri sınıfı, fingerprint ve süre ile bağlanır.
- Aylık hedef ve sert üst sınır 20 USD'dir. Üst sınır yalnızca göreve özel açık override onayıyla aşılabilir.
- Claude Code connector yalnızca read-only araçlar, safe mode, boş/strict MCP, plan permission mode, no-session-persistence ve çağrı başı bütçe sınırı kullanır.

## Yapı

- `jarvis/contracts.py`: görev, kapsam, izin, sonuç, doğrulama ve protocol sözleşmeleri.
- `jarvis/core.py`: checkpoint bırakan resumable Agent Loop state machine.
- `jarvis/memory.py`: SQLite oturum, task, onay, checkpoint, karar ve maliyet defteri.
- `jarvis/registry.py`: priority/suitability/availability/cost tabanlı connector seçimi.
- `jarvis/connectors/`: ortak connector ve güvenli Claude Code plan connector'ı.
- Eski `Agent Core`, `Config`, `Connectors`, `Memory`, `Tools` dosyaları uyumluluk sarmalayıcıları olarak korunur.

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

## Yerel durum kontrolü

```powershell
python -m jarvis status
```

Bu komut yalnızca yerel ayar ve SQLite şema durumunu gösterir; dış servise bağlanmaz.

## Bilinen dış blocker

Canlı Claude Code doğrulaması bu çalışmanın kapsamı dışında tutuldu. Önceki kontrolde yerel Max hesabının OAuth belirteci süresi dolmuştu. Kullanıcı yeniden giriş yaptıktan sonra bile canlı çağrı ancak ayrıca istendiğinde yapılmalıdır.
