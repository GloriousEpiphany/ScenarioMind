"""Explicit Windows Schannel HTTPS transport for public data endpoints.

Windows live probes reproduced failures in OpenSSL/BoringSSL while the native
Schannel stack successfully read the exact same certificate-verified HTTPS endpoint.
This profile is chosen before a request, never as an exception-time fallback.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlencode, urlsplit

from scenario_engine.errors import ContractViolation, DataUnavailable


@dataclass(frozen=True)
class PublicHTTPResponse:
    text: str
    status_code: int

    def raise_for_status(self):
        if self.status_code != 200:
            raise DataUnavailable("public HTTPS endpoint did not return HTTP 200",
                                  context={"http_status": self.status_code})

    def json(self):
        return json.loads(self.text)


def windows_schannel_get(url, *, params, timeout):
    target = urlsplit(url)
    if (target.scheme != "https" or target.netloc != "push2his.eastmoney.com"
            or target.query or target.fragment):
        raise ContractViolation("native public transport only permits its fixed HTTPS endpoint")
    if target.path not in {"/api/qt/stock/fflow/daykline/get", "/api/qt/stock/kline/get"}:
        raise ContractViolation("native public transport endpoint is outside its approved scope")
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    if os.name != "nt" or not executable.is_file():
        raise DataUnavailable("Windows Schannel curl transport is not installed; no TLS downgrade is allowed")
    connect_timeout, read_timeout = timeout
    argv = [str(executable), "--disable", "--proto", "=https", "--silent", "--show-error", "--fail",
            "--connect-timeout", str(connect_timeout), "--max-time", str(connect_timeout + read_timeout),
            "--max-filesize", "4194304", "--write-out", "\n%{http_code}", url + "?" + urlencode(params)]
    try:
        result = subprocess.run(argv, capture_output=True,
            timeout=connect_timeout + read_timeout + 5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise DataUnavailable("public HTTPS native transport exceeded its deadline") from exc
    if result.returncode:
        raise DataUnavailable("public HTTPS native transport failed; source and certificate checks were retained",
                              context={"curl_exit_code": result.returncode})
    try:
        output = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractViolation("native HTTPS transport response is not UTF-8 JSON") from exc
    body, separator, status = output.rpartition("\n")
    if not separator or not status.isdigit():
        raise ContractViolation("native HTTPS transport returned an incomplete response envelope")
    response = PublicHTTPResponse(body, int(status))
    response.raise_for_status()
    return response


def configured_public_http_profile():
    profile = (os.environ.get("STOCKM1_PUBLIC_HTTP_PROFILE") or "curl_cffi_tls_v1").strip()
    if profile not in {"curl_cffi_tls_v1", "windows_schannel_v1", "requests_tls_v1"}:
        raise ContractViolation("unknown explicit public HTTPS transport profile")
    return profile


def select_public_http_transport(profile=None):
    """Select once, before any request; failures never change this profile."""
    profile = profile or configured_public_http_profile()
    if profile == "windows_schannel_v1":
        return profile, windows_schannel_get
    if profile == "curl_cffi_tls_v1":
        from curl_cffi import requests as client
    elif profile == "requests_tls_v1":
        import requests as client
    else:
        raise ContractViolation("unknown explicit public HTTPS transport profile")

    def get(url, *, params, timeout):
        target = urlsplit(url)
        if (target.scheme != "https" or target.netloc != "push2his.eastmoney.com"
                or target.path not in {"/api/qt/stock/fflow/daykline/get", "/api/qt/stock/kline/get"}
                or target.query or target.fragment):
            raise ContractViolation("public JSON transport endpoint is outside its approved scope")
        response = client.get(url, params=params, timeout=timeout, allow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200:
            raise DataUnavailable("public HTTPS source did not return HTTP 200; redirects are not followed")
        return response
    return profile, get
