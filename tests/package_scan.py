"""Inspect the actual wheel and sdist without extracting them."""
import ast
import io
import sys
import tarfile
import zipfile

CONTRACT = "stardataparadise_sdk/contracts/sdp-contract-v1.zip"

def inspect(entries):
    files = [(name, data) for name, data in entries if "stardataparadise_sdk/" in name]
    assert any(name.endswith(CONTRACT) for name, data in files)
    assert all(not name.endswith((".pem", ".key", ".pfx", ".env")) for name, data in files)
    for name, data in files:
        if not name.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(data)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "-----BEGIN PRIVATE KEY-----" not in node.value
                assert "-----BEGIN RSA PRIVATE KEY-----" not in node.value
                assert not node.value.startswith(("ghp_", "github_pat_", "pypi-"))
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assert target.id.lower() not in {"password", "token", "secret", "api_key"}
    print("Source archive certificate and credential scan passed")

with zipfile.ZipFile(sys.argv[1]) as wheel:
    inspect((name, wheel.read(name)) for name in wheel.namelist())
with tarfile.open(sys.argv[2], "r:gz") as sdist:
    inspect((item.name, sdist.extractfile(item).read()) for item in sdist.getmembers() if item.isfile())