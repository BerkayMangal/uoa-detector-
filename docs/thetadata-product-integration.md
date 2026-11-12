# ThetaData → UOA: kullanım ve gerçek durum

12 Kasım 2026. Kullanıcının doğruladığı abonelik Options Pro.
Çalışan Terminal kullanıcının Mac’inde.
Bu cloud oturumunda ThetaData anahtarı veya Mac Terminal bağlantısı bulunmuyor.

## Eklenen ürün işlevi

Phase 5.70 ile `/opsiyon-dogrulama`, ana menüdeki **Otomatik takip** ekranıdır.
Mevcut giriş kararının uygun tek kontratlı opsiyon planı otomatik kaydedilir.
Mac bağlantı programı Terminal’den giriş ve alternatif +15/+60 dakika
kotasyonlarını alır. UOA ham paketleri doğrular, maliyetleri kendisi hesaplar
ve değiştirilmeyen sonuç kaydı oluşturur. Ekran durumunu kendiliğinden yeniler.
Bu belge kod davranışını anlatır; canlı sürüm ve gerçek veri doğrulaması
ilgili aşamanın kapanış raporunda ayrıca belirtilir.

Kaynak günün/hissenin ilk kaydedilmiş AL planıdır. Hisse veya spread planı,
eksik sözleşme ve geçersiz kaynak ayrı gerekçeyle kapsam dışında kalır.
Sonradan daha iyi sonuç veren kontrat seçilmez. Hiç uygun giriş yoksa ekran
bunu söyler; taramanın çalışması mutlaka yeni giriş oluşacağı anlamına gelmez.

Bir standart kontrat varsayılır. Alış ask + $0,01/hisse; satış bid − $0,01/hisse;
komisyon taraf başına $0,65. Girişten sonraki 120 saniyede uygun kotasyon
aranır; çıkış hedefi bulunan giriş kotasyonundan hesaplanır. Eksik fiyat sıfır
getiri değildir. Sonradan indirilen kotasyonlar gerçekleşmiş broker işlemi veya
o gün uygulamanın bildiği veri sayılmaz. Ayarlanmış kontrat teslimatı ayrıca
doğrulanmadığından 100 hisse çarpanı bir varsayımdır.

## Mac bağlantısı

Phase 5.70 sürekli bağlantısı:

1. ThetaData Terminal açık ve hesabına bağlı kalsın.
2. Sana verilen tek kullanımlık bağlantıyı aç veya açık UOA oturumunda
   **Otomatik takip → Mac’i bir kez bağla** seçeneğini kullan.
3. Bağlantı sayfasındaki indirme düğmesine bas. Yeni dosya benzersiz bir ad
   alır; böylece daha önce indirilmiş, şifre isteyen dosya ile karışmaz.
4. İndirme tamamlanınca sayfada görünen **o dosyaya ait komutu** Mac Terminal’e
   yapıştır. `--install` kullanıcı oturumunda otomatik çalışan yardımcıyı
   kurar. Kullanıcı adı, UOA şifresi ve ThetaData anahtarı sorulmaz. Sağlayıcı
   girişini zaten açık olan Theta Terminal yönetir.

İş olmadan bağlantı kurulabilir. Sonradan eklenen incelemeler ve otomatik
kayıtlar aynı bağlantıyla alınır; tekrar dosya indirmek gerekmez. Bağlantı
koptuğunda program sınırlı aralıklarla yeniden dener. Terminal kapalıyken
işlerin deneme hakkını tüketmez. Mac açık ve kullanıcı oturumu çalışır durumda
olmalıdır; uyku sırasında aktarım durur, yeniden çalışınca devam eder.

Python 3.9+ yeterlidir; ek Python paketi gerekmez. Varsayılan Terminal
`http://127.0.0.1:25503`, site mevcut UOA adresidir. Program dışarıya HTTPS
bağlantısı açar; Mac’in Terminal portunu internete açmaz, üretim veritabanı
parolası istemez. Python kurulu değilse bu bağımlılık hâlâ bir kurulum adımıdır;
imzalı bağımsız Mac uygulaması bu sürümün parçası değildir.

