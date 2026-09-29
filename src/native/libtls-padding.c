/* libtls-padding.c — first-in-LD_PRELOAD TLS padding for glibc processes that load bionic code through
 * libhybris (2026-09-27).
 *
 * WHY: bionic arm64 code addresses fixed thread-pointer slots — the Mali blob reads the stack-protector
 * canary at tp+0x28 (25,959 sites), reads the GL slot at tp+0x18 (655 sites) and ZEROES it (3 sites,
 * `str xzr, [tp, #0x18]`). In a glibc process (ELF TLS variant 1: 16-byte TCB at tp, the first TLS module
 * right after it) those addresses belong to the first module's thread-local variables: libc's in a small C
 * program (ctype table pointers -> libepoxy saw GL_VERSION "enGL ES 3.2" and aborted), libpython's thread
 * state in Python (_PyThreadState_Attach fatal error). An LD_PRELOADed library is the first TLS module
 * whenever the executable has no TLS of its own (true for gnome-shell, python3, nautilus, the GNOME apps;
 * NOT for firefox), so this zero-filled block turns tp+16 .. tp+16+sizeof into dead space.
 * Same idea as the "tls-padding" preload used on Halium/Droidian arm64. Verified on the device with
 * tlsprobe (prints the block's offset from the thread pointer). */
__attribute__((visibility("default"), used, aligned(16)))
__thread unsigned char hybris_tls_padding[256];

/* Keeps a reference so the block is never discarded; also lets tlsprobe find it. */
__attribute__((visibility("default")))
void *hybris_tls_padding_address(void) {
	return hybris_tls_padding;
}
