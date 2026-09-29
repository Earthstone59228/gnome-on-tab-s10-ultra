/*
 * liboutline-atomics-shim.c — aarch64 LSE outline-atomics helpers as a
 * standalone shared library.
 *
 * Why: libhybris's vendored bionic-linker plugins (mm/n/o/q.so) are compiled
 * with -moutline-atomics and carry UND refs to __aarch64_{cas,ldadd,swp}_*
 * helpers. These are normally exported by libgcc_s.so.1 — but Fedora's
 * aarch64 libgcc_s does not export any __aarch64_* symbols, so glibc's
 * dlopen(q.so, RTLD_NOW) fails with "undefined symbol: __aarch64_swp8_acq_rel"
 * (the first UND it tries to bind). This shim provides the exact set needed
 * by the staged linker plugins, with correct acq_rel semantics, implemented
 * via the compiler's inline __atomic builtins (LL/SC on baseline armv8-a).
 * Build with -mno-outline-atomics so the shim never references itself.
 *
 * Deploy: LD_PRELOAD this into any process that loads a hybris linker plugin
 * (glibc binds UND symbols from the global scope, which LD_PRELOAD populates).
 */

typedef unsigned int  u32;
typedef unsigned long u64;

/* 2026-09-26 audit #3 FIX: the libgcc/compiler-rt ABI is cas(expected, desired, ptr) and
 * returns the value found at *ptr (store happens only if it equalled `expected`). Verified
 * by compiling __atomic_compare_exchange_n with clang -moutline-atomics for aarch64: w0 =
 * expected, w1 = desired, x2 = ptr. The previous version took (desired, ptr) — it used the
 * DESIRED value as the pointer and did an unconditional swap, i.e. any call would write to
 * a garbage address. Only the old /usr/local/lib/hybris linker plugins (o.so, q.so; the
 * no-client-gpu fallback stack) import this symbol. */
u32 __aarch64_cas4_acq_rel(u32 expected, u32 desired, volatile u32 *ptr)
{
	__atomic_compare_exchange_n(ptr, &expected, desired, 0 /* strong */,
				    __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE);
	return expected; /* on failure the builtin stored the current value here */
}

u32 __aarch64_ldadd4_acq_rel(u32 v, volatile u32 *ptr)
{
	return __atomic_fetch_add(ptr, v, __ATOMIC_ACQ_REL);
}

u32 __aarch64_swp4_acq_rel(u32 v, volatile u32 *ptr)
{
	return __atomic_exchange_n(ptr, v, __ATOMIC_ACQ_REL);
}

u64 __aarch64_swp8_acq_rel(u64 v, volatile u64 *ptr)
{
	return __atomic_exchange_n(ptr, v, __ATOMIC_ACQ_REL);
}
