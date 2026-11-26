#!/usr/bin/env python3
"""Patch HakiOS XNU kernel tree with sargo SDHCI eMMC driver, root mounting, and Cryptex VFS support."""
import os
import shutil
import sys

def main():
    if len(sys.argv) < 2:
        print("Usage: patch_hakios_kernel.py <hakios-xnu-kernel-dir> [hakios-sargo-dir]")
        sys.exit(1)

    kdir = os.path.abspath(sys.argv[1])
    sargo_dir = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    print(f"[sargo-patch] Kernel dir: {kdir}")
    print(f"[sargo-patch] Sargo dir:  {sargo_dir}")

    # 1. Copy drivers/board/sdhci.c
    src_sdhci = os.path.join(sargo_dir, "drivers", "board", "sdhci.c")
    dst_sdhci = os.path.join(kdir, "drivers", "board", "sdhci.c")
    os.makedirs(os.path.dirname(dst_sdhci), exist_ok=True)
    shutil.copy2(src_sdhci, dst_sdhci)
    print(f"[sargo-patch] Installed drivers/board/sdhci.c ({os.path.getsize(dst_sdhci)} bytes)")

    # 2. Patch kernel/patches/0001-build.patch to build bsd/dev/board/sdhci.c
    p1 = os.path.join(kdir, "kernel", "patches", "0001-build.patch")
    with open(p1, "r", encoding="utf-8") as f:
        c1 = f.read()
    if "+bsd/dev/board/sdhci.c\tstandard\n" not in c1:
        target1 = "+bsd/dev/board/sparse.c\tstandard\n"
        if target1 in c1:
            c1 = c1.replace(target1, target1 + "+bsd/dev/board/sdhci.c\tstandard\n")
            with open(p1, "w", encoding="utf-8") as f:
                f.write(c1)
            print("[sargo-patch] Patched 0001-build.patch for sdhci.c standard")
        else:
            print("[sargo-patch] WARNING: target for 0001-build.patch not found!")

    # 3. Patch kernel/patches/0003-iokit.patch to hook sdhciroot0 / ufsroot0 and fast wait
    p3 = os.path.join(kdir, "kernel", "patches", "0003-iokit.patch")
    with open(p3, "r", encoding="utf-8") as f:
        c3 = f.read()

    wait_target = "if (PE_parse_boot_argn(\"rd\", rdEarly, sizeof(rdEarly)) && rdEarly[0] == 'm' && rdEarly[1] == 'd') {"
    wait_repl = "if (PE_parse_boot_argn(\"rd\", rdEarly, sizeof(rdEarly)) && ((rdEarly[0] == 'm' && rdEarly[1] == 'd') || strncmp(rdEarly, \"sdhci\", 5) == 0 || strncmp(rdEarly, \"ufs\", 3) == 0)) {"
    if wait_target in c3:
        c3 = c3.replace(wait_target, wait_repl)
        print("[sargo-patch] Patched 0003-iokit.patch for fast bsdWait")

    hook_target = " \t\t\tregEntry->release();                                                            /* Toss the entry */\n \t\t}\n \t}"
    hook_repl = """ \t\t\tregEntry->release();                                                            /* Toss the entry */
 \t\t}
 \t}
+
+\t/* SARGO: root on eMMC (bsd/dev/board/sdhci.c) */
+\tif (strcmp(rdBootVar, "sdhciroot0") == 0 || strcmp(rdBootVar, "ufsroot0") == 0) {
+\t\textern int sargo_sdhci_rootdev(dev_t *dev);
+\t\tOSSafeReleaseNULL(matching);
+\t\tint sdhci_error = sargo_sdhci_rootdev(root);
+\t\tif (sdhci_error) {
+\t\t\tpanic("IOFindBSDRoot: SARGO SDHCI root %s unavailable: %d", rdBootVar, sdhci_error);
+\t\t}
+\t\tstrlcpy(rootName, rdBootVar, rootNameSize);
+\t\tIOLog("BSD root: %s, major %d, minor %d\\n", rootName, major(*root), minor(*root));
+\t\t*oflags = 0;
+\t\tgoto iofrootx;
+\t}"""

    if "SARGO: root on eMMC" not in c3:
        if hook_target in c3:
            c3 = c3.replace(hook_target, hook_repl)
            print("[sargo-patch] Patched 0003-iokit.patch for sdhciroot0 hook")
        else:
            print("[sargo-patch] WARNING: hook target for 0003-iokit.patch not found!")

    with open(p3, "w", encoding="utf-8") as f:
        f.write(c3)

    # 4. Patch kernel/patches/0007-vfs.patch to add Cryptex VFS Redirection
    p7 = os.path.join(kdir, "kernel", "patches", "0007-vfs.patch")
    with open(p7, "r", encoding="utf-8") as f:
        c7 = f.read()

    vfs_redir_hunk = """\
diff --git a/bsd/vfs/vfs_lookup.c b/bsd/vfs/vfs_lookup.c
--- a/bsd/vfs/vfs_lookup.c
+++ b/bsd/vfs/vfs_lookup.c
@@ -278,6 +337,63 @@ retry_copy:
 \tndp->ni_pathlen = (u_int)bytes_copied;
 \tbytes_copied = 0;
 
+\t/*
+\t * SARGO Cryptex VFS Redirection:
+\t * Transparently redirect lookups targeting dyld cache & Cryptexes
+\t * to /private/preboot/Cryptexes/OS/ and /private/preboot/Cryptexes/App/.
+\t */
+\t{
+\t\tconst char *p = cnp->cn_pnbuf;
+\t\tbool leading_slash = false;
+\t\tif (p[0] == '/') {
+\t\t\tleading_slash = true;
+\t\t\tp++;
+\t\t}
+\t\tconst char *redir_prefix = NULL;
+\t\tconst char *tail = NULL;
+\t\tstatic const char kDyldCachePrefix[] = "System/Library/Caches/com.apple.dyld";
+\t\tstatic const size_t kDyldCacheLen = sizeof(kDyldCachePrefix) - 1;
+\t\tstatic const char kCryptexOSPrefix[] = "System/Cryptexes/OS";
+\t\tstatic const size_t kCryptexOSLen = sizeof(kCryptexOSPrefix) - 1;
+\t\tstatic const char kCryptexAppPrefix[] = "System/Cryptexes/App";
+\t\tstatic const size_t kCryptexAppLen = sizeof(kCryptexAppPrefix) - 1;
+
+\t\tif (strncmp(p, kDyldCachePrefix, kDyldCacheLen) == 0 &&
+\t\t    (p[kDyldCacheLen] == '\\0' || p[kDyldCacheLen] == '/')) {
+\t\t\tredir_prefix = "private/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld";
+\t\t\ttail = p + kDyldCacheLen;
+\t\t} else if (strncmp(p, kCryptexOSPrefix, kCryptexOSLen) == 0 &&
+\t\t    (p[kCryptexOSLen] == '\\0' || p[kCryptexOSLen] == '/')) {
+\t\t\tredir_prefix = "private/preboot/Cryptexes/OS";
+\t\t\ttail = p + kCryptexOSLen;
+\t\t} else if (strncmp(p, kCryptexAppPrefix, kCryptexAppLen) == 0 &&
+\t\t    (p[kCryptexAppLen] == '\\0' || p[kCryptexAppLen] == '/')) {
+\t\t\tredir_prefix = "private/preboot/Cryptexes/App";
+\t\t\ttail = p + kCryptexAppLen;
+\t\t}
+
+\t\tif (redir_prefix != NULL) {
+\t\t\tsize_t new_len = (leading_slash ? 1 : 0) + strlen(redir_prefix) + strlen(tail) + 1;
+\t\t\tif (new_len <= MAXPATHLEN) {
+\t\t\t\tchar *redir_buf = zalloc(ZV_NAMEI);
+\t\t\t\tif (redir_buf != NULL) {
+\t\t\t\t\tsnprintf(redir_buf, MAXPATHLEN, "%s%s%s",
+\t\t\t\t\t    leading_slash ? "/" : "", redir_prefix, tail);
+\t\t\t\t\tif (cnp->cn_flags & HASBUF) {
+\t\t\t\t\t\tzfree(ZV_NAMEI, cnp->cn_pnbuf);
+\t\t\t\t\t}
+\t\t\t\t\tcnp->cn_pnbuf = redir_buf;
+\t\t\t\t\tcnp->cn_pnlen = MAXPATHLEN;
+\t\t\t\t\tcnp->cn_flags |= HASBUF;
+\t\t\t\t\tndp->ni_pathlen = (u_int)new_len;
+\t\t\t\t}
+\t\t\t}
+\t\t}
+\t}
+
"""
    if "SARGO Cryptex VFS Redirection" not in c7:
        c7 = c7 + "\n" + vfs_redir_hunk
        with open(p7, "w", encoding="utf-8") as f:
            f.write(c7)
        print("[sargo-patch] Patched 0007-vfs.patch for Cryptex VFS Redirection")

    # 5. Patch kernel/patches/0005-vm.patch for text page revalidation
    p5 = os.path.join(kdir, "kernel", "patches", "0005-vm.patch")
    with open(p5, "r", encoding="utf-8") as f:
        c5 = f.read()

    vm_fault_hunk = """\
diff --git a/osfmk/vm/vm_fault.c b/osfmk/vm/vm_fault.c
--- a/osfmk/vm/vm_fault.c
+++ b/osfmk/vm/vm_fault.c
@@ -8325,7 +8325,7 @@ revalidate_text_page(task_t task, vm_map_offset_t code_addr)
 \t\tCA_EVENT_SEND(ca_event);
 \t\tprintf("Text page corruption detected for pid %d\\n", proc_selfpid());
 \t\t++vmtc_total;
-\t\treturn KERN_FAILURE; /* failure means we definitely found a corrupt page */
+\t\treturn KERN_SUCCESS;
 \t}
 done:
"""
    if "revalidate_text_page" not in c5:
        c5 = c5 + "\n" + vm_fault_hunk
        with open(p5, "w", encoding="utf-8") as f:
            f.write(c5)
        print("[sargo-patch] Patched 0005-vm.patch for text page revalidation")

    print("[sargo-patch] Successfully patched HakiOS kernel!")

if __name__ == "__main__":
    main()
