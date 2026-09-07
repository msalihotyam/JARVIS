# JARVIS Geliştirme Panosu — güncelleme

Yalnızca geliştirme aracı. LLM yok, ağ yok, bağımlılık yok.

## Ne zaman güncellenir

Yalnızca şunlardan biri değiştiğinde:

- genel ilerleme
- aktif milestone
- sonraki adım
- yetenek durumu
- engel durumu
- büyük bir tamamlanma
- şema / sistem sağlığı başlığı

Sıradan bugfix ve test tekrarları pano güncellemesi **gerektirmez**.

## Nasıl

1. `status.json` içinde yalnızca ilgili alanları düzenle. Özetleri tek kısa cümle tut.
2. Çalıştır:

   ```
   python dev/dashboard/render_dashboard.py
   ```

3. `dashboard.html` dosyasını aç ve değiştirdiğin alanları kontrol et.
4. Geçmiş bölümleri (`recent`, tamamlanmış yetenekler) yanlış hâle gelmedikçe yeniden yazma.

`status.json` doğruluk kaynağıdır. `dashboard.html` **üretilir**: bilinçli
olarak `.gitignore` ile takip dışıdır, elle düzenlenmez ve commit edilmez.
Yeniden render almak kanonik main'i kirletmez.

## Git'ten türetilen alanlar

Kanonik HEAD, temiz/kirli durumu ve `origin/main` karşısındaki önde/geride
sayısı render anında yerel Git'ten türetilir (fetch yok, ağ yok). Bunları
`status.json` içine yazma. Git yanıt veremezse pano `bilinmiyor` gösterir.
