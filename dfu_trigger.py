import asyncio
from bleak import BleakScanner, BleakClient

TRIGGER_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
STATUS_UUID  = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"

async def scan_triki(timeout=8):
    devices = await BleakScanner.discover(timeout=timeout)
    app = []
    dfu = []

    for d in devices:
        name = d.name or ""
        if name.startswith("TrikiDfu"):
            dfu.append(d)
        elif name.startswith("Triki"):
            app.append(d)

    return app, dfu

async def main():
    print("[scan] looking for Triki...")
    app, dfu = await scan_triki()

    for d in app:
        print(f"[app] {d.address}  {d.name}")
    for d in dfu:
        print(f"[dfu] {d.address}  {d.name}")

    if dfu and not app:
        print("[ok] already in DFU")
        return

    if not app:
        print("[fail] no app-mode Triki found")
        return

    dev = app[0]
    print(f"[connect] {dev.address}  {dev.name}")

    try:
        async with BleakClient(dev, timeout=15) as c:
            try:
                v = await c.read_gatt_char(STATUS_UUID)
                print(f"[status before] {v.hex()}")
            except Exception as e:
                print(f"[status before] read failed: {e}")

            print("[trigger] write 42 -> 6e400002")
            await c.write_gatt_char(TRIGGER_UUID, b"\x42", response=False)

            await asyncio.sleep(0.5)

            try:
                v = await c.read_gatt_char(STATUS_UUID)
                print(f"[status after] {v.hex()}")
            except Exception as e:
                print(f"[status after] read failed / reboot expected: {e}")

    except Exception as e:
        print(f"[connect/write note] {e}")

    print("[wait] scanning for TrikiDfu...")
    for i in range(8):
        await asyncio.sleep(1)
        app2, dfu2 = await scan_triki(timeout=2)

        for d in dfu2:
            print(f"[dfu] {d.address}  {d.name}")
            return

        if app2:
            print(f"[scan {i+1}] still app mode: " + ", ".join(f"{d.address} {d.name}" for d in app2))
        else:
            print(f"[scan {i+1}] no Triki seen")

    print("[fail] DFU not seen")

asyncio.run(main())