# ios-sweet on Pixel 3a (sargo)

**Apple's iOS 16 running on a Google Pixel 3a.**

This is a port of Apple's open-source XNU kernel (xnu-8796, the iOS 16
generation) to the Google Pixel 3a (`sargo`, Qualcomm SDM670 / Snapdragon
670). On top of it runs the **unmodified iOS 16.7.16 (20H392) userland of
the iPhone X**: launchd, the system daemons, backboardd, SpringBoard and
the stock apps.

It is built on [ios-sweet](https://github.com/frokfrdk/ios-sweet), the
iOS 16 port to the Xiaomi Redmi Note 10 Pro (`sweet`). The sweet port
boots to the iOS home screen. This fork reuses its kernel, its XNU
patches, and its iOS root image, and adds the drivers and device tree
for sargo.

> Experimental bring-up, not a usable phone OS. The kernel boots on
> the phone, but the storage driver is still being brought up, so iOS
> does not reach the lock screen yet. It installs into the `userdata`
> partition and so **erases Android's user data**.

## Current status

As of **2026-11-07** the phone boots XNU. The kernel reaches the root
device lookup and calls the sargo SDHCI driver. The driver is being
brought up step by step: the command path works (CMD0 through CMD16),
and the current work is the CMD17 data path, reading the first block
of the disk.

| Area | State |
| --- | --- |
| Boot | Stock Google bootloader → `fastboot boot` of our shim → XNU. Nothing is flashed to the boot partitions |
| CPU | Kryo 360 cores via PSCI, GICv3 interrupts, 19.2 MHz timer. The CPU driver match was fixed by adding `qcom,kryo470` to the ADT (commit 4772f92) |
| Memory | Two DDR banks: 2 GiB at 0x80000000 + ~1.97 GiB at 0x100000000 |
| Storage | Own SDHCI/eMMC host driver (patch 0082); the iOS root is a journaled HFS+ volume in `userdata`. In progress: the CMD17 data path |
| iOS userland | Real iPhone X 16.7.16 root filesystem and shared cache, flashed to `userdata`. Not yet reached (blocked on the storage driver) |
| Display | Kernel framebuffer console at 1080×2220, stride 4320. The panel is a Samsung S6E3FA7 in command mode |
| Device identity | iPhone X (`iPhone10,3` / `D22AP`): no Home button, gesture UI |

## What does not work (yet)

- **Storage reads.** The SDHCI driver runs and the command path
  works, but the data path (CMD17 block reads) is still being fixed.
  Until it reads, iOS cannot mount its root and cannot start.
- **Touch, buttons, battery, Wi-Fi, Bluetooth, cellular, audio,
  camera, sensors, GPS, SEP / Face ID.** None of these have sargo
  drivers yet. Touch is a rewrite: the sargo panel uses a Synaptics
  S3706, not the sweet Goodix part.
- **No GPU.** When iOS does run, everything will be drawn by the
  CPU, as on sweet.
- **Security is off:** permissive code signing (AMFI) and sandbox
  policies, a stub AppleKeyStore, no data protection. Do not put
  anything personal on it.

## How it boots

```
ABL (stock Google bootloader, unlocked)
  -> fastboot boot shim-boot.img              (temporary; nothing is flashed to boot)
     -> shim: own MMU setup, reads the live FDT, loads the payload from the ramdisk
        -> XNU Mach-O + generated Apple Device Tree (ADT) + boot-args
           -> XNU -> SDHCI driver -> HFS+ root in the userdata partition
              -> dyld + shared cache -> launchd -> daemons -> backboardd + SpringBoard
```

There is no serial port. The kernel draws its console into the
framebuffer that ABL leaves at `0x9c000000`. Panics are drawn on
screen; read them from a photo.

Google's ABL needs three shim fixes that sweet did not: the kernel
must be LZ4-compressed (`lz4 -9`, frame flags that ABL accepts),
`text_offset` is 0x80000, and `os_version` is 15.0.0 with
`os_patch_level` 2025-11.

## Hardware

| Part | Detail |
| --- | --- |
| SoC | Snapdragon 670 (SDM670), Kryo 360 (6 Silver + 2 Gold), PSCI 1.0 via SMC |
| RAM | 2 GiB at 0x80000000 + ~1.97 GiB at 0x100000000 |
| Storage | eMMC 5.1, 64 GB. SDHCI `sdhc_1` at 0x7c4000, CQHCI at 0x7c5000. Not UFS |
| Panel | Samsung S6E3FA7, 1080×2220 @ 60 Hz, command mode, DSI0 at 0xae94000 |
| Touch | Synaptics S3706 (RMI4), I2C 0x20 on i2c9 at 0xa84000 |
| PMIC | PM660 + PM660L, SPMI arbiter at 0xc440000 |
| GIC | GICv3 at 0x17a00000 / 0x17a60000, same as sweet |
| Timer | 19.2 MHz, virtual timer PPI 19 |
| IMEM restart reason | 0x146bf65c (sweet's 0x146aa65c is wrong for sargo) |

The live device tree (from the running phone) is authoritative. A
full hardware table is in the project notes.

## How to do it yourself

You need a Pixel 3a (`sargo`) with an **unlocked bootloader**, a
Linux PC, and a Codemagic account for the kernel build. **Everything
on `userdata` is lost.** Bootloaders, recovery, modem and persist
partitions are never touched.

### 0. Back up the phone

Boot a recovery with adb root and pull every partition by name except
`userdata`. They contain per-device calibration data. Keep them off
git; `.gitignore` already excludes `backup/`, `dtb/` and `recovery/`.

The shim needs the stock DTB. Unpack your phone's stock `boot.img`:

```sh
python3 tools/unpack_bootv2.py backup/boot.img dtb/boot_unpacked   # -> dtb/boot_unpacked/dtb
```

### 1. Build the kernel

The kernel builds on Codemagic (free tier, 500 Mac minutes a month).
The workflow fetches xnu-8796, applies `xnu/patches/` in order, and
collects the kernel. The `codemagic.yaml` at the repo root defines
it (workflow `xnu-sargo`, branch `sargo`). Start a build from the
Codemagic dashboard or its API, then download the artifacts zip.

GitHub Actions is not used for this branch; its macOS runners were
unreliable (builds cancelled in queue, zero steps).

To check a patch locally before spending a build, `tools/xnu-cc.py`
replays the compile commands of a previous CI log for single files.

### 2. Build the iOS root image

Download the iPhone X (iPhone10,3) iOS 16.7.16 (20H392) IPSW from
Apple. Then, on Linux, with `apfs-fuse`, `hfsprogs` and sudo:

```sh
# OS, SystemOS and AppOS disk images from the IPSW (see BuildManifest.plist for their names)
tools/mkiosroot.sh OS.dmg SystemOS.dmg AppOS.dmg ios16-root.img 16G
sudo fsck.hfsplus -fy ios16-root.img
sudo mount -t hfsplus -o loop,force ios16-root.img /mnt/iosroot
sudo python3 tools/sweet-root-tweaks.py /mnt/iosroot
sudo umount /mnt/iosroot
img2simg ios16-root.img ios16-root.simg
```

`mkiosroot.sh` copies the OS volume and both cryptexes into one
case-sensitive journaled HFS+ volume. It adds the dyld cache links,
the plain system keybag and the log directories the daemons expect.
`sweet-root-tweaks.py` applies the bring-up changes: it skips Setup,
seeds the activation state, removes the baseband daemon, disables
the watchdogs that kill slow apps and the crash-looping media paths,
and so on. The script's header explains each change.

The device tree builder copies `/product` from the real iPhone X
device tree, which MobileGestalt needs. Extract it once, without
downloading the whole IPSW:

```sh
python3 tools/remotezip.py IPSW_URL DeviceTree.d22ap.im4p research/ipsw-x/dt
ipsw img4 im4p extract research/ipsw-x/dt/Firmware/all_flash/DeviceTree.d22ap.im4p -o research/ipsw-x/d22ap.adt
```

### 3. Build the boot image

Needs `aarch64-linux-gnu-gcc` and `mkbootimg`.

```sh
python3 tools/mkadt-sargo.py out/sargo.adt --highmem
python3 tools/mkpayload.py xnu/out/kernel.development.sweet out/sargo.adt out/payload.bin \
    --cmdline 'debug=0xc serial=3 cpus=1 keepsyms=1 -enable_kprintf_spam rd=sdhciroot0'
(cd shim-sargo && ./build.sh ../out/payload.bin)   # -> shim-sargo/out/shim-boot.img
```

`mkadt-sargo.py` takes the output path as its first argument, with
flags after it. `--highmem` is the production setting (it uses the
upper RAM bank). The boot-args use `rd=sdhciroot0` for the sargo
SDHCI root device, and `cpus=1` is the safe fallback.

### 4. Flash the root and boot

```sh
fastboot -S 256M flash userdata ios16-root.simg   # ~7 minutes; do not unplug
fastboot boot shim-sargo/out/shim-boot.img
```

The phone's `max-download-size` is 256 MB, so `-S 256M` is required.
After a panic, force the phone off by holding Power until dark, then
Power + Volume down for fastboot. If the phone lands in fastbootd
(userspace fastboot), `fastboot boot` fails; fix it with `fastboot
reboot bootloader` first.

### QEMU

`tools/qemu-sweet.py` boots the same kernel on QEMU `virt` with a
test RAM-disk root. It is useful for kernel-only changes; see
`NOTES.md`.

## Repository layout

| Path | What |
| --- | --- |
| `shim-sargo/` | Bare-metal boot shim for sargo (C + asm) packed as an Android boot.img v2. Google-ABL fixes: LZ4 kernel, text_offset 0x80000, os_version 15.0.0 |
| `shim/` | Bare-metal boot shim for sweet (the upstream one) |
| `xnu/patches/` | The XNU patch series (applied in order by CI), plus staging scripts for libpthread, HFS and BearSSL |
| `xnu/patches/0082-sargo-sdhci-root-device.patch` | The sargo SDHCI/eMMC root device driver (PIO CMD17 reads, GPT scan, `sdhciroot0`) |
| `codemagic.yaml` | Kernel CI build (Codemagic, workflow `xnu-sargo`) |
| `.github/workflows/xnu.yml` | Kernel CI build (GitHub Actions, sweet; not used for sargo) |
| `tools/mkadt-sargo.py` | Generates the Apple Device Tree from sargo's MMIO layout and the iPhone X identity |
| `tools/mkadt.py` | Generates the Apple Device Tree for sweet |
| `tools/mkpayload.py` | Packs kernel + ADT + boot-args into the shim payload |
| `tools/mkiosroot.sh` | Builds the HFS+ iOS root from the IPSW images |
| `tools/sweet-root-tweaks.py` | Bring-up changes to the root (Setup skip, activation, daemons) |
| `tools/sweet-log-pull.sh`, `sweet-log.py` | Read the persistent kernel log from recovery (sweet) |
| `tools/sweet-crash.py` | Symbolicate console crash reports |
| `tools/xnu-cc.py` | Local single-file compile check from a CI log |
| `tools/qemu-sweet.py` | QEMU test boots with framebuffer text decoding |
| `tools/adt.py`, `remotezip.py`, `unpack_bootv2.py`, `sparsediff.py` | Helpers |
| `NOTES.md` | Detailed engineering log: every boot, crash and decision, plus hardware facts |

Not in git: partition backups, live device trees, recovery images,
Apple firmware and root images, and downloaded or generated build
trees. They contain device serials and calibration data, or Apple's
copyrighted files.

## Kernel patches

| # | What |
| --- | --- |
| 0001–0009 | Toolchain compatibility, SWEET machine config, non-PAC arm64, stubs for Apple-internal pieces |
| 0010–0018 | Early framebuffer console, physmap fix, PRNG without corecrypto, device-tree platform expert, timer PPI, boot CPU driver |
| 0019–0020 | Apple open-source libpthread kernel provider; BearSSL AES |
| 0021–0027 | RAM-disk root, mockfs, SHA-256, minimal AMFI, Apple open-source HFS+, MD5 |
| 0028–0034 | UFS host driver and root device (sweet), read/write root, DMA above 4 GiB |
| 0035–0045 | Entitlements, bindfs, AppleKeyStore stub, IOWatchdog, console crash reports |
| 0046 | IOSurface (IOCoreSurfaceRoot) and IOMobileFramebuffer (AppleCLCD) services |
| 0047–0055 | Diagnostics: persistent log, /dev/null tee, IOKit trace, heap strings and user stack sampler on crash; permissive Sandbox policy; HFS rename-swap; AppleKeyStore selectors |
| 0056–0057 | Throttled panel copy; no external identity resolver |
| 0058 | HID event services. Apple's IOHIDFamily-1915.140.3 user client and event queue, matching the iOS 16.7 userland |
| 0059 | iPhone X identity (`hw.product` / `hw.target` from the device tree) |
| 0060 | Power and volume keys over SPMI; touch over GENI I²C (sweet) |
| 0061 | Present the display as 1125×2436 (sweet) |
| 0062 | RPMh regulator vote for the touch rail; battery power source (sweet) |
| 0063 | GENI FIFO packing fix and the Goodix GT9896 protocol (sweet) |
| 0064 | Grant PRIV_WORK_INTERVAL (UIKit's CoreAnimation work interval) |
| 0065 | SMP: secondary A55 cores via PSCI CPU_ON |
| 0066–0067 | Fault-region crash info; console mute |
| 0068 | IOSurface lookup semantics for already-mapped surfaces |
| 0069 | Batched persistent-log flushes |
| 0082 | Sargo SDHCI/eMMC root device: PIO CMD17 single-block reads, GPT scan, `sdhciroot0`/`rsdhciroot0`, root via `rd=sdhciroot0` |

## Credits

Built on Apple's open-source releases (xnu, IOHIDFamily, libpthread,
hfs), [BearSSL](https://bearssl.org),
[blacktop/darwin-xnu-build](https://github.com/blacktop/darwin-xnu-build),
[ipsw](https://github.com/blacktop/ipsw), Qualcomm/Google's published
Linux kernel sources for the hardware details, and
[OrangeFox Recovery](https://orangefox.download). iOS itself is
Apple's and is not distributed here.

The sweet port this fork builds on is
[ios-sweet](https://github.com/frokfrdk/ios-sweet) by frokfrdk.
