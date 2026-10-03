/* dmaheap_shim.c — Item B (driver-build/02-display-session-stack.md).
 *
 * wlroots 0.19's built-in allocators (gbm, shm, drm-dumb, udmabuf) are all
 * dead ends on MTK v2 DRM: no render node for gbm, the drm backend only
 * declares DMABUF (not SHM) so shm never gets picked, CREATE_DUMB returns
 * EINVAL (no dumb-buffer support), and udmabuf needs drm_fd<0. Root causes
 * fully diagnosed in fedora-native/09-panel-native-path.md. The device DOES
 * support dma-heap -> PRIME -> ADDFB2 scanout (proven end-to-end by the
 * drmpanelprobe probe on 2026-09-16: real dma-heap buffer painted the panel).
 *
 * Rather than patching wlroots' render/allocator/allocator.c and rebuilding
 * the whole ~150-file library (and its dozen external deps) against a cross
 * sysroot, this is an LD_PRELOAD interposer for the single entry point the
 * compositor actually calls: wlr_allocator_autocreate(). Confirmed safe to
 * interpose (2026-09-16): labwc imports it as an UND global symbol, and
 * neither labwc nor libwlroots-0.19.so link -Bsymbolic (checked via readelf
 * -d on both — only BIND_NOW/PIE flags present, which govern eager PLT
 * resolution, not interposability). If our allocator can't be created (no
 * usable DRM fd), this falls back to the real upstream wlr_allocator_autocreate
 * via dlsym(RTLD_NEXT, ...) — normal behavior is preserved when this shim
 * isn't wanted.
 *
 * Buffer capabilities: DATA_PTR (mmap the dma-heap fd directly — pixman
 * renderer draws via CPU) + DMABUF (hand the same fd to the drm backend's
 * existing, unpatched PRIME_FD_TO_HANDLE + ADDFB2 import path). Only
 * XRGB8888/ARGB8888 linear is supported — the only format path the probe
 * proved; anything else falls through to the real allocator.
 */
#include <dlfcn.h>
#include <drm_fourcc.h>
#include <errno.h>
#include <execinfo.h>
#include <fcntl.h>
#include <linux/dma-buf.h>
#include <linux/dma-heap.h>
#include <signal.h>
#include <stdio.h>
#include <ucontext.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <xkbcommon/xkbcommon.h>

#include <EGL/egl.h>
#include <EGL/eglext.h>

#include <wlr/backend.h>
#include <wlr/interfaces/wlr_buffer.h>
#include <wlr/render/allocator.h>
#include <wlr/render/dmabuf.h>
#include <wlr/render/drm_format_set.h>
#include <wlr/render/egl.h>
#include <wlr/render/gles2.h>

#define DMAHEAP_PATH "/dev/dma_heap/system"
/* fprintf alone is fully block-buffered once stderr is redirected to a file
 * (not a TTY) — without the explicit fflush, none of this ever reaches disk
 * before a crash. Learned the hard way 2026-09-16: two live crash diagnostic
 * runs came back with completely empty logs because of exactly this. */
#define LOG(...) do { fprintf(stderr, "[dmaheap-shim] " __VA_ARGS__); fflush(stderr); } while (0)

/* Also make stderr unbuffered process-wide, as early as possible (library
 * load, before main()) — this is the only way to reliably capture wlroots'
 * OWN wlr_log() output too (we don't control its fprintf calls to add
 * fflush), which matters if the crash is past the point our LOG() calls
 * cover. */
/* Raw write() canary, bypassing stdio entirely, to a fixed path independent
 * of whatever fd 1/2 happen to be redirected to. If /tmp/dmaheap-canary.log
 * doesn't exist after a crash, the .so's constructor never ran at all (crash
 * during dynamic linker relocation, or LD_PRELOAD not actually taking effect
 * for this process) — a fundamentally different problem than a crash inside
 * our own logic further down. */
