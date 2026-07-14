#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Borsa Günlüğü — BIST şirket haberleri, KAP bildirimleri, altın ve BES fon takibi.
Günde iki kez GitHub Actions ile çalışır, docs/index.html üretir.

Kullanım:
    python scraper.py            # canlı veri
    python scraper.py --mock     # örnek veriyle sayfa üret (test için)
"""

import json
import logging
import os
import re
import sys
import time
import html as html_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


def _istek(method, url, tries=3, backoff=3, **kw):
    """Yeniden denemeli HTTP isteği. Türk devlet sitelerinin yavaşlığına karşı."""
    son_hata = None
    for i in range(tries):
        try:
            r = requests.request(method, url, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            son_hata = e
            if i < tries - 1:
                time.sleep(backoff * (i + 1))
    raise son_hata

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("borsa-gunlugu")

ROOT = Path(__file__).parent
TRT = timezone(timedelta(hours=3))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
GRAM_PER_OZ = 31.1034768

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
BIST30 = CONFIG["bist30"]
BIST100 = sorted(set(CONFIG["bist30"] + CONFIG["bist100_ek"]))
ADLAR = CONFIG["sirket_adlari"]


# ---------------------------------------------------------------- yardımcılar

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
         "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


def tr_num(x, nd=2):
    """1234.56 -> '1.234,56' (Türkçe biçim)."""
    if x is None:
        return "—"
    s = f"{x:,.{nd}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def pct_class(p):
    if p is None:
        return "flat"
    return "up" if p >= 0 else "down"


def pct_str(p):
    if p is None:
        return "—"
    isaret = "+" if p >= 0 else ""
    return f"{isaret}{tr_num(p, 2)}%"


def esc(s):
    return html_mod.escape(str(s or ""), quote=True)


def simdi():
    return datetime.now(TRT)


# Bülten günü sabah 08:45'te başlar. 08:45'ten önceki saatler (gece ABD haberleri
# dahil) hâlâ ÖNCEKİ bülten gününe aittir; feed bu sayede gece yarısı değil,
# sabah 08:45'ten sonraki ilk turda sıfırlanır.
GUN_BASLANGICI = (8, 45)


def bulten_gunu(now):
    esik = now.replace(hour=GUN_BASLANGICI[0], minute=GUN_BASLANGICI[1],
                       second=0, microsecond=0)
    gun = now if now >= esik else (now - timedelta(days=1))
    return gun.strftime("%Y-%m-%d")


# ---------------------------------------------------- biriken feed durumu

FEED_PATH = ROOT / "data" / "gunun_feedi.json"


def feed_yukle():
    try:
        return json.loads(FEED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def feed_kaydet(state):
    FEED_PATH.parent.mkdir(exist_ok=True)
    FEED_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                         encoding="utf-8")


def feed_birlestir(mevcut, yeni, eklenme_iso):
    """ID'ye göre tekrarları ayıklar; yalnızca yeni öğeleri ekler."""
    idler = {x.get("id") for x in mevcut if x.get("id")}
    eklenen = 0
    for it in yeni:
        iid = it.get("id")
        if not iid or iid in idler:
            continue
        it = dict(it)
        it["eklenme"] = eklenme_iso
        mevcut.append(it)
        idler.add(iid)
        eklenen += 1
    return mevcut, eklenen


def feed_sirala(items):
    """En yeni üstte: önce öğenin kendi zamanı, yoksa eklenme zamanı."""
    return sorted(items, key=lambda x: x.get("tarih_iso") or x.get("eklenme") or "",
                  reverse=True)


# ------------------------------------------------------------------ 1) KAP

KAP_KATEGORILER = [
    ("Yeni İş / Sözleşme", ["sözleşme", "ihale", "sipariş", "iş ilişkisi", "anlaşma", "proje"]),
    ("Borçlanma",          ["borçlanma", "tahvil", "bono", "kredi", "finansman bonosu", "sukuk", "kira sertifikası"]),
    ("Yatırım",            ["yatırım", "kapasite", "tesis", "fabrika", "üretim hattı", "santral"]),
    ("Pay Geri Alım",      ["geri alım", "geri alınan paylar"]),
    ("Temettü",            ["kâr payı", "kar payı", "temettü"]),
    ("Birleşme / Devralma", ["birleşme", "devralma", "pay devri", "iştirak", "hisse satış", "satın alım"]),
    ("Sermaye",            ["sermaye artırımı", "bedelsiz", "bedelli", "tahsisli"]),
]


def kap_kategori(baslik):
    b = (baslik or "").lower()
    for ad, kelimeler in KAP_KATEGORILER:
        if any(k in b for k in kelimeler):
            return ad
    return "Bildirim"


def fetch_kap_mynet(pencere_saat):
    """Yedek kaynak: KAP'a doğrudan erişilemediğinde Mynet Finans'ın KAP sayfası.
    KAP resmi sitesi yurt dışı sunuculardan sık sık zaman aşımına uğradığı için gerekli."""
    url = "https://finans.mynet.com/borsa/kaphaberleri/"
    try:
        r = _istek("GET", url, tries=2, backoff=3, headers=UA, timeout=30)
        ham = r.text
    except Exception as e:
        log.warning("Mynet KAP yedeği de alınamadı: %s", e)
        return []

    aylar = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6, "Jul": 7,
             "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
             "Oca": 1, "Şub": 2, "Mar": 3, "Nis": 4, "May": 5, "Haz": 6, "Tem": 7,
             "Ağu": 8, "Eyl": 9, "Eki": 10, "Kas": 11, "Ara": 12}

    # Bağlantıları çıkar (varsa), metni de ayrıca tara
    linkler = dict(re.findall(
        r'href="([^"]*kaphaberleri[^"]*)"[^>]*>\s*\*\*\*([A-Z0-9]{3,6})', ham))

    metin = re.sub(r"<[^>]+>", " ", ham)
    metin = html_mod.unescape(metin)

    desen = re.compile(
        r"\*\*\*([A-Z0-9 ,\*]{3,40}?)\*\*\*\s*(.{3,120}?)\s*\(([^()]{3,90})\)\s*"
        r"(\d{1,2})\s+(\w{3})\s+(\d{4})\s+(\d{2}):(\d{2})")

    esik = simdi() - timedelta(hours=pencere_saat)
    hedef = set(BIST100)
    sonuc = []

    for m in desen.finditer(metin):
        kod_ham, sirket, tur, gun, ay_s, yil, sa, dk = m.groups()
        kodlar = [k.strip() for k in re.split(r"[ ,\*]+", kod_ham) if k.strip()]
        eslesen = [k for k in kodlar if k in hedef]
        if not eslesen:
            continue

        ay = aylar.get(ay_s[:3].title()) or aylar.get(ay_s[:3])
        if not ay:
            continue
        try:
            zaman = datetime(int(yil), ay, int(gun), int(sa), int(dk), tzinfo=TRT)
        except ValueError:
            continue
        if zaman < esik:
            continue

        baslik = f"{sirket.strip()} — {tur.strip()}"
        link = "https://finans.mynet.com" + linkler.get(eslesen[0], "/borsa/kaphaberleri/")
        sonuc.append({
            "id": f"kap-my-{eslesen[0]}-{zaman.strftime('%Y%m%d%H%M')}",
            "baslik": baslik,
            "sirket": sirket.strip(),
            "kodlar": eslesen,
            "kategori": kap_kategori(tur),
            "saat": zaman.strftime("%H:%M"),
            "tarih_iso": zaman.isoformat(),
            "link": link if link.startswith("http") else "https://finans.mynet.com/borsa/kaphaberleri/",
        })

    onem = {ad: i for i, (ad, _) in enumerate(KAP_KATEGORILER)}
    sonuc.sort(key=lambda x: onem.get(x["kategori"], 99))
    log.info("KAP (Mynet yedeği): %d ilgili bildirim", len(sonuc))
    return sonuc[:40]


