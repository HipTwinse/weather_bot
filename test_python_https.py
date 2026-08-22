import asyncio
import ssl


async def main():
    host = "api.telegram.org"
    port = 443

    print("=" * 60)
    print("PYTHON DIRECT TLS DIAGNOSTIC")
    print("=" * 60)
    print(f"Target: {host}:{port}")
    print()

    try:
        print("1. TCP connection...")

        reader, writer = await asyncio.open_connection(
            host,
            port,
        )

        print("   ✅ TCP connection established")

        print("2. TLS handshake...")

        ssl_context = ssl.create_default_context()

        transport = writer.transport
        protocol = writer._protocol

        loop = asyncio.get_running_loop()

        new_transport = await loop.start_tls(
            transport,
            protocol,
            ssl_context,
            server_hostname=host,
        )

        print("   ✅ TLS handshake successful")

        new_writer = asyncio.StreamWriter(
            new_transport,
            protocol,
            reader,
            loop,
        )

        print("3. HTTPS request...")

        request = (
            f"HEAD / HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )

        new_writer.write(request.encode())
        await new_writer.drain()

        response = await reader.read(4096)

        print()
        print("Server response:")
        print(response.decode(errors="replace"))

        new_writer.close()
        await new_writer.wait_closed()

        print()
        print("=" * 60)
        print("✅ DIRECT TLS TEST PASSED")
        print("=" * 60)

    except Exception as e:
        print()
        print("=" * 60)
        print("❌ DIRECT TLS TEST FAILED")
        print("=" * 60)
        print(f"{type(e).__name__}: {e}")
        print()

        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())