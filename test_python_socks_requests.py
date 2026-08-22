import requests

PROXY = "socks5h://127.0.0.1:10808"

proxies = {
    "http": PROXY,
    "https": PROXY,
}

print("=" * 60)
print("PYTHON REQUESTS + SOCKS5 DIAGNOSTIC")
print("=" * 60)
print(f"Proxy: {PROXY}")
print()

try:
    print("Connecting to Telegram...")

    response = requests.get(
        "https://api.telegram.org",
        proxies=proxies,
        timeout=20,
    )

    print(f"✅ HTTP status: {response.status_code}")
    print(f"✅ Response length: {len(response.content)} bytes")

    print()
    print("=" * 60)
    print("✅ REQUESTS TEST PASSED")
    print("=" * 60)

except Exception as e:
    print()
    print("=" * 60)
    print("❌ REQUESTS TEST FAILED")
    print("=" * 60)
    print(f"{type(e).__name__}: {e}")