def fetch_kap(pencere_saat):
    """Önce KAP resmi kaynağı; başarısız olursa Mynet yedeği devreye girer."""
    url = "https://www.kap.org.tr/tr/api/disclosures"
    try:
        # Hızlı başarısız ol: KAP yurt dışından sık sık yanıt vermiyor,
        # 3x60sn beklemek her turda 3 dakika boşa harcıyordu.
        r = _istek("GET", url, tries=2, backoff=2,
                   headers={**UA, "Accept": "application/json"}, timeout=20)
        raw = r.json()
    except Exception as e:
        log.warning("KAP resmi kaynak alınamadı (%s) — Mynet yedeğine geçiliyor", e)
        return fetch_kap_mynet(pencere_saat)

    esik = simdi() - timedelta(hours=pencere_saat)
    hedef = set(BIST100)
    sonuc = []
    for item in raw if isinstance(raw, list) else []:
        basic = item.get("basic", item) if isinstance(item, dict) else {}
        baslik = basic.get("title") or basic.get("summary") or ""
        sirket = basic.get("companyName") or basic.get("relatedStocks") or ""
        kodlar_ham = basic.get("stockCodes") or basic.get("relatedStocks") or ""
        idx = basic.get("disclosureIndex") or basic.get("index") or ""
        tarih_ham = basic.get("publishDate") or basic.get("time") or ""

        kodlar = [k.strip().upper() for k in re.split(r"[,;/ ]+", str(kodlar_ham)) if k.strip()]
        eslesen = [k for k in kodlar if k in hedef]
        if not eslesen:
            continue

        # tarih ayrıştırma (KAP 'dd.MM.yy HH:mm' ya da benzeri biçimler kullanır)
        zaman = None
        for fmt in ("%d.%m.%y %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                zaman = datetime.strptime(str(tarih_ham).strip(), fmt).replace(tzinfo=TRT)
                break
            except ValueError:
                continue
        if zaman and zaman < esik:
            continue

        sonuc.append({
            "id": f"kap-{idx}" if idx else f"kap-{re.sub(r'[^a-z0-9]', '', str(baslik).lower())[:40]}",
            "baslik": str(baslik).strip(),
            "sirket": str(sirket).strip(),
            "kodlar": eslesen,
            "kategori": kap_kategori(str(baslik)),
            "saat": zaman.strftime("%H:%M") if zaman else "",
            "tarih_iso": zaman.isoformat() if zaman else "",
            "link": f"https://www.kap.org.tr/tr/Bildirim/{idx}" if idx else "https://www.kap.org.tr",
        })

    onem = {ad: i for i, (ad, _) in enumerate(KAP_KATEGORILER)}
    sonuc.sort(key=lambda x: onem.get(x["kategori"], 99))
    if not sonuc:
        log.warning("KAP resmi kaynak boş döndü — Mynet yedeği deneniyor")
        return fetch_kap_mynet(pencere_saat)
    log.info("KAP: %d ilgili bildirim", len(sonuc))
    return sonuc[:40]


# ------------------------------------------------------------------ 2) RSS

MAKRO_DESEN = re.compile(
    r"\b(BIST|Borsa İstanbul|TCMB|Merkez Bankası|faiz|enflasyon|TÜFE|ÜFE|dolar|euro|"
    r"cari açık|bütçe|Hazine|kredi notu|Fed|ECB)\b", re.IGNORECASE)


def _sirket_deseni():
    parcalar = []
    for kod in BIST100:
        parcalar.append(re.escape(kod))
        ad = ADLAR.get(kod)
        if ad:
            parcalar.append(re.escape(ad))
    return re.compile(r"\b(" + "|".join(parcalar) + r")\b", re.IGNORECASE)


def _rss_topla(kaynaklar, pencere_saat, filtre=None, etiket="RSS"):
    """Verilen kaynakları tarar; filtre(baslik, ozet) -> bool ise öğeyi alır."""
    try:
        import feedparser
    except ImportError:
        log.warning("feedparser kurulu değil, %s atlanıyor", etiket)
        return []

    esik = simdi() - timedelta(hours=pencere_saat)
    sonuc, gorulen = [], set()

    for kaynak in kaynaklar:
        try:
            feed = feedparser.parse(kaynak["url"], request_headers=UA)
            if not feed.entries:
                log.warning("%s: %s — hiç öğe yok (feed URL'si değişmiş olabilir)",
                            etiket, kaynak["ad"])
                continue
        except Exception as e:
            log.warning("%s hata (%s): %s", etiket, kaynak["ad"], e)
            continue

        alinan = 0
        for e in feed.entries[:60]:
            baslik = (e.get("title") or "").strip()
            if not baslik:
                continue
            anahtar = re.sub(r"\W+", "", baslik.lower())[:80]
            if anahtar in gorulen:
                continue

            zaman = None
            for alan in ("published_parsed", "updated_parsed"):
                t = e.get(alan)
                if t:
                    zaman = datetime(*t[:6], tzinfo=timezone.utc).astimezone(TRT)
                    break
            if zaman and zaman < esik:
                continue

            ozet = (e.get("summary") or "")[:400]
            if filtre and not filtre(baslik, ozet):
                continue

            link = e.get("link") or "#"
            gorulen.add(anahtar)
            sonuc.append({
                "id": f"{etiket[:3].lower()}-" + (link if link != "#" else anahtar),
                "baslik": baslik,
                "kaynak": kaynak["ad"],
                "link": link,
                "saat": zaman.strftime("%H:%M") if zaman else "",
                "tarih_iso": zaman.isoformat() if zaman else "",
                "kodlar": [],
            })
            alinan += 1
        log.info("%s: %s — %d öğe", etiket, kaynak["ad"], alinan)

    return sonuc


# Yabancı basında ilgi alanımız: piyasa/ekonomi/merkez bankası odaklı başlıklar
YABANCI_DESEN = re.compile(
    r"\b(stock|stocks|market|markets|economy|economic|inflation|fed|federal reserve|"
    r"ecb|central bank|rate|rates|yield|yields|treasury|bond|bonds|dollar|euro|"
    r"earnings|profit|revenue|recession|growth|gdp|tariff|trade|oil|energy|"
    r"nasdaq|dow|s&p|wall street|investor|investors|turkey|turkish|lira)\b",
    re.IGNORECASE)


def fetch_yabanci(pencere_saat):
    kaynaklar = CONFIG.get("yabanci_rss_kaynaklari", [])
    if not kaynaklar:
        return []
    sonuc = _rss_topla(kaynaklar, pencere_saat,
                       filtre=lambda b, o: bool(YABANCI_DESEN.search(b)),
                       etiket="Yabancı")
    log.info("Yabancı basın: %d haber", len(sonuc))
    return sonuc[:35]


# Altın: hem Türkçe hem İngilizce anahtar kelimeler.
# Türkçe ekler için (altını, altında değil ama "altın fiyatı") dikkatli sınırlar:
# "Altınordu", "altında" gibi kelimeler yakalanmasın diye ek harfleri dışlıyoruz.
ALTIN_DESEN = re.compile(
    r"(?<![a-zçğıöşü])(alt[ıi]n(?![a-zçğıöşü])|alt[ıi]n[ıi]n\b|ons\b|gram alt[ıi]n|"
    r"k[ıi]ymetli maden|de[ğg]erli maden|"
    r"gold(?![a-z])|bullion|precious metal|xau|"
    r"gold price|gold futures|central bank gold|gold demand|gold reserves)",
    re.IGNORECASE)


def fetch_altin_haber(pencere_saat):
    """Altın haberleri: hem yerli hem yabancı kaynaklar taranır."""
    kaynaklar = (CONFIG.get("rss_kaynaklari", [])
                 + CONFIG.get("yabanci_rss_kaynaklari", [])
                 + CONFIG.get("altin_rss_kaynaklari", []))
    if not kaynaklar:
        return []
    sonuc = _rss_topla(kaynaklar, pencere_saat,
                       filtre=lambda b, o: bool(ALTIN_DESEN.search(b + " " + o[:200])),
                       etiket="Altın")
    log.info("Altın haberleri: %d haber", len(sonuc))
    return sonuc[:25]


def fetch_rss(pencere_saat):
    try:
        import feedparser
    except ImportError:
        log.warning("feedparser kurulu değil, RSS atlanıyor")
        return [], []

    desen = _sirket_deseni()
    ad2kod = {v.lower(): k for k, v in ADLAR.items()}
    esik = simdi() - timedelta(hours=pencere_saat)

    sirket_haberleri, makro_haberler, gorulen = [], [], set()

    for kaynak in CONFIG["rss_kaynaklari"]:
        try:
            feed = feedparser.parse(kaynak["url"], request_headers=UA)
        except Exception as e:
            log.warning("RSS hata (%s): %s", kaynak["ad"], e)
            continue

        for e in feed.entries[:60]:
            baslik = (e.get("title") or "").strip()
            if not baslik:
                continue
            anahtar = re.sub(r"\W+", "", baslik.lower())[:80]
            if anahtar in gorulen:
                continue

            zaman = None
            for alan in ("published_parsed", "updated_parsed"):
                t = e.get(alan)
                if t:
                    zaman = datetime(*t[:6], tzinfo=timezone.utc).astimezone(TRT)
                    break
            if zaman and zaman < esik:
                continue

            eslesmeler = desen.findall(baslik + " " + (e.get("summary") or "")[:300])
            kodlar = sorted({
                (m.upper() if m.upper() in ADLAR or m.upper() in BIST100 else ad2kod.get(m.lower(), ""))
                for m in eslesmeler
            } - {""})

            link = e.get("link") or "#"
            kayit = {
                "id": "rss-" + (link if link != "#" else anahtar),
                "baslik": baslik,
                "kaynak": kaynak["ad"],
                "link": link,
                "saat": zaman.strftime("%H:%M") if zaman else "",
                "tarih_iso": zaman.isoformat() if zaman else "",
                "kodlar": kodlar,
            }
            if kodlar:
                gorulen.add(anahtar)
                sirket_haberleri.append(kayit)
            elif MAKRO_DESEN.search(baslik):
                gorulen.add(anahtar)
                makro_haberler.append(kayit)

    log.info("RSS: %d şirket, %d makro haber", len(sirket_haberleri), len(makro_haberler))
    return sirket_haberleri[:30], makro_haberler[:12]


# ---------------------------------------------------------- 3) Fiyatlar

def fetch_prices():
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance kurulu değil, fiyatlar atlanıyor")
        return {}, [], [], [], []

    semboller = [k + ".IS" for k in BIST100] + ["XU100.IS", "XU030.IS"]
    try:
        df = yf.download(semboller, period="7d", interval="1d",
                         group_by="ticker", progress=False, threads=True,
                         auto_adjust=False)
    except Exception as e:
        log.warning("Fiyat verisi alınamadı: %s", e)
        return {}, [], [], [], []

    degisim = {}
    for sym in semboller:
        try:
            kapanis = df[sym]["Close"].dropna()
            if len(kapanis) >= 2:
                son, onceki = float(kapanis.iloc[-1]), float(kapanis.iloc[-2])
                degisim[sym.replace(".IS", "")] = {
                    "fiyat": son,
                    "pct": (son / onceki - 1.0) * 100.0,
                }
        except Exception:
            continue

    endeksler = {k: degisim.get(k) for k in ("XU100", "XU030")}

    def uclar(kodlar):
        serisi = [(k, degisim[k]) for k in kodlar if k in degisim]
        serisi.sort(key=lambda x: x[1]["pct"], reverse=True)
        return serisi[:5], serisi[-5:][::-1]

    y100, d100 = uclar(BIST100)
    y30, d30 = uclar(BIST30)
    log.info("Fiyat: %d sembol", len(degisim))
    return endeksler, y100, d100, y30, d30


# ------------------------------------------------------------- 4) Altın

def fetch_gold():
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        df = yf.download(["GC=F", "TRY=X"], period="40d", interval="1d",
                         group_by="ticker", progress=False, auto_adjust=False)
        ons = df["GC=F"]["Close"].dropna()
        kur = df["TRY=X"]["Close"].dropna()
        ortak = ons.index.intersection(kur.index)
        gram = (ons.loc[ortak] / GRAM_PER_OZ * kur.loc[ortak]).dropna()
        if len(gram) < 2:
            return None

        def pct(seri, gun):
            if len(seri) <= gun:
                return None
            return (float(seri.iloc[-1]) / float(seri.iloc[-1 - gun]) - 1.0) * 100.0

        return {
            "gram_tl": float(gram.iloc[-1]),
            "ons_usd": float(ons.iloc[-1]),
            "usdtry": float(kur.iloc[-1]),
            "pct_1g": pct(gram, 1),
            "pct_7g": pct(gram, 5),    # ~1 hafta (işlem günü)
            "pct_30g": pct(gram, 21),  # ~1 ay (işlem günü)
        }
    except Exception as e:
        log.warning("Altın verisi alınamadı: %s", e)
        return None


# -------------------------------------------------------------- 5) TEFAS

def _tefas_tarih_key(v):
    """Tarih alanını sıralanabilir bir sayıya çevirir. TEFAS'ın yeni API'sinde
    biçim belgelenmemiş (epoch ms, 'dd.MM.yyyy' ya da ISO olabilir); üçünü de kabul et."""
    s = str(v or "").strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return int(m.group(3)) * 10000 + int(m.group(2)) * 100 + int(m.group(1))
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
    return 0


def fetch_tefas(kodlar):
    """TEFAS 2026'da eski DB/BindHistory* uçlarını kaldırdı; yerine fon bazlı
    fonFiyatBilgiGetir JSON API'si geldi. Periyot yalnızca 1/3/6/12/36/60 ay
    olabilir; 1G/7G/30G değişimleri için 3 ay yeterli."""
    url = "https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir"
    headers = {
        **UA,
        "Content-Type": "application/json",
        "Origin": "https://www.tefas.gov.tr",
        "Referer": "https://www.tefas.gov.tr/FonAnaliz.aspx",
        "X-Requested-With": "XMLHttpRequest",
    }

    sonuc = []
    for kod in kodlar:
        veri = []
        try:
            r = _istek("POST", url, tries=2, backoff=3, headers=headers, timeout=45,
                       json={"fonKodu": kod, "dil": "TR", "periyod": 3})
            veri = (r.json() or {}).get("resultList") or []
        except Exception as e:
            log.warning("TEFAS %s: %s", kod, e)
        if not veri:
            sonuc.append({"kod": kod, "ad": "Veri alınamadı", "fiyat": None,
                          "pct_1g": None, "pct_7g": None, "pct_30g": None})
            continue

        veri.sort(key=lambda x: _tefas_tarih_key(x.get("tarih")))
        fiyatlar = []
        for v in veri:
            try:
                f = float(str(v.get("fiyat")).replace(",", "."))
            except (TypeError, ValueError):
                continue
            if f > 0:
                fiyatlar.append(f)

        def pct(gun):
            if len(fiyatlar) <= gun:
                return None
            return (fiyatlar[-1] / fiyatlar[-1 - gun] - 1.0) * 100.0

        sonuc.append({
            "kod": kod,
            "ad": veri[-1].get("fonUnvan") or kod,
            "fiyat": fiyatlar[-1] if fiyatlar else None,
            "pct_1g": pct(1), "pct_7g": pct(5), "pct_30g": pct(21),
        })
    log.info("TEFAS: %d fon", len(sonuc))
    return sonuc


# -------------------------------------------------------------- 6) Gemini

def gemini_ozet(ctx, kapanis=False):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        log.info("GEMINI_API_KEY yok, AI özeti atlanıyor")
        return None

    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")

    kap_satirlar = "\n".join(
        f"- [{', '.join(k['kodlar'])}] ({k['kategori']}) {k['baslik']}"
        for k in ctx["kap"][:20])
    haber_satirlar = "\n".join(
        f"- [{', '.join(h['kodlar'])}] {h['baslik']}" for h in ctx["sirket_haberleri"][:15])
    makro_satirlar = "\n".join(f"- {h['baslik']}" for h in ctx["makro_haberler"][:8])

    altin = ctx.get("altin") or {}
    fonlar = "\n".join(
        f"- {f['kod']}: 1g {pct_str(f['pct_1g'])}, 1h {pct_str(f['pct_7g'])}, 1a {pct_str(f['pct_30g'])}"
        for f in ctx.get("fonlar", []))

    if kapanis:
        rol = ("Bugünün KAPANIŞ özetini yaz. Gün boyunca biriken tüm bildirim ve "
               "haberleri değerlendirip günü toparlayan, öne çıkanları vurgulayan bir kapanış "
               "değerlendirmesi yap.")
    else:
        rol = ("Şu ana kadarki gelişmelerin güncel özetini yaz. Gün boyu biriken "
               "bildirim ve haberlerden öne çıkanları anlat.")

    yabanci_satirlar = "\n".join(
        f"- ({h['kaynak']}) {h['baslik']}" for h in ctx.get("yabanci", [])[:15])
    altin_haber_satirlar = "\n".join(
        f"- ({h['kaynak']}) {h['baslik']}" for h in ctx.get("altin_haber", [])[:10])

    prompt = f"""Sen bir finans bülteni editörüsün. {rol}
Türkçe, tarafsız, abartısız yaz. Fiyat tahmini yapma, yatırım tavsiyesi verme; sadece olanı ve bağlamı anlat.
SADECE geçerli JSON döndür, başka hiçbir şey yazma. Şema:
{{"gunun_ozeti": "3-4 cümle", "yabanci_ozet": "2-3 cümle", "altin_yorumu": "2-3 cümle", "fon_notu": "1-2 cümle"}}

"yabanci_ozet" alanı: Aşağıdaki YABANCI BASIN başlıkları İngilizcedir. Bunları TÜRKÇE olarak özetle;
küresel piyasalarda öne çıkan gelişmeleri ve varsa Türkiye'ye etkisini 2-3 cümlede anlat.
"altin_yorumu" alanı: Hem altın fiyat verisini hem ALTIN HABERLERİ başlıklarını birlikte değerlendir.

KAP BİLDİRİMLERİ:
{kap_satirlar or "(yok)"}

ŞİRKET HABERLERİ:
{haber_satirlar or "(yok)"}

MAKRO HABERLER:
{makro_satirlar or "(yok)"}

YABANCI BASIN (İngilizce):
{yabanci_satirlar or "(yok)"}

ALTIN HABERLERİ:
{altin_haber_satirlar or "(yok)"}

ALTIN: gram {tr_num(altin.get('gram_tl'))} TL, ons {tr_num(altin.get('ons_usd'))} USD,
değişim 1g {pct_str(altin.get('pct_1g'))}, 7g {pct_str(altin.get('pct_7g'))}, 30g {pct_str(altin.get('pct_30g'))}

BES FONLARI:
{fonlar or "(yok)"}"""

    govde = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.4,
        },
    }
    for deneme in range(2):
        try:
            r = requests.post(url, timeout=60, json=govde)
            if r.status_code == 429:
                # Kotanın hangi limitten dolduğunu görmek için gövdeyi logla
                try:
                    detay = r.json().get("error", {}).get("message", "")[:300]
                except Exception:
                    detay = r.text[:300]
                log.warning("Gemini 429 — detay: %s", detay)
                bekle = 15 * (deneme + 1)
                log.warning("Gemini 429 (kota), %ds bekleniyor…", bekle)
                time.sleep(bekle)
                continue
            r.raise_for_status()
            metin = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            metin = re.sub(r"^```(json)?|```$", "", metin.strip(), flags=re.MULTILINE).strip()
            return json.loads(metin)
        except Exception as e:
            log.warning("Gemini özeti alınamadı: %s", e)
            return None
    log.warning("Gemini: kota (429) aşılamadı — önceki özet korunacak")
    return None


