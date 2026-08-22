import asyncio

from aiohttp import ClientSession
from aiohttp_socks import ProxyConnector


async def test_url(session, url):
    print(f"\n🌐 Проверяем: {url}")

    try:
        async with session.get(url, timeout=20) as response:
            print(f"✅ HTTP STATUS: {response.status}")

            text = await response.text()
            print(f"📡 Первые 200 символов ответа:")
            print(text[:200])

    except Exception as e:
        print(f"❌ ОШИБКА: {type(e).__name__}: {e}")


async def main():
    proxy_url = "socks5://127.0.0.1:10808"

    print("=" * 60)
    print("SOCKS5 PROXY DIAGNOSTIC")
    print("=" * 60)
    print(f"🔌 Proxy: {proxy_url}")

    connector = ProxyConnector.from_url(proxy_url)

    async with ClientSession(connector=connector) as session:
        await test_url(session, "https://www.google.com")
        await test_url(session, "https://api.telegram.org")


if __name__ == "__main__":
    asyncio.run(main())