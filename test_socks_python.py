import asyncio
import socket
import ssl


PROXY_HOST = "127.0.0.1"
PROXY_PORT = 10808
TARGET_HOST = "api.telegram.org"
TARGET_PORT = 443


async def main():
    print("=" * 60)
    print("PYTHON SOCKS5 LOW-LEVEL DIAGNOSTIC")
    print("=" * 60)

    print(f"Proxy: {PROXY_HOST}:{PROXY_PORT}")
    print(f"Target: {TARGET_HOST}:{TARGET_PORT}")
    print()

    try:
        print("1. Подключаемся к SOCKS5...")

        reader, writer = await asyncio.open_connection(
            PROXY_HOST,
            PROXY_PORT,
        )

        print("   ✅ TCP-соединение с SOCKS5 установлено")

        # SOCKS5 greeting:
        # VER = 5
        # NMETHODS = 1
        # METHOD = 0 (NO AUTH)
        writer.write(bytes([5, 1, 0]))
        await writer.drain()

        response = await reader.readexactly(2)

        print(f"   SOCKS5 greeting response: {response!r}")

        if response != bytes([5, 0]):
            print("   ❌ SOCKS5 не согласился на NO AUTH")
            writer.close()
            await writer.wait_closed()
            return

        print("   ✅ SOCKS5 handshake OK")

        # SOCKS5 CONNECT request
        target_bytes = TARGET_HOST.encode("idna")

        request = (
            bytes([5, 1, 0, 3, len(target_bytes)])
            + target_bytes
            + TARGET_PORT.to_bytes(2, "big")
        )

        print("2. Запрашиваем CONNECT к Telegram...")

        writer.write(request)
        await writer.drain()

        response = await reader.readexactly(4)

        print(f"   CONNECT response header: {response!r}")

        if response[1] != 0:
            print(f"   ❌ SOCKS5 CONNECT rejected, code={response[1]}")
            writer.close()
            await writer.wait_closed()
            return

        address_type = response[3]

        if address_type == 1:
            await reader.readexactly(4)

        elif address_type == 3:
            domain_length = (await reader.readexactly(1))[0]
            await reader.readexactly(domain_length)

        elif address_type == 4:
            await reader.readexactly(16)

        else:
            print(f"   ❌ Unknown address type: {address_type}")
            writer.close()
            await writer.wait_closed()
            return

        await reader.readexactly(2)

        print("   ✅ SOCKS5 CONNECT к Telegram установлен")

        print("3. Запускаем TLS handshake...")

        ssl_context = ssl.create_default_context()

        loop = asyncio.get_running_loop()

        transport = writer.transport
        protocol = writer._protocol

        new_transport = await loop.start_tls(
            transport,
            protocol,
            ssl_context,
            server_hostname=TARGET_HOST,
        )

        print("   ✅ TLS handshake успешно завершён")

        new_writer = asyncio.StreamWriter(
            new_transport,
            protocol,
            reader,
            loop,
        )

        print("4. Отправляем HTTPS HEAD...")

        request = (
            f"HEAD / HTTP/1.1\r\n"
            f"Host: {TARGET_HOST}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )

        new_writer.write(request.encode())
        await new_writer.drain()

        response_data = await reader.read(4096)

        print()
        print("Ответ сервера:")
        print(response_data.decode(errors="replace"))

        new_writer.close()
        await new_writer.wait_closed()

        print()
        print("=" * 60)
        print("✅ LOW-LEVEL TEST PASSED")
        print("=" * 60)

    except Exception as e:
        print()
        print("=" * 60)
        print("❌ LOW-LEVEL TEST FAILED")
        print("=" * 60)
        print(f"{type(e).__name__}: {e}")
        print()
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())