# ---------------------------------------------------------------- HTML

CSS = """
:root{
  --paper:#EFF2F6; --card:#FFFFFF; --ink:#17233A; --muted:#5B6778;
  --navy:#1F3A6E; --gold:#9C7A1D; --up:#0B8457; --down:#C13A3A;
  --line:#D9DFE8; --chip:#EAEEF4;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{background:var(--paper);color:var(--ink);
  font-family:'IBM Plex Sans',system-ui,sans-serif;font-size:15px;line-height:1.55}
a{color:inherit;text-decoration:none}
a:hover{text-decoration:underline}
a:focus-visible{outline:2px solid var(--navy);outline-offset:2px;border-radius:2px}
.wrap{max-width:1120px;margin:0 auto;padding:0 16px}

/* masthead */
header{background:var(--navy);color:#F4F7FC;padding:22px 0 18px}
.mast{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;flex-wrap:wrap}
h1{font-family:'Archivo',sans-serif;font-weight:900;font-size:clamp(28px,5vw,44px);
  letter-spacing:-.02em;line-height:1}
.mast small{display:block;margin-top:6px;color:#B9C6DE;font-size:13px}
.stamp{font-family:'IBM Plex Mono',monospace;font-size:12px;letter-spacing:.14em;
  text-transform:uppercase;border:1.5px solid #C8A94B;color:#E7CE85;
  padding:6px 12px;border-radius:3px;transform:rotate(-2deg)}

/* piyasa bandı */
.band{background:#14264A;border-top:1px solid #2C4472;overflow-x:auto;
  -webkit-overflow-scrolling:touch;scrollbar-width:none}
.band::-webkit-scrollbar{display:none}
.band-ic{display:flex;gap:0;min-width:max-content;padding:0 16px}
.tick{font-family:'IBM Plex Mono',monospace;font-size:13px;color:#DCE5F4;
  padding:10px 18px;border-right:1px solid #2C4472;white-space:nowrap}
.tick b{font-weight:600;color:#8FA5CC;margin-right:8px}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--muted)}
.band .up{color:#4FD79F} .band .down{color:#F08A8A}

/* düzen */
main{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:20px;padding:24px 0 8px}
@media(max-width:840px){main{grid-template-columns:1fr}}
section,.kart{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:18px;margin-bottom:20px}
h2{font-family:'Archivo',sans-serif;font-weight:800;font-size:17px;letter-spacing:.01em;
  padding-bottom:10px;border-bottom:2px solid var(--ink);margin-bottom:12px;
  display:flex;align-items:center;gap:8px}
h2 .say{font-family:'IBM Plex Mono',monospace;font-weight:500;font-size:12px;
  color:var(--muted);margin-left:auto}

/* haber listeleri */
.ml{list-style:none}
.ml li{padding:10px 0;border-bottom:1px dashed var(--line)}
.ml li:last-child{border-bottom:none}
.ml .b{font-weight:600;font-size:14.5px}
.meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:5px;align-items:center}
.meta .kay{font-size:12px;color:var(--muted)}
.chip{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;
  background:var(--chip);color:var(--navy);padding:2px 7px;border-radius:3px}
.chip.kat{background:#F3EDDC;color:var(--gold)}

/* özet */
.ozet{border-left:4px solid var(--gold)}
.ozet p{font-size:15px}

/* tablolar */
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:6px 4px;text-align:left;border-bottom:1px solid var(--line)}
th{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:500;
  color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
td.num,th.num{text-align:right;font-family:'IBM Plex Mono',monospace}
tr:last-child td{border-bottom:none}
.altlik{font-size:11.5px;color:var(--muted);margin-top:10px}

/* altın kartı */
.buyuk{font-family:'IBM Plex Mono',monospace;font-size:26px;font-weight:600}
.buyuk small{font-size:13px;color:var(--muted);font-weight:400}
.gridd{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}
.gridd div{background:var(--chip);border-radius:6px;padding:8px;text-align:center}
.gridd b{display:block;font-family:'IBM Plex Mono',monospace;font-size:14px}
.gridd span{font-size:11px;color:var(--muted)}
.yorum{margin-top:12px;font-size:13.5px;color:#33415C;border-top:1px dashed var(--line);padding-top:10px}

/* yabancı basın & altın haberleri — görsel ayrım */
.yabanci{border-left:4px solid var(--navy)}
.yabanci h2::before{content:"◷ ";color:var(--navy);font-weight:400}
.altin-h{border-left:4px solid var(--gold)}
.altin-h h2::before{content:"◆ ";color:var(--gold);font-weight:400}

.ornek-serit{background:#8A1C1C;color:#fff;text-align:center;padding:8px 12px;
  font-family:'IBM Plex Mono',monospace;font-size:12.5px;letter-spacing:.08em;
  text-transform:uppercase;font-weight:600}
footer{padding:16px 0 40px;color:var(--muted);font-size:12.5px;line-height:1.7}
.bos{color:var(--muted);font-size:13.5px;padding:6px 0}
"""

