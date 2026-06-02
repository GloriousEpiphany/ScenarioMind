# Streamlit 8504 WinError 10013

## Symptom

Running `python -m streamlit run visualization/app.py --server.port 8504`
fails before the app imports with `PermissionError: [WinError 10013]`.

## Root Cause

Windows has TCP excluded port range `8436-8535`, which includes `8504`.
Binding any port in that range is rejected by the OS.

Confirmed with:

```powershell
netsh interface ipv4 show excludedportrange protocol=tcp
```

Relevant range:

```text
8436 8535
```

## Fix / Workaround

Use a port outside the excluded ranges, for example:

```powershell
python -m streamlit run visualization/app.py --server.port 8600
```

Verified `http://localhost:8600` returned HTTP 200.
