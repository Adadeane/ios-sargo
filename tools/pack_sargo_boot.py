#!/usr/bin/env python3
"""Pack U-Boot, HakiOS loader.efi, DTB, and optional XNUP payload into a Pixel 3a (sargo) boot.img v2.

Google ABL on Pixel 3a requires:
  - boot_img_hdr_v2 with separate --dtb section and 4096-byte page alignment
  - os_version 15.0.0 / os_patch_level 2025-11 (matching LineageOS/Android 15 ABL checks)
  - LZ4-compressed kernel image (raw Image is rejected by ABL as "Unsupported")
"""
import argparse
import hashlib
import shutil
import subprocess
import struct
import zlib
from pathlib import Path

PAGE = 4096
BASE = 0x00000000
KERNEL_OFFSET = 0x00008000
RAMDISK_OFFSET = 0x01000000
SECOND_OFFSET = 0x00F00000
TAGS_OFFSET = 0x00000100
DTB_OFFSET = 0x01F00000

# os_version=15.0.0, os_patch_level=2025-11 -> ((15<<14)|(0<<7)|0)<<11 | ((2025-2000)<<4)|11
OS_VERSION = ((15 << 14) << 11) | ((2025 - 2000) << 4) | 11

HKUB_MAGIC = b"HKUB"
HKUB_VERSION = 1
PAYLOAD_MAGIC = b"XNUP"
PAYLOAD_LIMIT = 0x04000000 - 0x100000


def hkub(entries):
    """Build U-Boot's HKUB ramdisk container with 4 KiB aligned entries and CRC-32."""
    header_size = 16 + 32 * len(entries)
    first = (header_size + 0xFFF) & ~0xFFF
    table = bytearray()
    body = bytearray()
    for name, data in entries:
        encoded = name.encode("ascii")
        if len(encoded) >= 16:
            raise ValueError(f"HKUB entry name too long: {name}")
        offset = first + len(body)
        table += struct.pack("<16sIIII", encoded, offset, len(data), zlib.crc32(data), 0)
        body += data
        body += b"\0" * (-len(body) % 0x1000)
    head = struct.pack("<4sIII", HKUB_MAGIC, HKUB_VERSION, len(entries), header_size) + table
    return head + b"\0" * (first - len(head)) + body


def compress_lz4(data: bytes) -> bytes:
    """Compress kernel image as an LZ4 legacy/frame blob accepted by Pixel 3a ABL."""
    lz4_bin = shutil.which("lz4")
    if lz4_bin:
        proc = subprocess.run([lz4_bin, "-f", "-q", "-9", "-", "-"], input=data, capture_output=True, check=True)
        return proc.stdout
    try:
        import lz4.frame  # type: ignore
    except ImportError as exc:
        raise SystemExit("Install `lz4` CLI or Python `lz4` package to compress U-Boot for Pixel 3a ABL") from exc
    return lz4.frame.compress(
        data,
        compression_level=9,
        block_size=lz4.frame.BLOCKSIZE_MAX256KB,
        block_linked=False,
        content_checksum=True,
        store_size=False,
    )


def boot_image_v2(kernel: bytes, ramdisk: bytes, dtb: bytes) -> bytes:
    """Build Android boot.img header v2 with 4096-byte pages for Google Pixel 3a ABL."""
    sha = hashlib.sha1()
    for part in (kernel, ramdisk, b"", b"", dtb):
        sha.update(part)
        sha.update(struct.pack("<I", len(part)))

    header_size = 1660
    header = struct.pack(
        "<8s10I16s512s32s1024sIQIIQ",
        b"ANDROID!",
        len(kernel), BASE + KERNEL_OFFSET,
        len(ramdisk), BASE + RAMDISK_OFFSET,
        0, BASE + SECOND_OFFSET,
        BASE + TAGS_OFFSET,
        PAGE,
        2,  # header_version
        OS_VERSION,
        b"",
        b"console=null",
        sha.digest(),
        b"",
        0, 0,  # recovery_dtbo_size, recovery_dtbo_offset
        header_size,
        len(dtb), BASE + DTB_OFFSET,
    )
    assert len(header) == header_size

    def pad(blob: bytes) -> bytes:
        return blob + b"\0" * (-len(blob) % PAGE)

    return pad(header) + pad(kernel) + pad(ramdisk) + pad(dtb)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--u-boot", type=Path, required=True)
    p.add_argument("--loader", type=Path, required=True)
    p.add_argument("--dtb", type=Path, required=True)
    p.add_argument("--env", type=Path)
    p.add_argument("--payload", type=Path)
    p.add_argument("--no-lz4", action="store_true", help="Keep raw U-Boot binary (only if ABL accepts it)")
    p.add_argument(
        "--no-patch-text-offset",
        action="store_true",
        help="Do not rewrite zero text_offset in the ARM64 Image header to 0x80000",
    )
    p.add_argument("-o", "--output", type=Path, default=Path("build/hakios-sargo-boot.img"))
    a = p.parse_args()

    ub = bytearray(a.u_boot.read_bytes())
    if len(ub) < 64 or ub[0x38:0x3C] != b"ARM\x64":
        p.error(f"{a.u_boot} is not an AArch64 Linux image (missing ARMd header)")
    text_offset = struct.unpack_from("<Q", ub, 8)[0]
    if text_offset == 0:
        if a.no_patch_text_offset:
            print("warning: U-Boot image text_offset is 0; Pixel 3a ABL may reject it")
        else:
            struct.pack_into("<Q", ub, 8, 0x80000)
            print("patched ARM64 Image text_offset 0 -> 0x80000 for Pixel 3a ABL")

    loader = a.loader.read_bytes()
    if not loader.startswith(b"MZ"):
        p.error(f"{a.loader} is not a UEFI application")

    entries = [("loader", loader)]
    if a.payload:
        payload = a.payload.read_bytes()
        if len(payload) < 8 or payload[:4] != PAYLOAD_MAGIC:
            p.error(f"{a.payload} is not an XNUP payload")
        if len(payload) > PAYLOAD_LIMIT:
            p.error(f"payload exceeds 63 MiB UEFI reservation ({len(payload)} bytes)")
        entries.insert(0, ("payload", payload))
    if a.env:
        entries.append(("env", a.env.read_bytes()))

    ramdisk = hkub(entries)
    dtb = a.dtb.read_bytes()
    ub_bytes = bytes(ub)
    kernel = ub_bytes if a.no_lz4 else compress_lz4(ub_bytes)
    img = boot_image_v2(kernel, ramdisk, dtb)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_bytes(img)
    print(f"{a.output}: {len(img)} bytes (kernel={len(kernel)}, ramdisk={len(ramdisk)}, dtb={len(dtb)})")


if __name__ == "__main__":
    main()