HEAD = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{baslik} — {tarih}</title>
<meta name="description" content="{aciklama}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@700;800;900&family=IBM+Plex+Sans:wght@400;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
"""


def _haber_li(h, kategori_chip=False):
    chips = "".join(f'<span class="chip">{esc(k)}</span>' for k in h.get("kodlar", []))
    kat = f'<span class="chip kat">{esc(h["kategori"])}</span>' if kategori_chip and h.get("kategori") else ""
    saat_metni = h.get("saat_g") or h.get("saat") or ""
    saat = f'<span class="kay">{esc(saat_metni)}</span>' if saat_metni else ""
    kay = f'<span class="kay">{esc(h["kaynak"])}</span>' if h.get("kaynak") else ""
    return (f'<li><a class="b" href="{esc(h["link"])}" target="_blank" rel="noopener">'
            f'{esc(h["baslik"])}</a>'
            f'<div class="meta">{kat}{chips}{kay}{saat}</div></li>')


def _mover_tablo(baslik, yukselen, dusen):
    def satirlar(liste):
        out = []
        for kod, v in liste:
            out.append(f'<tr><td><span class="chip">{esc(kod)}</span></td>'
                       f'<td class="num">{tr_num(v["fiyat"])}</td>'
                       f'<td class="num {pct_class(v["pct"])}">{pct_str(v["pct"])}</td></tr>')
        return "".join(out)

    if not yukselen and not dusen:
        return f'<div class="kart"><h2>{baslik}</h2><p class="bos">Fiyat verisi alınamadı.</p></div>'
    return f"""<div class="kart"><h2>{baslik}</h2>
