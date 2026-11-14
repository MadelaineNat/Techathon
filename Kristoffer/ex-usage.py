
import asyncio

from ble_scanner2 import Scanner

def entered(installation_id):
    # Plain function: must return quickly
    print(f"ENTER installation {installation_id}")


async def left(installation_id):
    # Async function: free to await things without blocking the scan
    print(f"LEAVE installation {installation_id}")


async def main():
    scanner = Scanner(on_enter=entered, on_leave=left, verbose=False)
    await scanner.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass