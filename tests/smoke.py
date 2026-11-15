from importlib.metadata import version

import stardataparadise_sdk


if stardataparadise_sdk.__version__ != "0.2.0":
    raise SystemExit("Unexpected package version")
if version("stardataparadise-sdk") != stardataparadise_sdk.__version__:
    raise SystemExit("Wheel metadata does not match the public version")
print(stardataparadise_sdk.__version__)