<table><thead><tr><th>Yükselen</th><th class="num">Fiyat</th><th class="num">Değişim</th></tr></thead>
<tbody>{satirlar(yukselen)}</tbody></table>
<table style="margin-top:12px"><thead><tr><th>Düşen</th><th class="num">Fiyat</th><th class="num">Değişim</th></tr></thead>
<tbody>{satirlar(dusen)}</tbody></table></div>"""


def render_html(ctx):
    now = ctx["zaman"]
    tarih = f"{now.day} {AYLAR[now.month-1]} {now.year}, {GUNLER[now.weekday()]}"

    p = [HEAD.format(baslik=esc(CONFIG["site"]["baslik"]), tarih=esc(tarih),
                     aciklama=esc(CONFIG["site"]["aciklama"]), css=CSS)]

    if ctx.get("ornek"):
        p.append('<div class="ornek-serit">⚠ Bu sayfa örnek (demo) veriyle üretildi — '
                 'canlı değerler değildir. Gerçek bülten ilk çalıştırmada bu sayfanın yerini alır.</div>')

    # masthead
    biriken_not = ""
    if ctx.get("biriken"):
        biriken_not = " · Haberler gün boyu birikir, her sabah sıfırlanır"
    p.append(f"""<header><div class="wrap mast">
<div><h1>{esc(CONFIG["site"]["baslik"])}</h1>
<small>{esc(tarih)} · Son güncelleme {now.strftime("%H:%M")} (TSİ){esc(biriken_not)}</small></div>
<div class="stamp">{esc(ctx["baski"])}</div>
</div></header>""")

    # piyasa bandı
    bant = []
    for kod, etiket in (("XU100", "BIST 100"), ("XU030", "BIST 30")):
        v = (ctx.get("endeksler") or {}).get(kod)
        if v:
            bant.append(f'<div class="tick"><b>{etiket}</b>{tr_num(v["fiyat"])} '
                        f'<span class="{pct_class(v["pct"])}">{pct_str(v["pct"])}</span></div>')
    a = ctx.get("altin")
    if a:
        bant.append(f'<div class="tick"><b>GRAM ALTIN</b>{tr_num(a["gram_tl"])} ₺ '
                    f'<span class="{pct_class(a["pct_1g"])}">{pct_str(a["pct_1g"])}</span></div>')
        bant.append(f'<div class="tick"><b>USD/TRY</b>{tr_num(a["usdtry"], 4)}</div>')
        bant.append(f'<div class="tick"><b>ONS</b>{tr_num(a["ons_usd"])} $</div>')
    if bant:
        p.append(f'<div class="band"><div class="wrap band-ic">{"".join(bant)}</div></div>')

    p.append('<div class="wrap"><main><div>')  # ana kolon başı

    # günün özeti / kapanış özeti
    ai = ctx.get("ai") or {}
    if ai.get("gunun_ozeti"):
        ozet_baslik = "Kapanış Özeti" if ctx.get("kapanis") else "Günün Özeti"
        p.append(f'<section class="ozet"><h2>{esc(ozet_baslik)}</h2><p>{esc(ai["gunun_ozeti"])}</p></section>')

    # KAP
    kap = ctx.get("kap", [])
    p.append(f'<section><h2>KAP Bildirimleri <span class="say">{len(kap)}</span></h2>')
    if kap:
        p.append('<ul class="ml">' + "".join(_haber_li(k, kategori_chip=True) for k in kap) + "</ul>")
    else:
        p.append('<p class="bos">Bu pencerede BIST 100 şirketlerinden ilgili bildirim bulunamadı '
                 '(veya KAP kaynağına ulaşılamadı).</p>')
    p.append("</section>")

    # şirket haberleri
    sh = ctx.get("sirket_haberleri", [])
    p.append(f'<section><h2>Şirket Haberleri <span class="say">{len(sh)}</span></h2>')
    p.append('<ul class="ml">' + "".join(_haber_li(h) for h in sh) + "</ul>" if sh
             else '<p class="bos">Kaynaklarda BIST 100 şirketleriyle eşleşen yeni haber yok.</p>')
    p.append("</section>")

    # makro
    mk = ctx.get("makro_haberler", [])
    if mk:
        p.append(f'<section><h2>Makro &amp; Piyasa <span class="say">{len(mk)}</span></h2>'
                 '<ul class="ml">' + "".join(_haber_li(h) for h in mk) + "</ul></section>")

    # yabancı basın
    yb = ctx.get("yabanci", [])
    p.append(f'<section class="yabanci"><h2>Yabancı Basın <span class="say">{len(yb)}</span></h2>')
    if ai.get("yabanci_ozet"):
        p.append(f'<p class="yorum" style="margin:0 0 10px;border-top:none;padding-top:0">'
                 f'{esc(ai["yabanci_ozet"])}</p>')
    if yb:
        p.append('<ul class="ml">' + "".join(_haber_li(h) for h in yb) + "</ul>")
    else:
        p.append('<p class="bos">Bu pencerede yabancı kaynaklardan haber alınamadı.</p>')
    p.append("</section>")

    # altın haberleri
    ah = ctx.get("altin_haber", [])
    p.append(f'<section class="altin-h"><h2>Altın Haberleri <span class="say">{len(ah)}</span></h2>')
    if ah:
        p.append('<ul class="ml">' + "".join(_haber_li(h) for h in ah) + "</ul>")
    else:
        p.append('<p class="bos">Bu pencerede altınla ilgili haber bulunamadı.</p>')
    p.append("</section>")

    p.append("</div><aside>")  # yan kolon

    # movers
    p.append(_mover_tablo("BIST 100 — Günün Uçları", ctx.get("y100", []), ctx.get("d100", [])))
    p.append(_mover_tablo("BIST 30 — Günün Uçları", ctx.get("y30", []), ctx.get("d30", [])))

    # altın
    p.append('<div class="kart"><h2>Altın</h2>')
    if a:
        p.append(f'<div class="buyuk">{tr_num(a["gram_tl"])} ₺ <small>/ gram</small></div>'
                 f'<div class="gridd">'
                 f'<div><b class="{pct_class(a["pct_1g"])}">{pct_str(a["pct_1g"])}</b><span>1 gün</span></div>'
                 f'<div><b class="{pct_class(a["pct_7g"])}">{pct_str(a["pct_7g"])}</b><span>1 hafta</span></div>'
                 f'<div><b class="{pct_class(a["pct_30g"])}">{pct_str(a["pct_30g"])}</b><span>1 ay</span></div>'
                 f'</div>'
                 f'<p class="altlik">Ons: {tr_num(a["ons_usd"])} $ · USD/TRY: {tr_num(a["usdtry"], 4)} · '
                 f'gram = ons ÷ 31,10 × kur</p>')
        if ai.get("altin_yorumu"):
            p.append(f'<p class="yorum">{esc(ai["altin_yorumu"])}</p>')
    else:
        p.append('<p class="bos">Altın verisi alınamadı.</p>')
    p.append("</div>")

    # BES fonları
    fonlar = ctx.get("fonlar", [])
    p.append('<div class="kart"><h2>BES Fonlarım</h2>')
    if fonlar:
        satirlar = "".join(
            f'<tr><td><span class="chip">{esc(f["kod"])}</span></td>'
            f'<td class="num">{tr_num(f["fiyat"], 6) if f["fiyat"] else "—"}</td>'
            f'<td class="num {pct_class(f["pct_1g"])}">{pct_str(f["pct_1g"])}</td>'
            f'<td class="num {pct_class(f["pct_30g"])}">{pct_str(f["pct_30g"])}</td></tr>'
            for f in fonlar)
        adlar = " · ".join(f'{esc(f["kod"])}: {esc(f["ad"])}' for f in fonlar if f.get("ad"))
        p.append(f'<table><thead><tr><th>Fon</th><th class="num">Fiyat</th>'
                 f'<th class="num">1G</th><th class="num">1A</th></tr></thead>'
                 f'<tbody>{satirlar}</tbody></table>'
                 f'<p class="altlik">{adlar}</p>')
        if ai.get("fon_notu"):
            p.append(f'<p class="yorum">{esc(ai["fon_notu"])}</p>')
    else:
        p.append('<p class="bos">Fon verisi alınamadı.</p>')
    p.append("</div>")

    p.append("</aside></main>")

    p.append(f"""<footer>
