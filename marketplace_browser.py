"""Launch a direct Chrome session for Facebook Marketplace scans."""

import os
from pathlib import Path
import sys


def _chrome_major(browser_path):
    """Read Chrome's installed major version to find a cached matching driver."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class FixedInfo(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in (
            "signature", "structure_version", "file_version_ms", "file_version_ls",
            "product_version_ms", "product_version_ls", "flags_mask", "flags",
            "os", "file_type", "subtype", "file_date_ms", "file_date_ls",
        )]

    version = ctypes.windll.version
    size = version.GetFileVersionInfoSizeW(str(browser_path), None)
    if not size:
        raise RuntimeError("Could not read installed Chrome version")
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(browser_path), 0, size, buffer):
        raise RuntimeError("Could not inspect installed Chrome version")
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        raise RuntimeError("Could not parse installed Chrome version")
    return ctypes.cast(pointer, ctypes.POINTER(FixedInfo)).contents.file_version_ms >> 16


def _cached_chromedriver():
    """Use an installed driver if Selenium Manager cannot reach the network."""
    explicit = os.environ.get("SE_CHROMEDRIVER")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise RuntimeError("SE_CHROMEDRIVER does not point to an existing file")
        return path
    if sys.platform != "win32":
        return None
    chrome = next((path for path in (
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) /
        "Google/Chrome/Application/chrome.exe",
    ) if path.is_file()), None)
    if chrome is None:
        return None
    major = _chrome_major(chrome)
    cache = Path.home() / ".cache/selenium/chromedriver/win64"
    candidates = sorted(cache.glob(f"{major}.*/*chromedriver.exe"), reverse=True)
    return candidates[0] if candidates else None


def create_driver(*, headless=False, profile_dir=None):
    """Return a direct Selenium Chrome driver and its cleanup function."""
    from selenium import webdriver

    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
    if profile_dir is not None:
        profile = Path(profile_dir).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--no-proxy-server")
    cached = _cached_chromedriver()
    if cached:
        from selenium.webdriver.chrome.service import Service
        driver = webdriver.Chrome(service=Service(str(cached)), options=options)
    else:
        driver = webdriver.Chrome(options=options)
    return driver, driver.quit
