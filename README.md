# Borsa Günlüğü 📈

BIST şirket haberleri, KAP bildirimleri, günün yükselen/düşen hisseleri, altın analizi ve BES fon takibi — günde iki kez otomatik derlenen kişisel bülten. `demiryolu-gunlugu` ile aynı mimari: Python + GitHub Actions + GitHub Pages.

## Ne yapıyor?

Her hafta içi **08:45** (sabah baskısı) ve **18:45** (akşam baskısı) TSİ'de:

1. **KAP** — BIST 100 şirketlerinin son bildirimlerini çeker; sözleşme, borçlanma, yatırım, temettü, geri alım gibi kategorilere ayırır.
2. **RSS** — Bloomberg HT, Dünya, AA Ekonomi, Investing TR, BigPara akışlarını tarar; BIST 100 şirketleriyle eşleşen haberleri ve makro haberleri ayıklar.
3. **Fiyatlar** — Yahoo Finance'ten BIST 100/30 hisselerini çekip günün en çok yükselen/düşen 5'er hissesini çıkarır.
4. **Altın** — ons + USD/TRY'den gram altını hesaplar; 1 gün / 1 hafta / 1 ay değişimini gösterir.
5. **TEFAS** — BES fonlarının (varsayılan: GEL, GHA) fiyat ve getirilerini çeker.
6. **Gemini** — tüm veriden günün özeti, altın yorumu ve fon notu üretir (key yoksa bülten AI'sız yayımlanır, sistem durmaz).
7. Sonucu `docs/index.html` olarak yazar ve `data/` altına JSON arşivler.

## Kurulum (bir kere, ~10 dk)

1. GitHub'da **borsa-gunlugu** adında yeni bir repo aç ve bu klasördeki her şeyi push'la.
2. **Settings → Pages** → Source: *Deploy from a branch* → Branch: `main`, klasör: `/docs` → Save.
3. **Settings → Secrets and variables → Actions → New repository secret** → adı `GEMINI_API_KEY`, değeri Google AI Studio'dan aldığın key.
4. **Actions** sekmesinde workflow'u onayla, "Borsa Bülteni" → **Run workflow** ile ilk çalıştırmayı elle yap.
5. Birkaç dakika sonra: `https://KULLANICI_ADIN.github.io/borsa-gunlugu/`

> Repodaki mevcut `docs/index.html` örnek (mock) veriyle üretilmiş bir önizlemedir; ilk gerçek çalıştırmada üzerine yazılır.

## Gemini key sorunu için kontrol listesi

Demiryolu projesindeki bağlantı sorunu büyük olasılıkla şunlardan biriydi; sırayla dene:

- Key'i **Google AI Studio**'dan al (aistudio.google.com → Get API key). Google Cloud Console'dan alınan kısıtlı key'ler `generativelanguage.googleapis.com`'a kapalı olabilir.
- Key'in **API restriction** ayarında "Generative Language API" seçili (veya kısıtlama yok) olmalı.
- Secret adının **tam olarak** `GEMINI_API_KEY` olduğundan emin ol (boşluk/küçük harf hatası sık görülür).
- Hâlâ olmazsa Actions logunda `Gemini özeti alınamadı:` satırındaki hata koduna bak: `400` → istek/model adı, `403` → key kısıtlaması, `429` → kota.
- Model adını değiştirmek istersen workflow'a `GEMINI_MODEL` env değişkeni ekleyebilirsin (varsayılan `gemini-2.0-flash`).

## Kendine göre ayarlama

Her şey `config.json`'da:

- `bes_fonlari` — takip edilecek TEFAS fon kodları. **GEL ve GHA varsayılan; kendi BES ekranındaki kodlarla doğrula/değiştir.**
- `rss_kaynaklari` — kaynak ekle/çıkar.
- `bist30` / `bist100_ek` — endeks bileşenleri çeyrek dönemlerde değişir; Borsa İstanbul duyurularına göre arada güncelle.
- `haber_penceresi_saat` — her baskının kaç saat geriye bakacağı (varsayılan 14; iki baskı arası boşluk kalmasın diye payı var).
- Saatleri değiştirmek için `.github/workflows/bulten.yml` içindeki cron satırları (UTC = TSİ − 3).

## Yerelde deneme

```bash
pip install -r requirements.txt
python scraper.py --mock   # örnek veriyle tasarımı gör
python scraper.py          # canlı veri (GEMINI_API_KEY env olarak verilebilir)
```

## Bilinen kırılganlıklar

- **KAP** resmi bir API sunmaz; `kap.org.tr/tr/api/disclosures` uç noktası değişirse `fetch_kap` içindeki alan adlarını güncellemek gerekebilir. Kod bu durumda çökmez, o bölümü boş geçer.
- **TEFAS** uç noktası da resmi değildir; aynı ilke geçerli.
- Yahoo Finance BIST verisi ~15 dk gecikmelidir.

---
*Bu bülten bilgilendirme amaçlıdır, yatırım tavsiyesi değildir.*