static void canary(const char *msg) {
	int fd = open("/tmp/dmaheap-canary.log", O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
	if (fd >= 0) {
		write(fd, msg, strlen(msg));
		close(fd);
	}
}

/* Real crash location, since Android's debuggerd/crash_dump can't reach
 * into this chroot's process tree (confirmed 2026-09-16: no tombstone, no
 * logcat crash entry for any of 4 prior labwc segfaults). Async-signal-safe
 * subset only: backtrace() + backtrace_symbols_fd() are both documented safe
 * to call from a signal handler; write() is a raw syscall. Each process gets
 * its own PID-suffixed file so concurrent/successive crashers don't clobber
 * each other. */
static void crash_handler(int sig, siginfo_t *info, void *ucontext_arg) {
	char path[64];
	int n = snprintf(path, sizeof(path), "/tmp/dmaheap-crash.%d.log", getpid());
	(void)n;
	int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
	if (fd < 0) {
		fd = 2;
	}
	char hdr[160];
	int hn = snprintf(hdr, sizeof(hdr),
		"CRASH pid=%d sig=%d code=%d addr=%p\n",
		getpid(), sig, info->si_code, info->si_addr);
	if (hn > 0) {
		write(fd, hdr, (size_t)hn);
	}

	/* backtrace() cannot unwind across the signal-trampoline boundary
	 * without DWARF CFI, so frame 0 is useless (observed 2026-09-16: it
	 * just points back inside this handler). Read the real faulting PC
	 * and registers straight from the kernel-populated ucontext instead. */
	ucontext_t *uc = (ucontext_t *)ucontext_arg;
	char regs[512];
	int rn = snprintf(regs, sizeof(regs),
		"pc=0x%llx sp=0x%llx lr(x30)=0x%llx "
		"x0=0x%llx x1=0x%llx x2=0x%llx x3=0x%llx\n",
		(unsigned long long)uc->uc_mcontext.pc,
		(unsigned long long)uc->uc_mcontext.sp,
		(unsigned long long)uc->uc_mcontext.regs[30],
		(unsigned long long)uc->uc_mcontext.regs[0],
		(unsigned long long)uc->uc_mcontext.regs[1],
		(unsigned long long)uc->uc_mcontext.regs[2],
		(unsigned long long)uc->uc_mcontext.regs[3]);
	if (rn > 0) {
		write(fd, regs, (size_t)rn);
	}

	/* /proc/self/maps lets us map pc/lr back to "which library + offset"
	 * after the fact, since ASLR means we don't know load bases otherwise. */
	int maps_fd = open("/proc/self/maps", O_RDONLY);
	if (maps_fd >= 0) {
		const char *sep = "--- /proc/self/maps ---\n";
		write(fd, sep, strlen(sep));
		char buf[4096];
		ssize_t r;
		while ((r = read(maps_fd, buf, sizeof(buf))) > 0) {
			write(fd, buf, (size_t)r);
		}
		close(maps_fd);
	}

	void *bt[64];
	int count = backtrace(bt, 64);
	const char *btsep = "--- backtrace() (frame 0 unreliable across signal boundary) ---\n";
	write(fd, btsep, strlen(btsep));
	backtrace_symbols_fd(bt, count, fd);

	if (fd != 2) {
		close(fd);
	}
	signal(sig, SIG_DFL);
	raise(sig);
}

__attribute__((constructor))
static void dmaheap_shim_ctor(void) {
	canary("dmaheap_shim: constructor ran\n");
	setvbuf(stderr, NULL, _IONBF, 0);

	struct sigaction sa;
	memset(&sa, 0, sizeof(sa));
	sa.sa_sigaction = crash_handler;
	sa.sa_flags = SA_SIGINFO;
	sigaction(SIGSEGV, &sa, NULL);
	sigaction(SIGBUS, &sa, NULL);
	sigaction(SIGABRT, &sa, NULL);
	sigaction(SIGILL, &sa, NULL);
}

/* ---------------- buffer ---------------- */

struct dmaheap_buffer {
	struct wlr_buffer base; // must be first member
	uint32_t format;
	uint32_t stride;
	size_t size;
	void *data;
	struct wlr_dmabuf_attributes dmabuf;
	uint32_t sync_flags; // DMA_BUF_SYNC_* set by begin_data_ptr_access, read by end
};

static const struct wlr_buffer_impl dmaheap_buffer_impl;

static struct dmaheap_buffer *dmaheap_buffer_from_buffer(struct wlr_buffer *b) {
	return (struct dmaheap_buffer *)b;
}

/* 2026-09-17 fix: dma-heap buffers on this SoC are NOT CPU/display
 * cache-coherent by default. begin/end_data_ptr_access previously handed
 * pixman the raw mmap'd pointer with no DMA_BUF_IOCTL_SYNC bracketing at
 * all — pixman's damage-tracked (partial-repaint) writes could sit in CPU
 * cache lines the display's DMA read never observed, which is exactly what
 * "rectangular holes" in the live labwc/foot session (2026-09-17, M0 test)
 * looks like: blocks of stale/pre-clear content where only the *undamaged*
 * regions had actually reached memory. Fix: bracket every access with
 * DMA_BUF_IOCTL_SYNC per the kernel UAPI contract (linux/dma-buf.h) — START
 * before pixman touches the pointer, END after, translating wlroots' own
 * WLR_BUFFER_DATA_PTR_ACCESS_READ/WRITE (confirmed against the real
 * wlroots-0.19.3 devel headers, wlr/types/wlr_buffer.h) into the matching
 * DMA_BUF_SYNC_READ/WRITE bits 1:1. */
static uint32_t dmaheap_sync_flags(uint32_t wlr_flags) {
	uint32_t f = 0;
	if (wlr_flags & WLR_BUFFER_DATA_PTR_ACCESS_READ) {
		f |= DMA_BUF_SYNC_READ;
	}
	if (wlr_flags & WLR_BUFFER_DATA_PTR_ACCESS_WRITE) {
		f |= DMA_BUF_SYNC_WRITE;
	}
	return f ? f : DMA_BUF_SYNC_RW; // unknown/empty flags: sync both, never zero
}

static bool dmaheap_buffer_begin_data_ptr_access(struct wlr_buffer *wlr_buffer,
		uint32_t flags, void **data, uint32_t *format, size_t *stride) {
	struct dmaheap_buffer *buf = dmaheap_buffer_from_buffer(wlr_buffer);
	buf->sync_flags = dmaheap_sync_flags(flags);
	struct dma_buf_sync sync = { .flags = DMA_BUF_SYNC_START | buf->sync_flags };
	if (ioctl(buf->dmabuf.fd[0], DMA_BUF_IOCTL_SYNC, &sync) < 0) {
		LOG("DMA_BUF_IOCTL_SYNC(START) failed on fd=%d: %s\n",
			buf->dmabuf.fd[0], strerror(errno));
	}
	*data = buf->data;
	*format = buf->format;
	*stride = buf->stride;
	return true;
}

static void dmaheap_buffer_end_data_ptr_access(struct wlr_buffer *wlr_buffer) {
	struct dmaheap_buffer *buf = dmaheap_buffer_from_buffer(wlr_buffer);
	struct dma_buf_sync sync = { .flags = DMA_BUF_SYNC_END | buf->sync_flags };
	if (ioctl(buf->dmabuf.fd[0], DMA_BUF_IOCTL_SYNC, &sync) < 0) {
		LOG("DMA_BUF_IOCTL_SYNC(END) failed on fd=%d: %s\n",
			buf->dmabuf.fd[0], strerror(errno));
	}
	// dma-heap buffer stays mapped for its whole lifetime; nothing else to do.
}

static bool dmaheap_buffer_get_dmabuf(struct wlr_buffer *wlr_buffer,
		struct wlr_dmabuf_attributes *attribs) {
	struct dmaheap_buffer *buf = dmaheap_buffer_from_buffer(wlr_buffer);
	*attribs = buf->dmabuf; // same fd handed out each time, not dup'd — the
	                        // buffer itself owns and closes it at destroy,
	                        // matching wlroots' own drm_dumb.c convention.
	return true;
}

static void dmaheap_buffer_destroy(struct wlr_buffer *wlr_buffer) {
	struct dmaheap_buffer *buf = dmaheap_buffer_from_buffer(wlr_buffer);
	wlr_buffer_finish(wlr_buffer);
	if (buf->data != NULL && buf->data != MAP_FAILED) {
		munmap(buf->data, buf->size);
	}
	wlr_dmabuf_attributes_finish(&buf->dmabuf);
	free(buf);
}

static const struct wlr_buffer_impl dmaheap_buffer_impl = {
	.destroy = dmaheap_buffer_destroy,
	.get_dmabuf = dmaheap_buffer_get_dmabuf,
	.begin_data_ptr_access = dmaheap_buffer_begin_data_ptr_access,
	.end_data_ptr_access = dmaheap_buffer_end_data_ptr_access,
};

static bool format_supported(const struct wlr_drm_format *format) {
	if (format->format != DRM_FORMAT_XRGB8888 &&
			format->format != DRM_FORMAT_ARGB8888) {
		return false;
	}
	for (size_t i = 0; i < format->len; i++) {
		if (format->modifiers[i] == DRM_FORMAT_MOD_LINEAR ||
				format->modifiers[i] == DRM_FORMAT_MOD_INVALID) {
			return true;
		}
	}
	return false;
}

static struct wlr_buffer *dmaheap_create_buffer(struct wlr_allocator *wlr_alloc,
		int width, int height, const struct wlr_drm_format *format) {
	canary("dmaheap_create_buffer: entered\n");
	if (!format_supported(format)) {
		LOG("create_buffer: unsupported format 0x%08x, refusing\n", format->format);
		return NULL;
	}

	const uint32_t bpp = 4; // XRGB8888 / ARGB8888 only
	const uint32_t stride = (uint32_t)width * bpp;
	const size_t size = (size_t)stride * (size_t)height;

	int heap_fd = open(DMAHEAP_PATH, O_RDWR | O_CLOEXEC);
	if (heap_fd < 0) {
		LOG("open(%s) failed: %s\n", DMAHEAP_PATH, strerror(errno));
		return NULL;
	}

	struct dma_heap_allocation_data alloc_data = {
		.len = size,
		.fd = 0,
		.fd_flags = O_RDWR | O_CLOEXEC, // REQUIRED: fd_flags=O_RDONLY-only mmaps EACCES
		.heap_flags = 0,
	};
	int ret = ioctl(heap_fd, DMA_HEAP_IOCTL_ALLOC, &alloc_data);
	close(heap_fd);
	if (ret < 0) {
		LOG("DMA_HEAP_IOCTL_ALLOC failed: %s\n", strerror(errno));
		return NULL;
	}
	int dmabuf_fd = (int)alloc_data.fd;

	void *data = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, dmabuf_fd, 0);
	if (data == MAP_FAILED) {
		LOG("mmap dma-heap buffer failed: %s\n", strerror(errno));
		close(dmabuf_fd);
		return NULL;
	}
	memset(data, 0, size);

	struct dmaheap_buffer *buffer = calloc(1, sizeof(*buffer));
	if (buffer == NULL) {
		munmap(data, size);
		close(dmabuf_fd);
		return NULL;
	}

	wlr_buffer_init(&buffer->base, &dmaheap_buffer_impl, width, height);
	buffer->format = format->format;
	buffer->stride = stride;
	buffer->size = size;
	buffer->data = data;
	buffer->dmabuf = (struct wlr_dmabuf_attributes){
		.width = width,
		.height = height,
		.format = format->format,
		.modifier = DRM_FORMAT_MOD_LINEAR,
		.n_planes = 1,
		.offset = {0, 0, 0, 0},
		.stride = {stride, 0, 0, 0},
		.fd = {dmabuf_fd, -1, -1, -1},
	};

	LOG("allocated %dx%d dma-heap buffer (fd=%d, stride=%u, size=%zu)\n",
		width, height, dmabuf_fd, stride, size);
	return &buffer->base;
}

