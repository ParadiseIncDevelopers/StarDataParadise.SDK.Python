"""Run the installed SDK's pytest contract with a machine-readable completion marker."""
from pathlib import Path
import sys
import pytest

result = pytest.main(["-q", "-s", "--import-mode=importlib", "-p", "no:cacheprovider",
                      str(Path(__file__).with_name("test_contract.py"))])
if result == 0:
    print("SDP_MANAGED_OK")
raise SystemExit(result)