Kaynaklar: KAP, TEFAS, Yahoo Finance ve RSS haber akışları. Bu sayfa otomatik derlenir;
veriler gecikmeli veya eksik olabilir. İçerik bilgilendirme amaçlıdır, <b>yatırım tavsiyesi değildir</b>.
<br>Borsa Günlüğü · GitHub Actions ile gün boyu güncellenir. · <a href="arsiv/"><b>Geçmiş bültenler →</b></a>
</footer></div></body></html>""")

    return "".join(p)


# ----------------------------------------------------------------- arşiv

def arsiv_guncelle(html, bgun):
    """Bülten gününün kopyasını docs/arsiv/ altına yazar ve dizin sayfasını
    yeniden kurar. Her tur üzerine yazdığı için dosya, günün en son yayımlanan
    hâlini taşır — kapanış turu kaçsa bile arşiv boş kalmaz."""
    ad = ROOT / "docs" / "arsiv"
    ad.mkdir(parents=True, exist_ok=True)

    # kopyada footer'daki arşiv bağlantısı kendi dizinini göstersin
    (ad / f"{bgun}.html").write_text(
        html.replace('href="arsiv/"', 'href="./"'), encoding="utf-8")

    satirlar = []
    for f in sorted(ad.glob("*.html"), reverse=True):
        try:
            d = datetime.strptime(f.stem, "%Y-%m-%d")
        except ValueError:
            continue  # index.html vb.
        etiket = f"{d.day} {AYLAR[d.month-1]} {d.year}, {GUNLER[d.weekday()]}"
        satirlar.append(f'<li><a class="b" href="{f.name}">{esc(etiket)}</a></li>')

    p = [HEAD.format(baslik=esc(CONFIG["site"]["baslik"]), tarih="Arşiv",
                     aciklama="Geçmiş bültenler", css=CSS)]
    p.append(f"""<header><div class="wrap mast">