/* ---------------- allocator ---------------- */

struct dmaheap_allocator {
	struct wlr_allocator base;
};

static void dmaheap_allocator_destroy(struct wlr_allocator *wlr_alloc) {
	free((struct dmaheap_allocator *)wlr_alloc);
}

static const struct wlr_allocator_interface dmaheap_allocator_impl = {
	.create_buffer = dmaheap_create_buffer,
	.destroy = dmaheap_allocator_destroy,
};

static struct wlr_allocator *dmaheap_allocator_create(void) {
	if (access(DMAHEAP_PATH, R_OK | W_OK) != 0) {
		LOG("%s not accessible: %s\n", DMAHEAP_PATH, strerror(errno));
		return NULL;
	}
	struct dmaheap_allocator *alloc = calloc(1, sizeof(*alloc));
	if (alloc == NULL) {
		return NULL;
	}
	wlr_allocator_init(&alloc->base, &dmaheap_allocator_impl,
		WLR_BUFFER_CAP_DATA_PTR | WLR_BUFFER_CAP_DMABUF);
	return &alloc->base;
}

/* ---------------- interposer entry point ---------------- */

typedef struct wlr_allocator *(*autocreate_fn)(struct wlr_backend *,
	struct wlr_renderer *);

struct wlr_allocator *wlr_allocator_autocreate(struct wlr_backend *backend,
		struct wlr_renderer *renderer) {
	canary("wlr_allocator_autocreate: entered (interposition worked)\n");
	int drm_fd = wlr_backend_get_drm_fd(backend);
	canary("wlr_allocator_autocreate: got drm_fd\n");
	LOG("wlr_allocator_autocreate: drm_fd=%d\n", drm_fd);

	if (drm_fd >= 0) {
		canary("wlr_allocator_autocreate: calling dmaheap_allocator_create\n");
		struct wlr_allocator *alloc = dmaheap_allocator_create();
		canary("wlr_allocator_autocreate: dmaheap_allocator_create returned\n");
		if (alloc != NULL) {
			LOG("using dma-heap allocator\n");
			canary("wlr_allocator_autocreate: returning dma-heap allocator\n");
			return alloc;
		}
		LOG("dma-heap allocator unavailable, falling back to upstream autocreate\n");
	}

	static autocreate_fn real_autocreate = NULL;
	static bool looked_up = false;
	if (!looked_up) {
		real_autocreate = (autocreate_fn)dlsym(RTLD_NEXT, "wlr_allocator_autocreate");
		looked_up = true;
	}
	if (real_autocreate == NULL) {
		LOG("dlsym(RTLD_NEXT, wlr_allocator_autocreate) failed: %s\n", dlerror());
		return NULL;
	}
	return real_autocreate(backend, renderer);
}

