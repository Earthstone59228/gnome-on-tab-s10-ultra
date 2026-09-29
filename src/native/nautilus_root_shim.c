/*
 * nautilus_root_shim.c — let Nautilus start inside this chroot.
 *
 * Why: nautilus (50.3, the deployed Fedora 44 build) hard-exits in main()
 * when getuid() == 0 (src/nautilus-main.c: "Running as root is not
 * supported. Consider running `nautilus admin:///` instead." followed by
 * exit(ENOTSUP)). The suggested admin:/// escape hatch is for NON-root
 * users (gvfs + polkit); this project's entire session runs as uid 0, so
 * the guard fires unconditionally and the process dies before any window —
 * the observed "hung on loading, shows nothing" symptom (2026-09-20).
 *
 * Fix: LD_PRELOAD interposer that reports uid/gid 1000 (and the matching
 * euid variants) so the guard passes. File access is untouched — the real
 * uid stays 0, so no permission loss anywhere. GLib caches nothing user
 * -visible off these calls at startup that we don't also cover: passwd
 * lookups still return root's home (/root), which exists.
 *
 * Scope: preload ONLY for nautilus (see the .desktop override) — other
 * uid-0 processes must keep seeing the real uid.
 *
 * Builds via driver-build/01 recipe (clang aarch64 + DDK ld.lld, glibc
 * sysroot). Linked as a shared object against glibc only.
 */
#define _GNU_SOURCE
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/syscall.h>

/* 2026-09-26 audit #3: the preload used to be inherited by everything Nautilus launches
 * directly (open-with, scripts, terminals), so those root processes also believed they were
 * uid 1000. Drop our own entry from LD_PRELOAD once we are loaded; Nautilus itself keeps the
 * shim (already mapped), its children no longer get it. */
__attribute__((constructor)) static void drop_self_from_ld_preload(void) {
	const char *v = getenv("LD_PRELOAD");
	if (v == NULL || strstr(v, "nautilus_root_shim") == NULL) {
		return;
	}
	char buf[4096];
	size_t n = 0;
	const char *p = v;
	while (*p != '\0') {
		size_t len = strcspn(p, ": ");
		if (len > 0 && !(len >= 18 && memmem(p, len, "nautilus_root_shim", 18) != NULL)) {
			if (n > 0 && n < sizeof(buf) - 1) {
				buf[n++] = ':';
			}
			if (n + len >= sizeof(buf)) {
				return; /* absurdly long: leave it alone */
			}
			memcpy(buf + n, p, len);
			n += len;
		}
		p += len;
		if (*p != '\0') {
			p++;
		}
	}
	buf[n] = '\0';
	if (n == 0) {
		unsetenv("LD_PRELOAD");
	} else {
		setenv("LD_PRELOAD", buf, 1);
	}
}

uid_t getuid(void)  { return 1000; }
uid_t geteuid(void) { return 1000; }
gid_t getgid(void)  { return 1000; }
gid_t getegid(void) { return 1000; }