<div><h1>{esc(CONFIG["site"]["baslik"])}</h1>
<small>Geçmiş bültenler · her gün, günün son yayımlanan hâliyle saklanır</small></div>
<div class="stamp">Arşiv</div>
</div></header>
<div class="wrap"><main style="grid-template-columns:1fr">
<section><h2>Geçmiş Bültenler <span class="say">{len(satirlar)}</span></h2>
<ul class="ml">{"".join(satirlar)}</ul>
<p class="altlik"><a href="../"><b>← Bugünün bültenine dön</b></a></p>
</section></main></div></body></html>""")
    (ad / "index.html").write_text("".join(p), encoding="utf-8")
    log.info("Arşiv güncellendi: docs/arsiv/%s.html (%d gün)", bgun, len(satirlar))


# ----------------------------------------------------------------- mock

def mock_ctx():
    now = simdi()
    return {
        "zaman": now,
        "ornek": True,
        "baski": "Sabah Baskısı" if now.hour < 13 else "Akşam Baskısı",
        "kap": [
            {"baslik": "Yeni İş İlişkisi — Yurt dışı demiryolu elektrifikasyon projesi sözleşmesi imzalanması hk.",
             "sirket": "Örnek A.Ş.", "kodlar": ["ASELS"], "kategori": "Yeni İş / Sözleşme",
             "saat": "09:12", "link": "https://www.kap.org.tr"},
            {"baslik": "Borçlanma Aracı İhracı — 1,5 milyar TL tahvil ihracının tamamlanması",
             "sirket": "Örnek Banka", "kodlar": ["AKBNK"], "kategori": "Borçlanma",
             "saat": "08:47", "link": "https://www.kap.org.tr"},
            {"baslik": "Yatırım Kararı — Yeni üretim hattı kapasite artışı yatırımı",
             "sirket": "Örnek Sanayi", "kodlar": ["EREGL"], "kategori": "Yatırım",
             "saat": "08:30", "link": "https://www.kap.org.tr"},
        ],
        "sirket_haberleri": [
            {"baslik": "THYAO ilk yarı yolcu sayısını açıkladı", "kaynak": "Bloomberg HT",
             "link": "#", "saat": "07:58", "kodlar": ["THYAO"]},
            {"baslik": "Tüpraş'tan rafineri bakım takvimi güncellemesi", "kaynak": "Dünya",
             "link": "#", "saat": "07:31", "kodlar": ["TUPRS"]},
        ],
        "makro_haberler": [
            {"baslik": "TCMB haftalık rezerv verileri açıklandı", "kaynak": "AA Ekonomi",
             "link": "#", "saat": "07:00", "kodlar": []},
        ],
        "yabanci": [
            {"baslik": "Fed officials signal caution on further rate cuts",
             "kaynak": "CNBC", "link": "#", "saat": "21:40", "kodlar": []},
            {"baslik": "Dollar steadies as investors weigh inflation data",
             "kaynak": "MarketWatch", "link": "#", "saat": "20:15", "kodlar": []},
            {"baslik": "European stocks close higher on earnings optimism",
             "kaynak": "BBC Business", "link": "#", "saat": "18:50", "kodlar": []},
        ],
        "altin_haber": [
            {"baslik": "Gold holds near record as central banks keep buying",
             "kaynak": "Kitco", "link": "#", "saat": "19:05", "kodlar": []},
            {"baslik": "Gram altın yeni zirvesini test etti",
             "kaynak": "Bloomberg HT", "link": "#", "saat": "16:20", "kodlar": []},
        ],
        "endeksler": {"XU100": {"fiyat": 11234.5, "pct": 0.84},
                      "XU030": {"fiyat": 12345.6, "pct": 0.61}},
        "y100": [("KONTR", {"fiyat": 45.2, "pct": 6.1}), ("SASA", {"fiyat": 4.1, "pct": 4.8}),
                 ("ASTOR", {"fiyat": 98.4, "pct": 4.2}), ("MIATK", {"fiyat": 30.1, "pct": 3.9}),
                 ("GESAN", {"fiyat": 12.7, "pct": 3.4})],
        "d100": [("HEKTS", {"fiyat": 2.1, "pct": -3.8}), ("ODAS", {"fiyat": 5.6, "pct": -2.9}),
                 ("KRDMD", {"fiyat": 22.3, "pct": -2.2}), ("ZOREN", {"fiyat": 3.4, "pct": -1.9}),
                 ("PETKM", {"fiyat": 18.9, "pct": -1.5})],
        "y30": [("KONTR", {"fiyat": 45.2, "pct": 6.1}), ("SASA", {"fiyat": 4.1, "pct": 4.8}),
                ("ASTOR", {"fiyat": 98.4, "pct": 4.2}), ("THYAO", {"fiyat": 310.0, "pct": 2.8}),
                ("TUPRS", {"fiyat": 178.2, "pct": 2.1})],
        "d30": [("HEKTS", {"fiyat": 2.1, "pct": -3.8}), ("ODAS", {"fiyat": 5.6, "pct": -2.9}),
                ("KRDMD", {"fiyat": 22.3, "pct": -2.2}), ("PETKM", {"fiyat": 18.9, "pct": -1.5}),
                ("EKGYO", {"fiyat": 12.4, "pct": -1.1})],
        "altin": {"gram_tl": 4123.45, "ons_usd": 3350.20, "usdtry": 38.2915,
                  "pct_1g": 0.6, "pct_7g": 1.9, "pct_30g": 4.7},
        "fonlar": [
            {"kod": "GEL", "ad": "Örnek Emeklilik Altın Katılım Fonu", "fiyat": 0.123456,
             "pct_1g": 0.5, "pct_7g": 1.7, "pct_30g": 4.1},
            {"kod": "GHA", "ad": "Örnek Emeklilik Hisse Senedi Fonu", "fiyat": 0.654321,
             "pct_1g": 0.9, "pct_7g": -0.4, "pct_30g": 3.2},
        ],
        "ai": {
            "gunun_ozeti": "Örnek özet: BIST 100 günü alıcılı geçirirken savunma ve enerji "
                           "tarafında yeni sözleşme bildirimleri öne çıktı. (Bu, --mock modunda "
                           "üretilmiş örnek metindir.)",
            "yabanci_ozet": "Örnek yabancı basın özeti: Fed yetkilileri faiz indirimlerinde "
                            "temkinli bir dil kullanırken, dolar enflasyon verisi öncesi yatay seyretti.",
            "altin_yorumu": "Örnek yorum: Gram altın kur etkisiyle haftalık bazda yükselişini korudu; "
                            "merkez bankası alımları ons tarafında desteği sürdürüyor.",
            "fon_notu": "Örnek not: Altın fonu ayı artıda geçiriyor.",
        },
    }


# ----------------------------------------------------------------- main

def main():
    mock = "--mock" in sys.argv
    now = simdi()
    bugun = now.strftime("%Y-%m-%d")      # takvim günü (haberlerin "dün" etiketi için)
    bgun = bulten_gunu(now)               # bülten günü (08:45'te başlar; feed bu tarihe göre sıfırlanır)

    if mock:
        ctx = mock_ctx()
        out = ROOT / "docs" / "index.html"
        out.parent.mkdir(exist_ok=True)
        out.write_text(render_html(ctx), encoding="utf-8")
        log.info("Yazıldı (mock): %s", out)
        return

    # --- HER ZAMAN GÜNCEL: fiyat, altın, fon (bunlar birikmez)
    endeksler, y100, d100, y30, d30 = fetch_prices()
    altin = fetch_gold()
    fonlar = fetch_tefas(CONFIG.get("bes_fonlari", []))

    # --- BİRİKEN FEED: haberler ve KAP bildirimleri
    state = feed_yukle()
    yeni_gun = (state is None) or (state.get("tarih") != bgun)

    # Akşam turu (19:00+): BIST kapalı, ABD piyasası açık.
    # Bu turlarda sadece yabancı basın + altın haberleri toplanır.
    aksam_turu = now.hour >= 19

    if yeni_gun:
        # Bülten gününün ilk turu (08:45 sonrası): feed'i sıfırla, geniş pencere
        pencere = CONFIG.get("sabah_penceresi_saat", 14)
        # Yabancı basın için gece boyu (ABD kapanışı) haberlerini de yakala
        yabanci_pencere = CONFIG.get("yabanci_sabah_penceresi_saat", 16)
        state = {"tarih": bgun, "kap": [], "sirket": [], "makro": [],
                 "yabanci": [], "altin_haber": []}
        log.info("YENİ BÜLTEN GÜNÜ — feed sıfırlandı (%s), pencere BIST:%dh yabancı:%dh",
                 bgun, pencere, yabanci_pencere)
    else:
        pencere = CONFIG.get("gunici_penceresi_saat", 6)
        yabanci_pencere = pencere
        log.info("%s tur — feed'e ekleniyor, pencere %dh",
                 "Akşam" if aksam_turu else "Gün içi", pencere)

    # eski feed'lerde bu anahtarlar olmayabilir
    state.setdefault("yabanci", [])
    state.setdefault("altin_haber", [])

    now_iso = now.isoformat()

    # Yabancı basın + altın: her turda (akşam turları dahil)
    yabanci_yeni = fetch_yabanci(yabanci_pencere)
    altin_haber_yeni = fetch_altin_haber(yabanci_pencere)
    state["yabanci"], ek_yab = feed_birlestir(state["yabanci"], yabanci_yeni, now_iso)
    state["altin_haber"], ek_alt = feed_birlestir(state["altin_haber"], altin_haber_yeni, now_iso)

    # KAP + yerli haberler: akşam turlarında atlanır (BIST kapalı, yeni bildirim gelmez)
    ek_kap = ek_sir = ek_mak = 0
    if not aksam_turu:
        kap_yeni = fetch_kap(pencere)
        sirket_yeni, makro_yeni = fetch_rss(pencere)
        state["kap"], ek_kap = feed_birlestir(state.get("kap", []), kap_yeni, now_iso)
        state["sirket"], ek_sir = feed_birlestir(state.get("sirket", []), sirket_yeni, now_iso)
        state["makro"], ek_mak = feed_birlestir(state.get("makro", []), makro_yeni, now_iso)
    else:
        log.info("Akşam turu — BIST kapalı, KAP ve yerli haber taraması atlandı")

    # sınırsız büyümeyi önle
    state["kap"] = feed_sirala(state["kap"])[:200]
    state["sirket"] = feed_sirala(state["sirket"])[:200]
    state["makro"] = feed_sirala(state["makro"])[:80]
    state["yabanci"] = feed_sirala(state["yabanci"])[:120]
    state["altin_haber"] = feed_sirala(state["altin_haber"])[:80]
    state["guncelleme"] = now_iso
    feed_kaydet(state)
    log.info("Feed'e eklenen — KAP:%d haber:%d makro:%d yabancı:%d altın:%d "
             "| toplam KAP:%d haber:%d yabancı:%d altın:%d",
             ek_kap, ek_sir, ek_mak, ek_yab, ek_alt,
             len(state["kap"]), len(state["sirket"]),
             len(state["yabanci"]), len(state["altin_haber"]))

    kapanis = (18 <= now.hour < 19)  # 18:00–18:30 turları: kapanış özeti

    # --- Gemini kota yönetimi ---
    # Ücretsiz katmanın günlük sınırını korumak için AI özetini yalnızca gerçekten
    # gerektiğinde çağırıyoruz: (a) yeni gün başlarken, (b) kapanış turunda,
    # (c) feed'e kayda değer yeni içerik eklendiğinde. Aksi halde önceki özeti koruyoruz.
    yeni_icerik = (ek_kap + ek_sir + ek_yab + ek_alt)
    ai_gerekli = yeni_gun or kapanis or yeni_icerik >= CONFIG.get("ai_min_yeni_haber", 3)
    onceki_ai = state.get("ai")

    # önceki günden gelen öğelere "dün" damgası
    for lst in (state["kap"], state["sirket"], state["makro"],
                state["yabanci"], state["altin_haber"]):
        for it in lst:
            ti = it.get("tarih_iso", "")
            it["saat_g"] = ("dün " + it["saat"]) if (ti[:10] and ti[:10] != bugun
                                                      and it.get("saat")) else it.get("saat", "")

    ctx = {
        "zaman": now,
        "biriken": True,
        "kapanis": kapanis,
        "aksam_turu": aksam_turu,
        "baski": ("Akşam — Yabancı Basın" if aksam_turu else
                  "Kapanış Özeti" if kapanis else
                  "Sabah Baskısı" if yeni_gun else "Gün İçi"),
        "kap": state["kap"],
        "sirket_haberleri": state["sirket"],
        "makro_haberler": state["makro"],
        "yabanci": state["yabanci"],
        "altin_haber": state["altin_haber"],
        "endeksler": endeksler,
        "y100": y100, "d100": d100, "y30": y30, "d30": d30,
        "altin": altin,
        "fonlar": fonlar,
    }
    if ai_gerekli:
        yeni_ai = gemini_ozet(ctx, kapanis=kapanis)
        if yeni_ai:
            ctx["ai"] = yeni_ai
            state["ai"] = yeni_ai
            feed_kaydet(state)   # özeti kalıcı sakla
        else:
            # Gemini başarısız: önceki özeti koru, sayfa özetsiz kalmasın
            ctx["ai"] = onceki_ai
            if onceki_ai:
                log.info("Gemini başarısız — önceki özet korunuyor")
    else:
        ctx["ai"] = onceki_ai
        log.info("AI çağrısı atlandı (yeni içerik: %d) — kota korunuyor", yeni_icerik)

    out = ROOT / "docs" / "index.html"
    out.parent.mkdir(exist_ok=True)
    html = render_html(ctx)
    out.write_text(html, encoding="utf-8")
    log.info("Yazıldı: %s", out)

    arsiv_guncelle(html, bgun)

    # kapanış turunda o günün kalıcı arşivini de bırak
    if kapanis:
        arsiv = ROOT / "data"
        arsiv.mkdir(exist_ok=True)
        (arsiv / f"{bugun}-kapanis.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("Arşiv: data/%s-kapanis.json", bugun)


if __name__ == "__main__":
    main()