/* wlr_egl_destroy() is a real exported libwlroots symbol (confirmed via
 * readelf --dyn-syms on the on-device libwlroots-0.19.so) but isn't declared
 * in the public wlr/render/egl.h shipped by wlroots0.19-devel — it's an
 * internal-but-exported cleanup function. Signature matches render/egl.c's
 * actual definition (read from the cloned wlroots 0.19 source, not guessed). */
extern void wlr_egl_destroy(struct wlr_egl *egl);

/* ---------------- wlr_renderer_autocreate interposer (Item 04) ----------------
 *
 * 2026-09-17 late evening. driver-build/04-gpu-acceleration.md's "GBM-platform
 * question: ANSWERED" section has the full writeup; summary here.
 *
 * wlr_renderer_autocreate()'s automatic GLES2 path
 * (wlr_gles2_renderer_create_with_drm_fd -> wlr_egl_create_with_drm_fd) is a
 * confirmed dead end against the Mali blob via hybris: that function only
 * succeeds via the EGL_EXT_platform_device or EGL_KHR_platform_gbm/
 * EGL_MESA_platform_gbm client extensions, and the blob's real client
 * extension string (live-verified) has none of them — it only knows how to
 * be an EGL_KHR_platform_android platform, never DRM/GBM.
 *
 * The real path bypasses that function entirely: wlr_egl_create_with_context()
 * builds a struct wlr_egl from an EXISTING EGLDisplay+EGLContext (only checks
 * client type == EGL_OPENGL_ES_API and version >= 2), and
 * wlr_gles2_renderer_create() only hard-requires EGL_EXT_image_dma_buf_import
 * + GL_EXT_texture_format_BGRA8888 + GL_EXT_unpack_subimage — all three
 * live-verified present on this blob (gles2_ext_probe.c, same artifacts dir).
 * Both are real, unmodified, exported libwlroots functions — no wlroots
 * source patch needed.
 *
 * Same interposition mechanism as wlr_allocator_autocreate above (labwc
 * imports this symbol UND global, libwlroots-0.19.so exports it GLOBAL
 * DEFAULT, no -Bsymbolic on either side — reconfirmed via readelf 2026-09-17,
 * same as the original 2026-09-16 check for the allocator).
 *
 * Gated on HYBRIS_EGLPLATFORM being set in the environment, so this is
 * completely inert or a session that doesn't set up the hybris env (falls
 * straight through to the real autocreate) — the launcher script controls
 * whether this path is even attempted, not this file.
 *
 * IMPORTANT runtime precondition this does NOT establish itself: whichever
 * libEGL.so.1/libGLESv2.so.2 the PROCESS resolves for libwlroots-0.19.so's
 * own DT_NEEDED entries must already be hybris's, not the chroot's own Mesa
 * copy (a separate, previously-undocumented landmine, also written up in 04
 * and empirically fixed by launcher-side LD_LIBRARY_PATH ordering — vendor_probe.c
 * proves it). If the launcher gets that ordering wrong, eglGetDisplay/
 * eglInitialize below silently succeed against Mesa/llvmpipe instead, and
 * everything downstream still "works" but isn't actually using the Mali
 * blob — check GL_RENDERER in the log output to tell them apart.
 */

