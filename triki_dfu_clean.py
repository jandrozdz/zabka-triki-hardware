"""Small, self-contained updater for genuine Triki Nordic Secure DFU packages.

Default mode validates the package with the command object only.  Add --write
only after that succeeds to send the firmware data.

Requirements: pip install bleak
"""

import argparse
import asyncio
import sys
import zipfile
import zlib

from bleak import BleakClient, BleakScanner

TRIGGER_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
CP_UUID = "8ec90001-f315-4f60-9fb8-838830daea50"
PKT_UUID = "8ec90002-f315-4f60-9fb8-838830daea50"

CREATE, SET_PRN, CRC, EXECUTE, SELECT, RESPONSE = 1, 2, 3, 4, 6, 0x60
COMMAND, DATA = 1, 2

RESULT = {
    0: "INVALID", 1: "SUCCESS", 2: "OP_NOT_SUPPORTED", 3: "INVALID_PARAMETER",
    4: "INSUFFICIENT_RESOURCES", 5: "INVALID_OBJECT", 7: "UNSUPPORTED_TYPE",
    8: "NOT_PERMITTED", 10: "OP_FAILED", 11: "EXT_ERROR",
}
EXT = {
    4: "INIT_COMMAND_INVALID", 5: "FW_VERSION", 6: "HW_VERSION",
    7: "SD_VERSION", 8: "SIGNATURE_MISSING", 9: "WRONG_HASH_TYPE",
    10: "HASH_FAILED", 11: "WRONG_SIGNATURE_TYPE", 12: "VERIFICATION_FAILED",
    13: "INSUFFICIENT_SPACE",
}


def package_contents(path):
    with zipfile.ZipFile(path) as archive:
        manifest = archive.read("manifest.json")
        names = archive.namelist()
        dat = next(name for name in names if name.lower().endswith(".dat"))
        image = next(name for name in names if name.lower().endswith(".bin"))
        return archive.read(dat), archive.read(image), manifest


async def scan_one(prefix, timeout, suffix=""):
    print(f"[scan] looking for {prefix}...")
    devices = await BleakScanner.discover(timeout=timeout)
    choices = [d for d in devices if (d.name or "").startswith(prefix)]
    if suffix:
        matching = [d for d in choices if (d.name or "").endswith(suffix)]
        choices = matching or choices
    if not choices:
        return None
    device = choices[0]
    print(f"[scan] found {device.address}  {device.name}")
    return device


async def enter_dfu(timeout):
    app = await scan_one("Triki ", timeout)
    if not app:
        print("[trigger] normal-mode Triki not found; checking DFU mode directly")
        return await scan_one("TrikiDfu", timeout)

    suffix = (app.name or "").removeprefix("Triki ")
    print("[trigger] sending 42 to the normal-mode DFU characteristic")
    try:
        async with BleakClient(app, timeout=20) as client:
            await client.write_gatt_char(TRIGGER_UUID, b"\x42", response=False)
    except Exception as exc:
        # Windows usually reports a cancelled operation because the Triki reboots.
        print(f"[trigger] disconnect during reboot is normal: {exc}")

    await asyncio.sleep(2)
    return await scan_one("TrikiDfu", timeout, suffix)