İndirme bağlantısı 24 saat içinde bir kez kullanılabilir. Sürekli cihaz yetkisi
ekrandan iptal edilene kadar mevcut ve gelecek doğrulama işlerini kapsar;
raporları, kullanıcı yönetimini veya genel site erişimini kapsamaz. Sunucu
sadece yetki özetlerini saklar. Mac’te yetki içeren dosyalar kullanıcıya özel
izinlerle saklanır. Genel site şifresi dosyada bulunmaz; UOA adresi sabittir.

Bir çalışmada en fazla dört iş, iş başına üç adet 120 saniyelik pencere;
istek başına 20 saniye ve 8 MiB sınırı vardır. İlk cihaz bildirimi yalnızca
yardımcı programın siteye ulaştığını gösterir. Terminal portunun açık olması
sağlayıcı girişini veya fiyat erişimini kanıtlamaz; gerçek kotasyon başarısı
ayrı gösterilir. Gönderimi belirsiz kalan paketin aynı içeriği yeniden
gönderilir; yeni sonuç uydurulmaz.

Eski Phase 5.68.2 bağlantıları değişmez: iki saatlik indirme, yedi günlük
dosya yetkisi ve yalnızca başlangıçtaki en fazla 32 olgunlaşmış iş. Bu sınırlı
akış geçmiş inceleme bölümünde kalır. Sürekli izleme için yeni cihaz kurulur.

## Kayıtlı veriden tekrar ölçüm

Günü seç; **Kayıtlı veriden yeniden hesapla** ile kaydedilmiş paketlerden aynı
sabit maliyetli ölçümü üret. **Tam gün kaydını indir** bekleyen, eksik ve
kapsam dışı kayıtları da kaynak kimlikleriyle verir. Raporun kapsam ve yöntem
özetleri aynı veriyle tekrarı denetlemeyi sağlar. Bu işlem yeni fiyat indiren
veya kazanan strateji arayan sınırsız bir backtest değildir. Yeni hipotezler
ayrı protokolle ve bütün denemeler kaydedilerek sınanır.

Üç başarısız veri denemesinden sonra açık yeniden deneme işlemi asıl sözleşme
ve saati koruyan bağlı bir istek açar. Eski denemeler raporda kalır; en fazla
sekiz böyle tekrar mümkündür. Kesinleşmiş bir sonuç, eksik fiyat nedeniyle
UNKNOWN olsa bile, daha iyi sonuç seçmek için yeniden açılmaz.

Yeni kayıtlar ilk kalıcı gözlemden sonraki sabit giriş gecikmesini kullanır.
Geçmişten alınan sınırlı kayıtlar geriye dönük olarak işaretlenir; tarihsel
yayın saati, verinin o anda fiziksel olarak elde olduğunu kanıtlamaz.

İşletim notu: mevcut canlı servis yapılandırması varsa otomatik gözlemci
varsayılan olarak çalışır; `THETA_AUTOMATIC_ENABLED=false` yeni gözlemi durdurur.
Bu ayar geçmiş kayıtları veya ölçüm sonuçlarını silmez. Bağlantı iptali ayrı bir
işlemdir. Dışa aktarımın `include_packets=true` seçeneği, boyut sınırı içindeki
ham kotasyon paketlerini de verir; normal indirme bütün kapsamın kimliklerini,
durumlarını ve kaynak özetlerini içerir.

## Abonelikten çıkarılabilecek işler

