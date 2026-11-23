# StarDataParadise Python SDK

Python 3.12+ için DB-API 2.0 istemcisi. StarDataParadise Server’a TLS üzerinden wire v1 ile bağlanır. Çalışma zamanında Python standart kütüphanesini kullanır. `0.3.0` paketi sabit wire sözleşmesini de içerir.

## Paketi kurma

Sürüm wheel’ini edinin veya aşağıdaki derleme adımıyla üretin. Örnek depo kökünde, `out/packages` altındaki dosyayı ayrı bir sanal ortama kurar.

```powershell
python -m venv out/client-env
.\out\client-env\Scripts\python.exe -m pip install --no-deps .\out\packages\stardataparadise_sdk-0.3.0-py3-none-any.whl
```

## TLS ve parametreli SQL

Örnek için mevcut `items(id, note)` tablosuna okuma yetkisi gerekir. `SDP_USER`, `SDP_PASSWORD` ve CA PEM dosyasına işaret eden `SDP_CA` ortam değişkenlerini sağlayın. `tls_name`, sunucu sertifikasının doğrulanacak adıdır.

```python
import os

from stardataparadise_sdk import connect

with connect(
    host="127.0.0.1",
    port=7443,
    user=os.environ["SDP_USER"],
    password=os.environ["SDP_PASSWORD"],
    ca_file=os.environ["SDP_CA"],
    tls_name="localhost",
) as connection:
    with connection.cursor() as cursor:
        cursor.execute("SELECT note FROM items WHERE id = :key", {"key": 7})
        print(cursor.fetchmany(16))
```

- İsimli bağları `execute` çağrısına ayrı sözlükle verin. Tipli NULL için `Bind(None, "TEXT")` gibi bir değer kullanın. `fetchmany` ile satırları gruplar halinde okuyun.
- Varsayılan manuel commit kipinde bağlantı bağlamı başarıda commit, hatada rollback yapar ve bağlantıyı kapatır. Bir bağlantı tek etkin sonuç taşır. Ayrı iş parçacıkları için ayrı bağlantılar açın.
- `callproc` ve `nextset`, yordamın OUT satırını ve sonraki sonuç kümelerini okur. `capabilities()` ile yönetim, model ve iş çağrılarının sunucu desteğini sorgulayabilirsiniz.
- Sonucu belirsiz yazmada `OutcomeUnknown.request_token` değerini aynı kimlikle açılan yeni bağlantıda `outcome(token)` ile sorgulayın. `unknown` kaydı sonucu belirsiz bırakır.

## Native SQL ve AI kapsamı

`connect_native()` ayrı bir native SQL/AI oturumu açar; kullanım örneği [BUILD.md](BUILD.md) içindedir. Yeni SQL `HIST`/SQLgit ve SQL `JOB` zamanlama özellikleri ertelenmiştir ve bu sürümde etkin değildir. Mevcut `history()`, `submit_compute()` ve `job()` wire API'leri sunucunun capability/yetki kontrolleriyle korunur.

## Wheel, sdist ve testler

Python 3.12 ile paketleyin. `-Wheelhouse` sabit setuptools wheel’inin, `-TestWheelhouse` ise [sabit build ve pytest wheel’lerinin](tests/requirements-build.txt) bulunduğu dizindir.

```powershell
pwsh -NoProfile -File .\ci.ps1 -Wheelhouse C:\SDP\wheelhouse -TestWheelhouse C:\SDP\test-wheelhouse
```

CI, `out/packages` altında wheel ve sdist üretir ve paketleri ayrı ortamlara kurup sınar. Yerel canlı TLS kabulü için `-Server`, `-OpenSsl` ve `-FixtureHarness` seçeneklerini birlikte verin. [DB-API davranışı, yordam ve yönetim örnekleri](BUILD.md) · [Server ve diğer SDK’lar](https://github.com/ParadiseIncDevelopers/StarDataParadiseServer)

## Lisans

Özgün Python SDK, örnekler ve belgeler [MIT](LICENSE) lisanslıdır.
Telif ve izin bildirimi korunarak ticari kullanım, değiştirme ve yeniden
dağıtıma izin verilir. Paket metadata'sı MIT lisansını ve dağıtıma eklenecek
LICENSE dosyasını belirtir. Ayrıntılar: [LICENSING.md](LICENSING.md).
