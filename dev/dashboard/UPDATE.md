# JARVIS Geliştirme Panosu — güncelleme

Yalnızca geliştirme aracı. LLM yok, ağ yok, bağımlılık yok.

## Milestone güncellemesi (normal akış)

1. `status.json` içinde ilgili milestone'un `state` değerini değiştir
   (`completed` / `in_progress` / `not_started` / `deferred`).
2. Gerekiyorsa `milestone`, `next_step` ve `blockers` alanlarını güncelle.
3. Çalıştır:

   ```
   python dev/dashboard/render_dashboard.py
   ```

Yüzdeler bundan sonra otomatik hesaplanır. Elle yüzde yazma; `status.json`
içinde yüzde alanı yoktur.

Sıradan bugfix ve test tekrarları pano güncellemesi **gerektirmez**.

## İlerleme modeli

- `completed` = 1.0, `in_progress` = 0.0, `not_started` = 0.0
- **Kısmi kredi yoktur.** İlerleme "usable-v1 roadmap'inin ne kadarı doğrulanıp
  kapandı" sorusunun cevabıdır; bir milestone'a başlanmış olması yüzdeyi
  artırmaz, yalnızca kabul edilip kapanması artırır.
- `deferred` milestone paydadan tamamen düşer (usable-v1 dışı iş, ilerlemeyi
  aşağı çekmez)
- yetenek yüzdesi = kendi milestone ağırlıklarının kredisi
- genel yüzde = yetenek yüzdelerinin `weight` ile ağırlıklı ortalaması
- kod hacmi, test sayısı, commit sayısı ve geçen süre ilerlemeyi **artırmaz**
- bir milestone tam krediyi ancak kabul edilip canonical olduğunda alır

Model doğru mu diye tek komutluk kontrol:

```
python dev/dashboard/render_dashboard.py --selftest
```

## Üretilen dosya

`status.json` doğruluk kaynağıdır. `dashboard.html` **üretilir**: bilinçli
olarak `.gitignore` ile takip dışıdır, elle düzenlenmez ve commit edilmez.
Yeniden render almak canonical main'i kirletmez.

Pano sekmesi görünürken 60 saniyede bir kendini yeniler; sekme arka plandayken
yenilemez, tekrar öne geldiğinde bir kez yeniler. Bu tamamen yerel `file://`
davranışıdır: ağ, sunucu veya arka plan süreci yoktur. Aralık `status.json`
içindeki `refresh_seconds` alanıdır; `0` yaparsan script hiç üretilmez.

## Git'ten türetilen alanlar

Canonical HEAD, temiz/kirli durumu ve `origin/main` karşısındaki önde/geride
sayısı render anında yerel Git'ten türetilir (fetch yok, ağ yok). Bunları
`status.json` içine yazma. Git yanıt veremezse pano `bilinmiyor` gösterir.
