# U-Boot / UEFI profile for Google Pixel 3a (`sargo`)

Upstream Das U-Boot (`v2026.07`) builds for `sargo` using the generic Qualcomm config (`qcom_defconfig`) plus mobile phone fragment (`qcom-phone.config`) and upstream `qcom/sdm670-google-sargo.dts`.

## Build steps (Linux/macOS)

Because `hakios-uefi/u-boot/build.sh` runs a single `make $UBOOT_DEFCONFIG` target, merge the Qualcomm defconfig, `qcom-phone.config`, and `hakios_sargo.config` into a single defconfig or run:

```sh
source ./env.sh
export HAKIOS_PROFILE_ROOT=/path/to/hakios-sargo/uefi-profiles
u-boot/apply.sh google/sargo

# Configure with qcom_defconfig + qcom-phone.config + hakios_sargo.config:
make -C build/u-boot-sargo-src O="$PWD/build/u-boot-sargo" \
  CROSS_COMPILE=aarch64-linux-gnu- \
  qcom_defconfig qcom-phone.config hakios_sargo.config
make -C build/u-boot-sargo-src O="$PWD/build/u-boot-sargo" \
  CROSS_COMPILE=aarch64-linux-gnu- -j"$(nproc)"
```

## Packing for Google Pixel 3a ABL

Pixel 3a's stock Android Bootloader (ABL) rejects raw `Image` kernels and requires a `boot.img` v2 image with 4K pages, `os_version 15.0.0`, `os_patch_level 2025-11`, and an LZ4-compressed U-Boot image (verified on hardware in `sargo-repo`). Use `tools/pack_sargo_boot.py` from this repository:

```sh
python3 tools/pack_sargo_boot.py \
  --u-boot build/u-boot-sargo/u-boot.bin \
  --loader "$KERNEL_ROOT/arch/arm64/boot/loader.efi" \
  --dtb build/u-boot-sargo/dts/upstream/src/arm64/qcom/sdm670-google-sargo.dtb \
  --payload /path/to/sargo.xnup \
  -o build/hakios-sargo-boot.img

fastboot boot build/hakios-sargo-boot.img
```