struct wlr_renderer *wlr_renderer_autocreate(struct wlr_backend *backend) {
	canary("wlr_renderer_autocreate: entered (interposition worked)\n");

	if (getenv("HYBRIS_EGLPLATFORM") == NULL) {
		LOG("wlr_renderer_autocreate: HYBRIS_EGLPLATFORM not set, "
			"not attempting the hybris/Mali path\n");
		goto fallback;
	}

	EGLDisplay dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
	if (dpy == EGL_NO_DISPLAY) {
		LOG("wlr_renderer_autocreate: eglGetDisplay failed\n");
		goto fallback;
	}

	EGLint major = 0, minor = 0;
	if (!eglInitialize(dpy, &major, &minor)) {
		LOG("wlr_renderer_autocreate: eglInitialize failed (0x%x)\n", eglGetError());
		goto fallback;
	}
	LOG("wlr_renderer_autocreate: EGL %d.%d, vendor=%s\n",
		major, minor, eglQueryString(dpy, EGL_VENDOR));
	canary("wlr_renderer_autocreate: eglInitialize OK\n");

	if (!eglBindAPI(EGL_OPENGL_ES_API)) {
		LOG("wlr_renderer_autocreate: eglBindAPI failed (0x%x)\n", eglGetError());
		goto fallback;
	}