class Dfu:
    def __init__(self, client):
        self.client = client
        self.responses = asyncio.Queue()

    def notification(self, _sender, value):
        self.responses.put_nowait(bytes(value))

    async def start(self):
        await self.client.start_notify(CP_UUID, self.notification)

    async def command(self, value):
        while not self.responses.empty():
            self.responses.get_nowait()
        await self.client.write_gatt_char(CP_UUID, value, response=True)
        reply = await asyncio.wait_for(self.responses.get(), timeout=20)
        if len(reply) < 3 or reply[0] != RESPONSE:
            raise RuntimeError(f"unexpected control-point reply: {reply.hex()}")
        if reply[2] != 1:
            detail = ""
            if reply[2] == 11 and len(reply) > 3:
                detail = f" ({EXT.get(reply[3], hex(reply[3]))})"
            raise RuntimeError(f"DFU {RESULT.get(reply[2], hex(reply[2]))}{detail}")
        return reply[3:]

    async def select(self, object_type):
        payload = await self.command(bytes([SELECT, object_type]))
        if len(payload) != 12:
            raise RuntimeError(f"bad Select reply: {payload.hex()}")
        maximum = int.from_bytes(payload[0:4], "little")
        offset = int.from_bytes(payload[4:8], "little")
        checksum = int.from_bytes(payload[8:12], "little")
        print(f"[dfu] select {object_type}: max={maximum}, offset={offset}, crc={checksum:08x}")
        return maximum, offset, checksum

    async def create(self, object_type, size):
        await self.command(bytes([CREATE, object_type]) + size.to_bytes(4, "little"))

    async def send(self, content, chunk_size):
        for offset in range(0, len(content), chunk_size):
            await self.client.write_gatt_char(PKT_UUID, content[offset:offset + chunk_size], response=False)
            await asyncio.sleep(0.001)

    async def check_crc(self):
        payload = await self.command(bytes([CRC]))
        return int.from_bytes(payload[0:4], "little"), int.from_bytes(payload[4:8], "little")

    async def execute(self):
        await self.command(bytes([EXECUTE]))


async def validate_init(dfu, init, chunk_size):
    maximum, _, _ = await dfu.select(COMMAND)
    if len(init) > maximum:
        raise RuntimeError(f"init is {len(init)} bytes but bootloader allows {maximum}")
    await dfu.command(bytes([SET_PRN, 0, 0]))
    await dfu.create(COMMAND, len(init))
    await dfu.send(init, chunk_size)
    offset, checksum = await dfu.check_crc()
    expected = zlib.crc32(init) & 0xFFFFFFFF
    if (offset, checksum) != (len(init), expected):
        raise RuntimeError(f"init CRC mismatch: board {offset}/{checksum:08x}, expected {len(init)}/{expected:08x}")
    await dfu.execute()
    print("[ok] init accepted")


async def upload_data(dfu, image, chunk_size):
    maximum, offset, checksum = await dfu.select(DATA)
    if offset:
        raise RuntimeError("data object already exists; reboot the Triki and retry")
    running = 0
    sent = 0
    for start in range(0, len(image), maximum):
        block = image[start:start + maximum]
        await dfu.create(DATA, len(block))
        await dfu.send(block, chunk_size)
        sent += len(block)
        running = zlib.crc32(block, running) & 0xFFFFFFFF
        offset, checksum = await dfu.check_crc()
        if (offset, checksum) != (sent, running):
            raise RuntimeError(f"data CRC mismatch at {sent}: board {offset}/{checksum:08x}, expected {sent}/{running:08x}")
        await dfu.execute()
        print(f"[write] {sent}/{len(image)} bytes")


async def main():
    parser = argparse.ArgumentParser(description="Triki genuine-package updater")
    parser.add_argument("zip", help="genuine Nordic DFU ZIP")
    parser.add_argument("--write", action="store_true", help="send firmware after the non-destructive init check")
    parser.add_argument("--scan-timeout", type=float, default=8)
    parser.add_argument("--chunk", type=int, default=20, help="BLE payload size; 20 is safest")
    args = parser.parse_args()
    if not 1 <= args.chunk <= 240:
        parser.error("--chunk must be 1..240")

    init, image, _manifest = package_contents(args.zip)
    print(f"[zip] init={len(init)} bytes, image={len(image)} bytes")
    device = await enter_dfu(args.scan_timeout)
    if not device:
        raise RuntimeError("no TrikiDfu device found")

    async with BleakClient(device, timeout=30) as client:
        dfu = Dfu(client)
        await dfu.start()
        await validate_init(dfu, init, args.chunk)
        if not args.write:
            print("[done] no firmware data was sent. Re-run with --write to install it.")
            return
        print("[write] starting firmware transfer")
        await upload_data(dfu, image, args.chunk)
        print("[done] transfer complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[stopped]")
    except Exception as exc:
        print(f"[failed] {exc}")
        sys.exit(2)