İncelenen v3 şema kopyasında 34 opsiyon işlemi, minimum Professional olarak
etiketlenmiş 11 işlem var. Bu **doküman envanteridir**; hesabın güncel erişim,
geçmiş derinliği ve limitleri henüz gerçek ThetaData isteğiyle doğrulanmadı.
Resmî dokümana bu cloud ağından erişim engellendi; üçüncü taraf şema kopyası
[kaynak commit](https://github.com/ashler-herrick/py-theta-client/blob/bd03b1449d9013f94fcb0a3c2bb17a5b2f342c50/openapiv3.yaml).

| Veri | Mevcut UOA’da somut kullanım |
|---|---|
| Tick NBBO kotasyon geçmişi | İşlem geçmeyen anlarda giriş/çıkış fiyatı; mevcut UW ölçüm boşluklarını sınamak |
| İşlem + eşlenmiş kotasyon | Akışın gerçek spreadi ve alış/satış tarafı; büyük primin tek başına yanıltıcılığını ölçmek |
| Tarihsel IV ve birinci derece Greeks | Yön doğruyken opsiyonun neden zarar ettiğini delta, theta ve IV ile açıklamak |
| İkinci/üçüncü derece ve işlem bazlı Greeks | Yalnızca temel IV/delta modeline ek ölçülebilir katkı sağlıyorsa kullanmak |
| EOD, açık pozisyon, vade ve strike listeleri | Zincir kapsamını doğrulamak; ertesi gün OI değişimiyle açılış hipotezini sınamak |
| Snapshot / belirli an kotasyonu | Güncel maliyet kontrolü; önceki kotasyonun yaşı bilinmeden onu işlem fiyatı saymamak |

Stock/index ve şirket aksiyonu yetkileri Options Pro’dan otomatik çıkarılmaz.
İkinci sağlayıcı eklemek sinyal kalitesini otomatik artırmaz; ölçüm hatasını
gidermek ve hangi filtrenin masraflardan sonra katkı verdiğini sınamak içindir.

## İlk araştırma sırası

1. Dondurulmuş sekiz Kasım adayındaki veri eksiklerini aynı kontrat/saatlerle
   tamamla; eksik kaldı diye kontrat değiştirme.
2. Aynı gözlemlerde UW/Theta fiyat ve zaman farklarını ölç. Uyuşmazlıkları
   getiriye göre sağlayıcı seçerek kapatma.
3. Spread, komisyon ve gecikme maliyetini sabit tutarak yeni, önceden seçilmiş
   seanslarda CALL ve PUT sonuçlarını ayrı değerlendir.
4. Aynı kohortta IV/Greeks ve ertesi gün OI bilgisinin **karardan önce mevcut**
   olan kısmıyla ek katkı sınaması yap. Sonradan bilinen OI eski karara eklenmez.
5. Maliyet, gecikme, en iyi işlem/hisse çıkarma kontrolleri geçmeden canlı
   kuralları değiştirme. Bu çalışmada seçilmiş veya kanıtlanmış kazanan yok.

`scripts/probe_theta_capabilities.py` bu ihtiyaçlar için on küçük, salt okunur
istek planlar; varsayılanı ağ çağrısı yapmayan dry-run’dır. `--execute` ve
yetkili Terminal adresi verilince ham cevapları ve durumları kaydeder.

## Bu oturumda gerçek veride bulunanlar

- 66 UW API GET denemesi; ayrıca mevcut ürünün salt okunur raporları incelendi.
- AAPL 18 Eylül: ilk sayfa 500, yedi sayfa 3.498 kontrat. İlk sayfada 14–60 DTE
  sıfır, tam cevapta 676 kontrat. Tarihsel indirici düzeltildi; eski kayıtlar
  değiştirilmedi. 183 eski 500 satırlık arşivin tamlık iddiası doğrulanmamış.
  Canlı aday yolu ayrı `/option-contracts` sorgularında açık kontrat listeleri
  kullanıyor; bu tarihsel hata canlı kayıpların nedeni olarak gösterilemez.
- Eski QQQ örneğinde varsayılan stop fiyatı −$47,20, gözlenen günlük kotasyon
  −$61,20 verir. Motor artık günlük kotasyonda görülmeyen daha iyi fill yazmaz.
- Sekiz eski hisse adayına geriye dönük, sonuç görülmeden seçilen CALL
  eşlemesinde yalnız iki +60 dakika sonucu fiyatlanabildi: TSLA −$88,30,
  META −$228,30. Altı diğer aday ve bütün +15 sonuçları UNKNOWN. Bunlar gerçek
  emir veya eksiksiz portföy sonucu değildir; toplam getiri çıkarılmadı.

**ThetaData canlı doğrulaması:** henüz yapılmadı. Yerel gerçek HTTP taşıma testi
sentetik sağlayıcı cevaplarıyla geçti; bu test kullanıcı hesabına erişim kanıtı
değildir. Canlıya alma ve gerçek Mac bağlantısından sonra bu durum güncellenir.