	EGLint cfg_attribs[] = {
		EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
		EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
		EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
		EGL_NONE
	};
	EGLConfig cfg;
	EGLint num_cfg = 0;
	if (!eglChooseConfig(dpy, cfg_attribs, &cfg, 1, &num_cfg) || num_cfg < 1) {
		LOG("wlr_renderer_autocreate: eglChooseConfig failed (0x%x)\n", eglGetError());
		goto fallback;
	}

	EGLint ctx_attribs[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
	EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attribs);
	if (ctx == EGL_NO_CONTEXT) {
		LOG("wlr_renderer_autocreate: eglCreateContext failed (0x%x)\n", eglGetError());
		goto fallback;
	}
	canary("wlr_renderer_autocreate: eglCreateContext OK\n");

	struct wlr_egl *egl = wlr_egl_create_with_context(dpy, ctx);
	if (egl == NULL) {
		LOG("wlr_renderer_autocreate: wlr_egl_create_with_context failed\n");
		eglDestroyContext(dpy, ctx);
		goto fallback;
	}
	canary("wlr_renderer_autocreate: wlr_egl_create_with_context OK\n");

	struct wlr_renderer *renderer = wlr_gles2_renderer_create(egl);
	if (renderer == NULL) {
		LOG("wlr_renderer_autocreate: wlr_gles2_renderer_create failed\n");
		/* egl is now owned by nothing on failure per wlroots' own contract
		 * (wlr_gles2_renderer_create_with_drm_fd calls wlr_egl_destroy(egl)
		 * itself in this situation) — do the same here for symmetry. */
		wlr_egl_destroy(egl);
		goto fallback;
	}

