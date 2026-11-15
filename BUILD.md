# Python SDK derleme ve test

Python 3.12 (doğrulama ortamında 3.12.4) ve PEP 517 için setuptools 80.9.0 kullanılır. Çalışma zamanında ek bağımlılık yoktur.

```powershell
powershell -NoProfile -File .\ci.ps1
```

Çevrimdışı derleme için `-Wheelhouse` ile setuptools wheel dosyasını içeren dizini geçir. `out/packages/` altındaki SDK wheel'i yerel venv'e yüklenir, smoke testi dağıtım sürümünü kurulu paketten okur.

Python PSF lisanslıdır. Setuptools MIT lisanslı bir derleme bağımlılığıdır. [Setuptools 80.9.0](https://pypi.org/project/setuptools/80.9.0/)
