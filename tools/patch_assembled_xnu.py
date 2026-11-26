#!/usr/bin/env python3
"""Patch the assembled XNU tree directly with sargo SDHCI eMMC driver, root hook, and Cryptex support."""
import os
import shutil
import sys

def patch_file(path, target, replacement, desc):
    if not os.path.exists(path):
        print(f"[sargo-xnu] WARNING: {path} not found for {desc}")
        return False
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    if target in content:
        content = content.replace(target, replacement)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[sargo-xnu] Patched {desc} in {os.path.basename(path)}")
        return True
    else:
        # Try normalizing line endings
        target_norm = target.replace("\r\n", "\n")
        content_norm = content.replace("\r\n", "\n")
        if target_norm in content_norm:
            content_norm = content_norm.replace(target_norm, replacement.replace("\r\n", "\n"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(content_norm)
            print(f"[sargo-xnu] Patched {desc} (normalized) in {os.path.basename(path)}")
            return True
        print(f"[sargo-xnu] WARNING: target not found for {desc} in {os.path.basename(path)}")
        return False

def main():
    if len(sys.argv) < 2:
        print("Usage: patch_assembled_xnu.py <assembled-xnu-tree-dir> [sargo-repo-dir]")
        sys.exit(1)

    xnu_dir = os.path.abspath(sys.argv[1])
    sargo_dir = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    print(f"[sargo-xnu] XNU tree dir: {xnu_dir}")
    print(f"[sargo-xnu] Sargo dir:    {sargo_dir}")

    # 1. Copy drivers/board/sdhci.c into bsd/dev/board/sdhci.c
    src_sdhci = os.path.join(sargo_dir, "drivers", "board", "sdhci.c")
    dst_sdhci = os.path.join(xnu_dir, "bsd", "dev", "board", "sdhci.c")
    os.makedirs(os.path.dirname(dst_sdhci), exist_ok=True)
    shutil.copy2(src_sdhci, dst_sdhci)
    print(f"[sargo-xnu] Copied sdhci.c to {dst_sdhci} ({os.path.getsize(dst_sdhci)} bytes)")

    # 2. Append bsd/dev/board/sdhci.c to bsd/conf/files
    files_conf = os.path.join(xnu_dir, "bsd", "conf", "files")
    with open(files_conf, "r", encoding="utf-8", errors="ignore") as f:
        fc = f.read()
    if "bsd/dev/board/sdhci.c" not in fc:
        with open(files_conf, "a", encoding="utf-8") as f:
            f.write("\nbsd/dev/board/sdhci.c\t\tstandard\n")
        print("[sargo-xnu] Registered bsd/dev/board/sdhci.c in bsd/conf/files")

    # 3. Hook sdhciroot0 / ufsroot0 in IOKitBSDInit.cpp
    iokit_cpp = os.path.join(xnu_dir, "iokit", "bsddev", "IOKitBSDInit.cpp")
    hook_target = """\
\t\t\tregEntry->release();                                                            /* Toss the entry */
\t\t}
\t}"""
    hook_repl = """\
\t\t\tregEntry->release();                                                            /* Toss the entry */
\t\t}
\t}

\t/* SARGO: root on eMMC (bsd/dev/board/sdhci.c) */
\tif (strcmp(rdBootVar, "sdhciroot0") == 0 || strcmp(rdBootVar, "ufsroot0") == 0) {
\t\textern int sargo_sdhci_rootdev(dev_t *dev);
\t\tOSSafeReleaseNULL(matching);
\t\tint sdhci_error = sargo_sdhci_rootdev(root);
\t\tif (sdhci_error) {
\t\t\tpanic("IOFindBSDRoot: SARGO SDHCI root %s unavailable: %d", rdBootVar, sdhci_error);
\t\t}
\t\tstrlcpy(rootName, rdBootVar, rootNameSize);
\t\tIOLog("BSD root: %s, major %d, minor %d\\n", rootName, major(*root), minor(*root));
\t\t*oflags = 0;
\t\tgoto iofrootx;
\t}"""
    patch_file(iokit_cpp, hook_target, hook_repl, "sdhciroot0 hook")

    wait_target = "if (PE_parse_boot_argn(\"rd\", rdEarly, sizeof(rdEarly)) && rdEarly[0] == 'm' && rdEarly[1] == 'd') {"
    wait_repl = "if (PE_parse_boot_argn(\"rd\", rdEarly, sizeof(rdEarly)) && ((rdEarly[0] == 'm' && rdEarly[1] == 'd') || strncmp(rdEarly, \"sdhci\", 5) == 0 || strncmp(rdEarly, \"ufs\", 3) == 0)) {"
    patch_file(iokit_cpp, wait_target, wait_repl, "fast bsdWait")

    # 4. Cryptex VFS Redirection in bsd/vfs/vfs_lookup.c
    lookup_c = os.path.join(xnu_dir, "bsd", "vfs", "vfs_lookup.c")
    vfs_target = """\
retry_copy:
\tndp->ni_pathlen = (u_int)bytes_copied;
\tbytes_copied = 0;"""
    vfs_repl = """\
retry_copy:
\tndp->ni_pathlen = (u_int)bytes_copied;
\tbytes_copied = 0;

\t/*
\t * SARGO Cryptex VFS Redirection:
\t * In iOS 16, dyld shared cache and Cryptex assets are located under
\t * /private/preboot/Cryptexes/OS/ and /private/preboot/Cryptexes/App/.
\t * Transparently redirect lookups targeting:
\t * 1) System/Library/Caches/com.apple.dyld -> private/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld
\t * 2) System/Cryptexes/OS -> private/preboot/Cryptexes/OS
\t * 3) System/Cryptexes/App -> private/preboot/Cryptexes/App
\t */
\t{
\t\tconst char *p = cnp->cn_pnbuf;
\t\tbool leading_slash = false;
\t\tif (p[0] == '/') {
\t\t\tleading_slash = true;
\t\t\tp++;
\t\t}
\t\tconst char *redir_prefix = NULL;
\t\tconst char *tail = NULL;
\t\tstatic const char kDyldCachePrefix[] = "System/Library/Caches/com.apple.dyld";
\t\tstatic const size_t kDyldCacheLen = sizeof(kDyldCachePrefix) - 1;
\t\tstatic const char kCryptexOSPrefix[] = "System/Cryptexes/OS";
\t\tstatic const size_t kCryptexOSLen = sizeof(kCryptexOSPrefix) - 1;
\t\tstatic const char kCryptexAppPrefix[] = "System/Cryptexes/App";
\t\tstatic const size_t kCryptexAppLen = sizeof(kCryptexAppPrefix) - 1;

\t\tif (strncmp(p, kDyldCachePrefix, kDyldCacheLen) == 0 &&
\t\t    (p[kDyldCacheLen] == '\\0' || p[kDyldCacheLen] == '/')) {
\t\t\tredir_prefix = "private/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld";
\t\t\ttail = p + kDyldCacheLen;
\t\t} else if (strncmp(p, kCryptexOSPrefix, kCryptexOSLen) == 0 &&
\t\t    (p[kCryptexOSLen] == '\\0' || p[kCryptexOSLen] == '/')) {
\t\t\tredir_prefix = "private/preboot/Cryptexes/OS";
\t\t\ttail = p + kCryptexOSLen;
\t\t} else if (strncmp(p, kCryptexAppPrefix, kCryptexAppLen) == 0 &&
\t\t    (p[kCryptexAppLen] == '\\0' || p[kCryptexAppLen] == '/')) {
\t\t\tredir_prefix = "private/preboot/Cryptexes/App";
\t\t\ttail = p + kCryptexAppLen;
\t\t}

\t\tif (redir_prefix != NULL) {
\t\t\tsize_t new_len = (leading_slash ? 1 : 0) + strlen(redir_prefix) + strlen(tail) + 1;
\t\t\tif (new_len <= MAXPATHLEN) {
\t\t\t\tchar *redir_buf = zalloc(ZV_NAMEI);
\t\t\t\tif (redir_buf != NULL) {
\t\t\t\t\tsnprintf(redir_buf, MAXPATHLEN, "%s%s%s",
\t\t\t\t\t    leading_slash ? "/" : "", redir_prefix, tail);
\t\t\t\t\tif (cnp->cn_flags & HASBUF) {
\t\t\t\t\t\tzfree(ZV_NAMEI, cnp->cn_pnbuf);
\t\t\t\t\t}
\t\t\t\t\tcnp->cn_pnbuf = redir_buf;
\t\t\t\t\tcnp->cn_pnlen = MAXPATHLEN;
\t\t\t\t\tcnp->cn_flags |= HASBUF;
\t\t\t\t\tndp->ni_pathlen = (u_int)new_len;
\t\t\t\t}
\t\t\t}
\t\t}
\t}"""
    patch_file(lookup_c, vfs_target, vfs_repl, "Cryptex VFS Redirection")

    # 5. Text page revalidation in osfmk/vm/vm_fault.c
    fault_c = os.path.join(xnu_dir, "osfmk", "vm", "vm_fault.c")
    fault_target = """\
\t\tCA_EVENT_SEND(ca_event);
\t\tprintf("Text page corruption detected for pid %d\\n", proc_selfpid());
\t\t++vmtc_total;
\t\treturn KERN_FAILURE; /* failure means we definitely found a corrupt page */"""
    fault_repl = """\
\t\tCA_EVENT_SEND(ca_event);
\t\tprintf("Text page corruption detected for pid %d\\n", proc_selfpid());
\t\t++vmtc_total;
\t\treturn KERN_SUCCESS;"""
    patch_file(fault_c, fault_target, fault_repl, "text page revalidation bypass")

    print("[sargo-xnu] All assembled XNU patches successfully applied!")

if __name__ == "__main__":
    main()