	LOG("wlr_renderer_autocreate: hybris/Mali GLES2 renderer created OK\n");
	canary("wlr_renderer_autocreate: returning hybris/Mali renderer\n");
	return renderer;

fallback: ;
	typedef struct wlr_renderer *(*renderer_autocreate_fn)(struct wlr_backend *);
	static renderer_autocreate_fn real_autocreate = NULL;
	static bool looked_up = false;
	if (!looked_up) {
		real_autocreate = (renderer_autocreate_fn)dlsym(RTLD_NEXT, "wlr_renderer_autocreate");
		looked_up = true;
	}
	if (real_autocreate == NULL) {
		LOG("dlsym(RTLD_NEXT, wlr_renderer_autocreate) failed: %s\n", dlerror());
		return NULL;
	}
	return real_autocreate(backend);
}

/* ---------------- xkb NULL-state crash workarounds ----------------
 *
 * RECONSTRUCTED 2026-09-17: these interposers existed in the .so deployed
 * by the 2026-09-16 evening session (confirmed via `strings` on the backup
 * copy, libwlr_dmaheap_shim.so.pre-cachesync-bak, which still has the exact
 * log text below) but were NEVER present in this source file — a gap
 * between what got built/deployed live and what got saved back to the
 * canonical source, not noticed until a later rebuild (2026-09-17, the
 * dma-heap cache-sync fix) silently dropped them, causing a real regression
 * (labwc crashed at the same NULL-xkb_state site again, see
 * driver-build/06 for the incident). Reconstructed from the documented
 * behavior (driver-build/06's "xkbcommon crash chain" writeup) and the
 * exact strings recovered from the backup binary, not guessed.
 *
 * wlroots'/labwc's own keyboard/seat init calls xkb_state_get_keymap() with
 * an uninitialized/NULL xkb_state — not this shim's doing, not the
 * allocator's doing (see 06 for the full diagnosis: SIGSEGV at
 * libxkbcommon.so offset 0x27e58, x0=0 i.e. NULL state arg, 12 bytes into
 * the real function). Interposing it to return NULL instead of
 * dereferencing NULL just moves the crash to the next call in the same
 * chain (xkb_keymap_num_layouts(NULL)), so both need the same treatment.
 * Falls back to the real upstream function via dlsym(RTLD_NEXT, ...) for a
 * genuinely non-NULL state/keymap, same pattern as the allocator interposer
 * above — normal behavior is preserved whenever the NULL case doesn't apply. */

typedef struct xkb_keymap *(*xkb_state_get_keymap_fn)(struct xkb_state *);
typedef xkb_layout_index_t (*xkb_keymap_num_layouts_fn)(struct xkb_keymap *);

struct xkb_keymap *xkb_state_get_keymap(struct xkb_state *state) {
	if (state == NULL) {
		LOG("xkb_state_get_keymap(NULL) workaround fired\n");
		return NULL;
	}

	static xkb_state_get_keymap_fn real_fn = NULL;
	static bool looked_up = false;
	if (!looked_up) {
		real_fn = (xkb_state_get_keymap_fn)dlsym(RTLD_NEXT, "xkb_state_get_keymap");
		looked_up = true;
	}
	if (real_fn == NULL) {
		LOG("dlsym(RTLD_NEXT, xkb_state_get_keymap) failed: %s\n", dlerror());
		return NULL;
	}
	return real_fn(state);
}

xkb_layout_index_t xkb_keymap_num_layouts(struct xkb_keymap *keymap) {
	if (keymap == NULL) {
		LOG("xkb_keymap_num_layouts(NULL) workaround fired\n");
		return 0;
	}

	static xkb_keymap_num_layouts_fn real_fn = NULL;
	static bool looked_up = false;
	if (!looked_up) {
		real_fn = (xkb_keymap_num_layouts_fn)dlsym(RTLD_NEXT, "xkb_keymap_num_layouts");
		looked_up = true;
	}
	if (real_fn == NULL) {
		LOG("dlsym(RTLD_NEXT, xkb_keymap_num_layouts) failed: %s\n", dlerror());
		return 0;
	}
	return real_fn(keymap);
}
