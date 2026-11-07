#!/usr/bin/env python3
"""Build a bootable Android boot.img v2 for Pixel 3a (sargo) with XNU kernel and shim."""

import argparse
import hashlib
import os
import struct
import subprocess
import sys

DEFAULT_BOOT_ARGS = (
    "debug=0xc serial=3 cpus=1 keepsyms=1 -enable_kprintf_spam -noprogress "
    "rd=sdhciroot0 sdhciroot=userdata unentitled_ios_sim_launch=1 ignition_level=1 "
    "wdt_test=no_user_checkin rootrw=1 launchd_unsecure_cache=1 wdt=-1"
)

def pack_boot_img_v2(kernel_data, ramdisk_data, dtb_data, out_path, cmdline="console=null"):
    ps = 4096
    rnd = lambda n: (n + ps - 1) // ps * ps

    ks = len(kernel_data)
    rs = len(ramdisk_data)
    ds = len(dtb_data)

    header = bytearray(ps)
    header[0:8] = b"ANDROID!"

    osv = 0x1e00019b
    struct.pack_into("<10I", header, 8, ks, 0x8000, rs, 0x1000000, 0, 0, 0x100, ps, 2, osv)

    cmd_bytes = cmdline.encode("ascii")[:512]
    header[64:64 + len(cmd_bytes)] = cmd_bytes

    h1 = hashlib.sha1()
    h1.update(kernel_data)
    h1.update(struct.pack("<I", ks))
    h1.update(ramdisk_data)
    h1.update(struct.pack("<I", rs))
    h1.update(b"")
    h1.update(struct.pack("<I", 0))
    h1.update(b"")
    h1.update(struct.pack("<I", 0))
    h1.update(dtb_data)
    h1.update(struct.pack("<I", ds))
    header[576:576 + 20] = h1.digest()

    struct.pack_into("<IQI", header, 1632, 0, 0, 1660)
    struct.pack_into("<IQ", header, 1648, ds, 0x1f00000)

    with open(out_path, "wb") as f:
        f.write(header)
        f.write(kernel_data)
        if rnd(ks) > ks:
            f.write(b"\0" * (rnd(ks) - ks))
        f.write(ramdisk_data)
        if rnd(rs) > rs:
            f.write(b"\0" * (rnd(rs) - rs))
        f.write(dtb_data)
        if rnd(ds) > ds:
            f.write(b"\0" * (rnd(ds) - ds))


def main():
    parser = argparse.ArgumentParser(description="Pack XNU kernel into sargo boot image")
    parser.add_argument("kernel", nargs="?", help="Path to kernel.development.sweet")
    parser.add_argument("--out", "-o", default="out/shim-boot.img", help="Output boot.img path")
    parser.add_argument("--cmdline", default=DEFAULT_BOOT_ARGS, help="XNU boot arguments")
    parser.add_argument("--adt", default="out/sargo.adt", help="Path to output ADT")
    parser.add_argument("--template", help="Path to reference sargo-shim-boot.img template")
    args = parser.parse_args()

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(repo_root)

    kernel_path = args.kernel
    if not kernel_path:
        candidates = [
            os.path.join(repo_root, "xnu/out/kernel.development.sweet"),
            os.path.expanduser("~/Downloads/kernel.development.sweet"),
            os.path.expanduser("~/Downloads/kernel"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                kernel_path = c
                break

    if not kernel_path or not os.path.isfile(kernel_path):
        print("Error: Kernel Mach-O not found.", file=sys.stderr)
        print("Please provide the path to kernel.development.sweet downloaded from Codemagic.", file=sys.stderr)
        print(f"Usage: {sys.argv[0]} <path_to_kernel.development.sweet>", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Using kernel: {kernel_path}")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.adt)), exist_ok=True)

    print("[*] Generating sargo Apple Device Tree (ADT)...")
    subprocess.check_call([sys.executable, "tools/mkadt-sargo.py", args.adt, "--highmem"])

    payload_bin = "out/payload.bin"
    print(f"[*] Packing XNU payload with boot-args:\n    {args.cmdline}")
    subprocess.check_call([
        sys.executable, "tools/mkpayload.py", kernel_path, args.adt, payload_bin,
        "--cmdline", args.cmdline
    ])

    shim_kernel_path = os.path.join(repo_root, "shim-sargo/template/shim_kernel.lz4")
    dtb_path = os.path.join(repo_root, "shim-sargo/template/sargo.dtb")
    if not os.path.isfile(shim_kernel_path) or not os.path.isfile(dtb_path):
        ref = args.template or os.path.expanduser("~/Downloads/sargo-shim-boot-sdhci.img")
        if ref and os.path.isfile(ref):
            print(f"[*] Extracting template from {ref}...")
            os.makedirs("shim-sargo/template", exist_ok=True)
            subprocess.check_call([sys.executable, "tools/unpack_bootv2.py", ref, "shim-sargo/template"])
            shim_kernel_path = "shim-sargo/template/kernel"
            dtb_path = "shim-sargo/template/dtb"

    with open(shim_kernel_path, "rb") as f:
        k_data = f.read()
    with open(payload_bin, "rb") as f:
        r_data = f.read()
    with open(dtb_path, "rb") as f:
        d_data = f.read()

    print(f"[*] Assembling Android boot image v2: {args.out}")
    pack_boot_img_v2(k_data, r_data, d_data, args.out)

    size = os.path.getsize(args.out)
    print(f"[+] Successfully generated {args.out} ({size:,} bytes)")
    print("\nNext step: Boot the phone with:")
    print(f"    fastboot boot {args.out}")


if __name__ == "__main__":
    main()
