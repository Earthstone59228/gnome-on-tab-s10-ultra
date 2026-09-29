/* gnome_gbm_shim.c — GNOME Shell/mutter GPU integration.
 *
 * This is the real scope of driver-build/02-display-session-stack.md's "Item D"
 * (previously scoped as "just write a logind shim"). Written 2026-09-18 night after
 * reading mutter's actual source (GNOME/mutter, main branch) instead of assuming the
 * labwc/wlroots GPU work carries over — it doesn't, because mutter's renderer is a
 * completely separate codebase from wlroots.
 *
 * THE PROBLEM (source-verified, not guessed):
 * meta-renderer-native.c's meta_renderer_native_create_renderer_gpu_data() hard-requires
 * a working MetaRenderDeviceGbm for any real (non-headless) GPU:
 *   if (META_IS_RENDER_DEVICE_GBM (render_device)) { ... }
 *   else { g_assert_not_reached (); return NULL; }
 * The surfaceless render device (meta-render-device-surfaceless.c) is ONLY ever
 * constructed when gpu_kms == NULL, i.e. headless/no-physical-display — it can't be used
 * for real panel scanout. There is no built-in mutter equivalent of wlroots'
 * wlr_egl_create_with_context() bypass for a real KMS device.
 *
 * This device's MTK v2 DRM driver has no working GBM/dumb-buffer allocation at all
 * (DRM_IOCTL_MODE_CREATE_DUMB -> EINVAL, confirmed multiple times across this project —
 * see fedora-native/09-panel-native-path.md and dmaheap_shim.c's own header comment,
 * which hit the identical wall for wlroots). Real Mesa gbm_create_device()/gbm_bo_create()
 * would therefore fail here regardless of which compositor calls them — this is a kernel
 * driver limitation, not a wlroots-specific or mutter-specific one.
 *
 * THE FIX: mutter's entire gbm_* usage is through libgbm.so's public, exported API
 * (confirmed via source read of meta-render-device-gbm.c and meta-drm-buffer-gbm.c —
 * grepped every gbm_* call site). mutter NEVER calls gbm_surface_* (only the direct
 * gbm_bo_create[_with_modifiers2] path, confirmed by grep across meta-renderer-native.c
 * and meta-render-device-gbm.c), which simplifies this shim considerably: no gbm_surface
 * implementation needed at all.
 *
 * This LD_PRELOAD interposer REPLACES gbm_create_device, gbm_bo_create (and variants),
 * gbm_bo_get_* accessors, gbm_bo_destroy, and gbm_device_* outright (no dlsym(RTLD_NEXT)
 * real-GBM fallback — there is
 * no working real GBM on this hardware to fall back to) with the exact same proven
 * dma-heap + PRIME mechanism dmaheap_shim.c already uses for wlroots: allocate from
 * /dev/dma_heap/system, import via DRM_IOCTL_PRIME_FD_TO_HANDLE on the SAME fd mutter
 * already holds.
 *
 * Critical correctness point (GEM handles are per struct-drm_file, i.e. per open fd —
 * a handle from one fd is meaningless on another): mutter's own code guarantees the
 * fd consistency this needs. meta_render_device_gbm_new() calls
 * gbm_create_device(meta_device_file_get_fd(device_file)), and
 * meta_drm_buffer_gbm_new_take() stores that SAME device_file for its own later
 * DRM_IOCTL_MODE_ADDFB2 ioctl (meta_drm_buffer_gbm_ensure_fb_id(), source-read) — so the
 * fd our gbm_create_device() receives is exactly the fd our PRIME import must run on, and
 * mutter's later ADDFB2 call reuses that same fd itself. No fd bookkeeping needed beyond
 * "remember the fd we were given."
 *
 * gbm_bo_get_handle_for_plane() always returns {.s32 = -1}, which source-verified forces
 * meta_drm_buffer_gbm_ensure_fb_id()'s single-plane ADDFB2 branch — the only proven buffer
 * shape on this driver (single-plane linear XRGB8888/ARGB8888, exactly matching
 * dmaheap_shim.c's own format_supported() restriction).
 *
 * Companion piece, same file: an eglGetPlatformDisplay/eglGetPlatformDisplayEXT +
 * eglQueryString interposer reroutes mutter's hardcoded EGL_PLATFORM_GBM_KHR request
 * (meta_render_device_gbm_create_egl_display(), source-read) down to the same
 * eglGetDisplay(EGL_DEFAULT_DISPLAY) path already proven for wlroots in dmaheap_shim.c's
 * wlr_renderer_autocreate(). Same underlying reason: the Mali blob only ever advertised
 * EGL_KHR_platform_android as a platform client extension (04-gpu-acceleration.md,
 * live-verified 2026-09-17), never EGL_KHR_platform_gbm/EGL_MESA_platform_gbm, so mutter's
 * platform-based eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, ...) call would fail exactly
 * the same way wlroots' automatic path did.
 *
 * STATUS: in daily use since 2026-09-19 (GNOME Shell 50.4 / mutter 50.4 exactly; re-verify
 * every gbm_* import with `nm -D libmutter-18.so` after any mutter update). Hardware cursor
 * plane stays disabled by the runner (MUTTER_DEBUG_DISABLE_HW_CURSORS=1, SMMU/DEVAPC panic).
 * 2026-09-25 review pass: slot state machine rewrite (lock returned the on-screen buffer),
 * persistent per-slot scanout bos, 4 buffers, gbm_bo user_data/import/write, quiet logging.
 * 2026-09-26 audit #3: slot_lock around every slot transition, deferred gbm_surface_destroy
 * (no use-after-free when mutter releases the on-screen buffer after the onscreen is gone),
 * spawn functions resolved in a constructor, posix_spawn ENOSYS. Independent audit applied.
 */
#include <dlfcn.h>
#include <drm/drm.h>
#include <drm_fourcc.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <time.h>
#include <linux/dma-heap.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <spawn.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <gbm.h>
#include <EGL/egl.h>
#include <EGL/eglext.h>

/* 2026-09-19, third pass: real gbm_surface implementation. See this file's
 * git-tracked history / driver-build/02's "third pass" section for the full
 * reasoning; short version: mutter's create_surfaces_gbm() (the ONLY path in
 * the actually-deployed mutter 50.4, confirmed against real tagged source —
 * there is no create_bos_gbm() in this version) just casts whatever
 * gbm_surface_create() returns straight to EGLNativeWindowType and hands it
 * to eglCreateWindowSurface(). The Mali blob only ever speaks
 * EGL_KHR_platform_android, so it will treat that pointer as a real
 * ANativeWindow* regardless. Real ANativeWindow ABI, from the actual staged
 * headers (android-headers/system/window.h, nativebase/nativebase.h,
 * cutils/native_handle.h) — not guessed. Buffer allocation goes through the
 * REAL, PUBLIC, stable NDK AHardwareBuffer API (AHardwareBuffer_allocate /
 * _getNativeHandle / _release, all confirmed exported by the already-staged
 * libnativewindow.so via nm -D, whose entire dependency closure is already
 * satisfied by the proven 68-lib hybris-vendor closure from 04) — this calls
 * the real IAllocator via Binder internally, so the returned native_handle_t
 * is genuinely, correctly shaped for this exact device/driver, with no
 * private ARM/MTK struct layout guessing needed (that path was tried and
 * deliberately abandoned — the actual make_private_handle()/buffer_descriptor_t
 * layout used by this blob build is a newer, plane-aware version with no
 * matching public source found; guessing it would mean handing a malformed
 * struct to the real driver while it holds DRM master).
 */
#include <android/hardware_buffer.h>
#include <vndk/hardware_buffer.h>
#include <system/window.h>
#include <cutils/native_handle.h>

#define DMAHEAP_PATH "/dev/dma_heap/system"
#define LOG(...) do { fprintf(stderr, "[gnome-gbm-shim] " __VA_ARGS__); fflush(stderr); } while (0)

/* ================= fake gbm_device / gbm_bo ================= */

struct gbm_device {
	int fd; /* the exact fd mutter passed to gbm_create_device(); we never close it —
	         * mutter/MetaDeviceFile owns it, matching real libgbm's own contract
	         * (gbm_device_destroy() never closes the backing fd either). */
};

struct gbm_bo {
	struct gbm_device *dev;
	int dmabuf_fd;        /* dma-heap allocation, owned by this bo (or a dup'd
	                        * real-gralloc fd for a surface-locked front buffer —
	                        * see gbm_surface_lock_front_buffer()) */
	uint32_t gem_handle;  /* imported on dev->fd via DRM_IOCTL_PRIME_FD_TO_HANDLE */
	uint32_t width, height, stride, format;
	size_t size;
	void *owner_slot;     /* NULL for a plain gbm_bo_create() allocation; set to the
	                        * owning gbm_surface_buffer* when this wraps a surface's
	                        * front buffer. Such a bo is persistent (one per slot, like
	                        * real libgbm) and is only freed by gbm_surface_destroy(). */
	/* gbm_bo_set_user_data()/get_user_data(): mutter 50.4 caches an EGLImage per bo
	 * this way (meta-egl-gbm.c). Missing before 2026-09-25, so those calls resolved
	 * to the real Mesa libgbm operating on this fake struct. */
	void *user_data;
	void (*destroy_user_data)(struct gbm_bo *, void *);
};

static bool format_ok(uint32_t format) {
	return format == GBM_FORMAT_XRGB8888 || format == GBM_FORMAT_ARGB8888;
}

struct gbm_device *gbm_create_device(int fd) {
	struct gbm_device *dev = calloc(1, sizeof(*dev));
	if (dev == NULL) {
		return NULL;
	}
	dev->fd = fd;
	LOG("gbm_create_device(fd=%d) -> %p (dma-heap shim, not real Mesa GBM)\n",
		fd, (void *)dev);
	return dev;
}

void gbm_device_destroy(struct gbm_device *dev) {
	free(dev);
}

int gbm_device_get_fd(struct gbm_device *dev) {
	return dev->fd;
}

const char *gbm_device_get_backend_name(struct gbm_device *dev) {
	(void)dev;
	return "dma_heap_shim";
}

int gbm_device_is_format_supported(struct gbm_device *dev, uint32_t format, uint32_t flags) {
	(void)dev;
	(void)flags;
	return format_ok(format) ? 1 : 0;
}

int gbm_device_get_format_modifier_plane_count(struct gbm_device *dev,
		uint32_t format, uint64_t modifier) {
	(void)dev;
	if (!format_ok(format)) {
		return 0;
	}
	return (modifier == DRM_FORMAT_MOD_LINEAR || modifier == DRM_FORMAT_MOD_INVALID) ? 1 : 0;
}

static struct gbm_bo *alloc_bo(struct gbm_device *dev, uint32_t width, uint32_t height,
		uint32_t format) {
	if (!format_ok(format)) {
		LOG("alloc_bo: unsupported format 0x%08x, refusing\n", format);
		errno = EINVAL;
		return NULL;
	}

	const uint32_t bpp = 4; /* XRGB8888 / ARGB8888 only, same restriction as dmaheap_shim.c */
	const uint32_t stride = width * bpp;
	const size_t size = (size_t)stride * (size_t)height;

	/* 2026-09-19 morning: repeated SEGV_ACCERR/SIGBUS crashes (varies run to
	 * run — a real race, not deterministic) right after mutter starts
	 * actually using a bo allocated here. Leading theory: the Mali/kbase
	 * driver's internal tiling/burst-access assumes some minimum row/size
	 * alignment beyond the exact width*height*4 we report via
	 * gbm_bo_get_stride()/width/height — an over-read past our exact
	 * allocation would show up exactly like this (mapped, wrong-permission
	 * or unmapped access, different specific address each run depending on
	 * allocator/ASLR state). Padding the ACTUAL allocation generously while
	 * reporting the SAME (unpadded) stride/size to mutter costs a
	 * negligible amount of dma-heap memory (305G free) and should absorb
	 * any such over-read without mutter's own accounting ever changing.
	 * Not proven yet — this is the next thing to test, not a confirmed fix. */
	const size_t alloc_size = (((size + 4 * stride + 65535) / 65536) + 1) * 65536;

	int heap_fd = open(DMAHEAP_PATH, O_RDWR | O_CLOEXEC);
	if (heap_fd < 0) {
		LOG("open(%s) failed: %s\n", DMAHEAP_PATH, strerror(errno));
		return NULL;
	}
	struct dma_heap_allocation_data alloc_data = {
		.len = alloc_size,
		.fd = 0,
		.fd_flags = O_RDWR | O_CLOEXEC, /* O_RDONLY-only mmaps EACCES, per dmaheap_shim.c */
		.heap_flags = 0,
	};
	int ret = ioctl(heap_fd, DMA_HEAP_IOCTL_ALLOC, &alloc_data);
	close(heap_fd);
	if (ret < 0) {
		LOG("DMA_HEAP_IOCTL_ALLOC failed: %s\n", strerror(errno));
		return NULL;
	}
	int dmabuf_fd = (int)alloc_data.fd;

	/* Zero-fill the WHOLE padded allocation (not just the logical size) so the padding
	 * margin above is never stale/garbage dma-heap-pool content either. Transient mmap
	 * only (unlike dmaheap_shim.c's buffers, mutter never calls gbm_bo_map() on scanout
	 * bos in the path this shim covers, so there's no reason to keep it mapped). */
	void *data = mmap(NULL, alloc_size, PROT_READ | PROT_WRITE, MAP_SHARED, dmabuf_fd, 0);
	if (data != MAP_FAILED) {
		memset(data, 0, alloc_size);
		munmap(data, alloc_size);
	} else {
		LOG("warning: zero-fill mmap failed: %s (continuing; first frame may show "
			"stale content)\n", strerror(errno));
	}

	struct drm_prime_handle ph = { .fd = dmabuf_fd, .flags = 0 };
	if (ioctl(dev->fd, DRM_IOCTL_PRIME_FD_TO_HANDLE, &ph) < 0) {
		LOG("DRM_IOCTL_PRIME_FD_TO_HANDLE failed on drm_fd=%d dmabuf_fd=%d: %s\n",
			dev->fd, dmabuf_fd, strerror(errno));
		close(dmabuf_fd);
		return NULL;
	}

	struct gbm_bo *bo = calloc(1, sizeof(*bo));
	if (bo == NULL) {
		close(dmabuf_fd);
		return NULL;
	}
	bo->dev = dev;
	bo->dmabuf_fd = dmabuf_fd;
	bo->gem_handle = ph.handle;
	bo->width = width;
	bo->height = height;
	bo->stride = stride;
	bo->format = format;
	bo->size = size;

	LOG("alloc_bo: %ux%u format=0x%08x dmabuf_fd=%d gem_handle=%u stride=%u\n",
		width, height, format, dmabuf_fd, bo->gem_handle, stride);
	return bo;
}

struct gbm_bo *gbm_bo_create(struct gbm_device *dev, uint32_t width, uint32_t height,
		uint32_t format, uint32_t flags) {
	(void)flags;
	return alloc_bo(dev, width, height, format);
}

struct gbm_bo *gbm_bo_create_with_modifiers2(struct gbm_device *dev, uint32_t width,
		uint32_t height, uint32_t format, const uint64_t *modifiers,
		const unsigned int count, uint32_t flags) {
	(void)flags;
	bool ok = (count == 0);
	for (unsigned int i = 0; i < count && !ok; i++) {
		if (modifiers[i] == DRM_FORMAT_MOD_LINEAR || modifiers[i] == DRM_FORMAT_MOD_INVALID) {
			ok = true;
		}
	}
	if (!ok) {
		LOG("gbm_bo_create_with_modifiers2: no LINEAR modifier in requested set, refusing\n");
		errno = ENOTSUP;
		return NULL;
	}
	return alloc_bo(dev, width, height, format);
}

struct gbm_bo *gbm_bo_create_with_modifiers(struct gbm_device *dev, uint32_t width,
		uint32_t height, uint32_t format, const uint64_t *modifiers,
		const unsigned int count) {
	return gbm_bo_create_with_modifiers2(dev, width, height, format, modifiers, count, 0);
}

int gbm_bo_get_fd(struct gbm_bo *bo) {
	int dup_fd = fcntl(bo->dmabuf_fd, F_DUPFD_CLOEXEC, 0);
	if (dup_fd < 0) {
		LOG("gbm_bo_get_fd: dup failed: %s\n", strerror(errno));
	}
	return dup_fd;
}

int gbm_bo_get_fd_for_plane(struct gbm_bo *bo, int plane) {
	if (plane != 0) {
		return -1;
	}
	return gbm_bo_get_fd(bo);
}

uint32_t gbm_bo_get_width(struct gbm_bo *bo) { return bo->width; }
uint32_t gbm_bo_get_height(struct gbm_bo *bo) { return bo->height; }
uint32_t gbm_bo_get_stride(struct gbm_bo *bo) { return bo->stride; }
uint32_t gbm_bo_get_stride_for_plane(struct gbm_bo *bo, int plane) {
	return plane == 0 ? bo->stride : 0;
}
uint32_t gbm_bo_get_format(struct gbm_bo *bo) { return bo->format; }
uint32_t gbm_bo_get_bpp(struct gbm_bo *bo) { (void)bo; return 32; }
int gbm_bo_get_plane_count(struct gbm_bo *bo) { (void)bo; return 1; }
uint64_t gbm_bo_get_modifier(struct gbm_bo *bo) { (void)bo; return DRM_FORMAT_MOD_LINEAR; }
uint32_t gbm_bo_get_offset(struct gbm_bo *bo, int plane) { (void)bo; (void)plane; return 0; }
struct gbm_device *gbm_bo_get_device(struct gbm_bo *bo) { return bo->dev; }

union gbm_bo_handle gbm_bo_get_handle(struct gbm_bo *bo) {
	union gbm_bo_handle h;
	h.u32 = bo->gem_handle;
	return h;
}

union gbm_bo_handle gbm_bo_get_handle_for_plane(struct gbm_bo *bo, int plane) {
	(void)bo;
	(void)plane;
	/* Always -1: forces mutter's meta_drm_buffer_gbm_ensure_fb_id() single-plane branch
	 * (source-verified, meta-drm-buffer-gbm.c) — the only proven buffer shape here. */
	union gbm_bo_handle h;
	h.s32 = -1;
	return h;
}

void gbm_bo_set_user_data(struct gbm_bo *bo, void *data,
		void (*destroy_user_data)(struct gbm_bo *, void *)) {
	bo->user_data = data;
	bo->destroy_user_data = destroy_user_data;
}

void *gbm_bo_get_user_data(struct gbm_bo *bo) {
	return bo->user_data;
}

/* Not supported by this allocator. mutter checks both for failure: gbm_bo_import()
 * (client direct scanout, meta-wayland-dma-buf.c) falls back to compositing, and
 * gbm_bo_write() (cursor plane, disabled here anyway) falls back to a software cursor. */
struct gbm_bo *gbm_bo_import(struct gbm_device *dev, uint32_t type, void *buffer,
		uint32_t flags) {
	(void)dev; (void)type; (void)buffer; (void)flags;
	errno = ENOTSUP;
	return NULL;
}

int gbm_bo_write(struct gbm_bo *bo, const void *buf, size_t count) {
	(void)bo; (void)buf; (void)count;
	errno = ENOTSUP;
	return -1;
}

static void bo_free(struct gbm_bo *bo) {
	if (bo->destroy_user_data != NULL) {
		bo->destroy_user_data(bo, bo->user_data);
	}
	struct drm_gem_close gc = { .handle = bo->gem_handle };
	if (ioctl(bo->dev->fd, DRM_IOCTL_GEM_CLOSE, &gc) < 0) {
		LOG("gbm_bo_destroy: GEM_CLOSE(handle=%u) failed: %s\n",
			bo->gem_handle, strerror(errno));
	}
	close(bo->dmabuf_fd);
	free(bo);
}

static void surface_bo_release(struct gbm_bo *bo);

void gbm_bo_destroy(struct gbm_bo *bo) {
	if (bo == NULL) {
		return;
	}
	if (bo->owner_slot != NULL) {
		/* A surface bo belongs to its slot; mutter releases those through
		 * gbm_surface_release_buffer(). Treat a stray destroy the same way. */
		surface_bo_release(bo);
		return;
	}
	bo_free(bo);
}

/* ================= real gbm_surface, backed by a real ANativeWindow =================
 *
 * History: 2026-09-19 morning stubs (log-and-fail) confirmed the crash diagnosis
 * (undefined PLT symbols) and that mutter genuinely takes this path, not
 * create_bos_gbm() (which doesn't exist in the actually-deployed mutter 50.4 —
 * confirmed against real tagged source the same day, see driver-build/02).
 * 2026-09-19, third pass: real implementation. mutter's create_surfaces_gbm()
 * just does `egl_native_window = (EGLNativeWindowType) new_gbm_surface;` and
 * hands that straight to eglCreateWindowSurface() — it never inspects what we
 * return. The Mali blob only ever speaks EGL_KHR_platform_android, so it will
 * treat that pointer as ANativeWindow* regardless. `struct gbm_surface` is
 * opaque everywhere real GBM uses it, and we own both sides (mutter never
 * looks inside it), so we define it here with the real `struct ANativeWindow`
 * as its FIRST member — a pointer to one is a valid pointer to the other,
 * exactly like Android's own C++ subclasses do this via first-member layout.
 */

/* 4, not 3: mutter 50 renders a new frame while one buffer is on screen and another
 * is waiting for its page flip, and the Mali driver needs a free buffer to dequeue
 * at that moment. With 3 the log showed "no free slot" -> EGL_BAD_ALLOC (3003) ->
 * "Invalid back buffer age: forcing full redraw", i.e. a full-panel repaint hitch. */
#define GBM_SURFACE_NUM_BUFFERS 4

/* Slot lifecycle: FREE -> DEQUEUED (driver renders) -> QUEUED (swap done) ->
 * LOCKED (mutter holds it for scanout) -> FREE (gbm_surface_release_buffer).
 * The old code had only dequeued/queued flags and a locked buffer stayed "queued",
 * so lock_front_buffer() could return the buffer already on screen instead of the
 * newly rendered one, and its early release let the GPU draw into a buffer that
 * was still being scanned out (stale / half-drawn frames). */
enum slot_state { SLOT_FREE, SLOT_DEQUEUED, SLOT_QUEUED, SLOT_LOCKED };

struct gbm_surface_buffer {
	AHardwareBuffer *ahb;
	struct ANativeWindowBuffer anwb;
	enum slot_state state;
	uint64_t queue_seq;  /* order of queueBuffer() calls; the newest QUEUED slot is the front */
	uint64_t free_seq;   /* when the slot last became FREE; dequeue takes the oldest, so the
	                      * buffer that just left scanout is reused last (no present fence
	                      * on this driver tells us when the display stopped reading it) */
	int acquire_fence;   /* GPU-completion sync fence from queueBuffer(), -1 if none;
	                      * waited on in gbm_surface_lock_front_buffer() before KMS may
	                      * scan the buffer out */
	struct gbm_bo *bo;   /* persistent scanout bo: PRIME-imported once on first lock and
	                      * reused, instead of an import + GEM_CLOSE (IOMMU map/unmap of the
	                      * whole ~22 MB buffer) every frame */
	struct gbm_surface *surf; /* owning surface (deferred destroy, see gbm_surface_destroy) */
};

struct gbm_surface {
	struct ANativeWindow anw; /* MUST stay first member — see block comment above */
	struct gbm_device *dev;
	uint32_t width, height, format;
	uint64_t next_seq;
	bool destroyed; /* gbm_surface_destroy() ran while a slot was still LOCKED */
	struct gbm_surface_buffer buffers[GBM_SURFACE_NUM_BUFFERS];
};

/* 2026-09-26 audit #3: one lock for every slot state transition. mutter 50 has a KMS
 * thread next to the main thread; the old "all transitions happen on the main thread"
 * assumption was never verified, and an unsynchronised FREE/LOCKED race would hand the
 * GPU a buffer that is on screen. Uncontended cost is a few ns per call; the fence wait
 * (the only blocking step) runs with the lock released. */
static pthread_mutex_t slot_lock = PTHREAD_MUTEX_INITIALIZER;

static void anw_incref(struct android_native_base_t *base) { (void)base; }
static void anw_decref(struct android_native_base_t *base) { (void)base; }

/* libnativewindow.so (which implements AHardwareBuffer_*) is a BIONIC
 * (Android-ABI) library, confirmed via `file` on the staged copy — it is NOT
 * glibc, and this shim IS glibc (same reason libhybris exists at all for the
 * Mali blob). Calling into it requires hybris's own bionic-compatible
 * loader, not plain dlopen/dlsym, which would crash on the ABI mismatch
 * (different TLS layout, different libc internals). hybris_dlopen/hybris_dlsym
 * are real, exported, documented low-level libhybris API — confirmed present
 * via `nm -D` on the already cross-built and staged libhybris-common.so
 * (artifacts/libhybris-aarch64/lib/libhybris-common.so.1.0.0). This is the
 * same underlying mechanism hybris's own EGL/GLES2 wrapper libraries use
 * internally to reach the real vendor blob — we're just using it directly
 * ourselves, since libhybris's cross-build never included a glibc-side
 * wrapper for libnativewindow/AHardwareBuffer (out of scope for the
 * EGL/GLES2-only build in 04). Declared here, not pulled from a hybris
 * header (none staged) — signature matches libhybris's real, stable,
 * upstream public API (mirrors plain dlopen/dlsym exactly). Resolved via
 * dlsym(RTLD_DEFAULT, ...) rather than a hard link-time dependency —
 * confirmed via readelf that libEGL.so.1 (which mutter/Cogl already links
 * normally) itself has NEEDED libhybris-common.so.1, so it's guaranteed
 * already loaded into the process well before this shim's lazy init runs;
 * this avoids adding a new link-time NEEDED entry to this .so at all. */
typedef void *(*fn_hybris_dlopen)(const char *filename, int flag);
typedef void *(*fn_hybris_dlsym)(void *handle, const char *symbol);

typedef int (*fn_AHardwareBuffer_allocate)(const AHardwareBuffer_Desc *, AHardwareBuffer **);
typedef void (*fn_AHardwareBuffer_describe)(const AHardwareBuffer *, AHardwareBuffer_Desc *);
typedef const native_handle_t *(*fn_AHardwareBuffer_getNativeHandle)(const AHardwareBuffer *);
typedef void (*fn_AHardwareBuffer_release)(AHardwareBuffer *);

static fn_AHardwareBuffer_allocate p_AHardwareBuffer_allocate;
static fn_AHardwareBuffer_describe p_AHardwareBuffer_describe;
static fn_AHardwareBuffer_getNativeHandle p_AHardwareBuffer_getNativeHandle;
static fn_AHardwareBuffer_release p_AHardwareBuffer_release;

#define HYBRIS_VENDOR_LIBNATIVEWINDOW "/usr/local/lib/hybris-vendor/libnativewindow.so"

/* Lazy, one-time load — mirrors this file's existing resolve_real()/looked_up
 * pattern for other optional/late-bound symbols. Returns false (logged) if
 * anything in the chain fails, so callers fail cleanly instead of crashing
 * on a NULL function pointer. */
static bool ensure_native_window_loaded(void) {
	static bool attempted = false;
	static bool ok = false;
	if (attempted) {
		return ok;
	}
	attempted = true;

	fn_hybris_dlopen p_hybris_dlopen = (fn_hybris_dlopen)dlsym(RTLD_DEFAULT, "hybris_dlopen");
	fn_hybris_dlsym p_hybris_dlsym = (fn_hybris_dlsym)dlsym(RTLD_DEFAULT, "hybris_dlsym");
	if (p_hybris_dlopen == NULL || p_hybris_dlsym == NULL) {
		LOG("dlsym(RTLD_DEFAULT, hybris_dlopen/hybris_dlsym) failed: %s\n", dlerror());
		return false;
	}

	void *handle = p_hybris_dlopen(HYBRIS_VENDOR_LIBNATIVEWINDOW, RTLD_NOW);
	if (handle == NULL) {
		LOG("hybris_dlopen(%s) failed\n", HYBRIS_VENDOR_LIBNATIVEWINDOW);
		return false;
	}

	p_AHardwareBuffer_allocate =
		(fn_AHardwareBuffer_allocate)p_hybris_dlsym(handle, "AHardwareBuffer_allocate");
	p_AHardwareBuffer_describe =
		(fn_AHardwareBuffer_describe)p_hybris_dlsym(handle, "AHardwareBuffer_describe");
	p_AHardwareBuffer_getNativeHandle = (fn_AHardwareBuffer_getNativeHandle)
		p_hybris_dlsym(handle, "AHardwareBuffer_getNativeHandle");
	p_AHardwareBuffer_release =
		(fn_AHardwareBuffer_release)p_hybris_dlsym(handle, "AHardwareBuffer_release");

	if (!p_AHardwareBuffer_allocate || !p_AHardwareBuffer_describe ||
			!p_AHardwareBuffer_getNativeHandle || !p_AHardwareBuffer_release) {
		LOG("hybris_dlsym: missing one or more AHardwareBuffer_* symbols in %s "
			"(allocate=%p describe=%p getNativeHandle=%p release=%p)\n",
			HYBRIS_VENDOR_LIBNATIVEWINDOW, (void *)p_AHardwareBuffer_allocate,
			(void *)p_AHardwareBuffer_describe, (void *)p_AHardwareBuffer_getNativeHandle,
			(void *)p_AHardwareBuffer_release);
		return false;
	}

	LOG("ensure_native_window_loaded: %s loaded via hybris_dlopen, all 4 symbols resolved\n",
		HYBRIS_VENDOR_LIBNATIVEWINDOW);
	ok = true;
	return true;
}

/* GBM fourccs are LITTLE-ENDIAN byte orders: DRM_FORMAT_ARGB8888/XRGB8888
 * expect memory bytes B,G,R,(A|X) — NOT the R,G,B,(A|X) layout of
 * HAL_PIXEL_FORMAT_RGBA/RGBX_8888. Allocating RGBA/RGBX buffers here made the
 * Mali blob write R,G,B,X bytes while the display controller scanned the very
 * same buffer as B,G,R,X: red and blue swapped on the panel (GNOME's
 * dark-purple desktop rendered rust-brown; found live 2026-09-20). BGRA_8888's
 * layout B,G,R,A matches ARGB8888 exactly, and its alpha byte is ignored by an
 * XRGB scanout — so both requested fourccs map to the same allocation format.
 * (AHARDWAREBUFFER_FORMAT_B8G8R8X8 would be the exact XRGB match but only
 * exists on API 33+; BGRA_8888 is the universal choice and the alpha channel
 * is opaque here by construction.) */
static uint32_t fourcc_to_ahb_format(uint32_t fourcc) {
	switch (fourcc) {
	case GBM_FORMAT_ARGB8888: return AHARDWAREBUFFER_FORMAT_B8G8R8A8_UNORM;
	case GBM_FORMAT_XRGB8888: return AHARDWAREBUFFER_FORMAT_B8G8R8A8_UNORM;
	default: return 0;
	}
}

static bool gbm_surface_fill_slot(struct gbm_surface *surf, struct gbm_surface_buffer *slot) {
	if (!ensure_native_window_loaded()) {
		return false;
	}

	uint32_t ahb_format = fourcc_to_ahb_format(surf->format);
	if (ahb_format == 0) {
		LOG("gbm_surface_fill_slot: unsupported format 0x%08x\n", surf->format);
		return false;
	}

	AHardwareBuffer_Desc desc;
	memset(&desc, 0, sizeof(desc));
	desc.width = surf->width;
	desc.height = surf->height;
	desc.layers = 1;
	desc.format = ahb_format;
	/* 2026-09-19 night, eighth pass: first live session (5-minute time-box,
	 * clean restore) reached real atomic KMS page-flips to the physical panel
	 * at native 2960x1848 via the real Mali GPU — but the panel showed a
	 * tiled/repeating-block pattern instead of real content. Diagnosis: ARM
	 * Mali gralloc defaults FRAMEBUFFER|COMPOSER_OVERLAY-usage buffers with no
	 * CPU-access hint to AFBC (Arm Frame Buffer Compression) — a tiled,
	 * compressed internal layout — for bandwidth/power reasons. This shim's
	 * gbm_bo_get_modifier() (above) unconditionally reports
	 * DRM_FORMAT_MOD_LINEAR to mutter's ADDFB2 call, so the display
	 * controller scans out AFBC's compressed superblocks as if they were raw
	 * linear pixels — exactly the repeating small-block grid observed live.
	 * Adding a CPU-read usage hint is the standard, portable way to make ARM
	 * gralloc skip AFBC and allocate plain linear memory (AFBC isn't cheaply
	 * CPU-readable, so requesting CPU access forces the linear path) — costs
	 * a little bandwidth/power, not correctness, and needs no private/vendor
	 * usage bits. */
	desc.usage = AHARDWAREBUFFER_USAGE_GPU_FRAMEBUFFER | AHARDWAREBUFFER_USAGE_COMPOSER_OVERLAY |
		AHARDWAREBUFFER_USAGE_CPU_READ_RARELY;

	AHardwareBuffer *ahb = NULL;
	int ret = p_AHardwareBuffer_allocate(&desc, &ahb);
	if (ret != 0 || ahb == NULL) {
		LOG("AHardwareBuffer_allocate(%ux%u, ahb_format=0x%x) failed: %d\n",
			surf->width, surf->height, ahb_format, ret);
		return false;
	}

	AHardwareBuffer_Desc real_desc;
	memset(&real_desc, 0, sizeof(real_desc));
	p_AHardwareBuffer_describe(ahb, &real_desc);

	const native_handle_t *handle = p_AHardwareBuffer_getNativeHandle(ahb);
	if (handle == NULL) {
		LOG("AHardwareBuffer_getNativeHandle returned NULL\n");
		p_AHardwareBuffer_release(ahb);
		return false;
	}
	LOG("gbm_surface_fill_slot: real AHardwareBuffer allocated %ux%u "
		"(real stride=%u) ahb_format=0x%x, handle numFds=%d numInts=%d\n",
		surf->width, surf->height, real_desc.stride, ahb_format,
		handle->numFds, handle->numInts);

	slot->ahb = ahb;
	memset(&slot->anwb, 0, sizeof(slot->anwb));
	slot->anwb.common.magic = ANDROID_NATIVE_BUFFER_MAGIC;
	slot->anwb.common.version = sizeof(struct ANativeWindowBuffer);
	slot->anwb.common.incRef = anw_incref;
	slot->anwb.common.decRef = anw_decref;
	slot->anwb.width = (int)surf->width;
	slot->anwb.height = (int)surf->height;
	slot->anwb.stride = (int)(real_desc.stride ? real_desc.stride : surf->width);
	slot->anwb.format = (int)ahb_format;
	slot->anwb.usage_deprecated = (int)desc.usage;
	slot->anwb.usage = desc.usage;
	slot->anwb.layerCount = desc.layers; /* independent audit finding, 2026-09-19: real
	                                       * AOSP AHardwareBuffer_to_ANativeWindowBuffer
	                                       * always sets this from desc->layers; every real
	                                       * buffer the Mali driver has ever seen had
	                                       * layerCount==1, never 0 (this field's zero-init
	                                       * default from the memset above). */
	slot->anwb.handle = handle;
	slot->state = SLOT_FREE;
	slot->queue_seq = 0;
	slot->free_seq = 0;
	slot->acquire_fence = -1;
	slot->bo = NULL;
	slot->surf = surf;
	return true;
}

static uint64_t free_counter; /* guarded by slot_lock */

static void slot_set_free(struct gbm_surface_buffer *slot) {
	slot->state = SLOT_FREE;
	slot->free_seq = ++free_counter;
}

static void slot_drop_fence(struct gbm_surface_buffer *slot) {
	if (slot->acquire_fence >= 0) {
		close(slot->acquire_fence);
		slot->acquire_fence = -1;
	}
}

static void gbm_surface_free_slot(struct gbm_surface_buffer *slot) {
	slot_drop_fence(slot);
	if (slot->bo != NULL) {
		bo_free(slot->bo);
		slot->bo = NULL;
	}
	if (slot->ahb != NULL && p_AHardwareBuffer_release != NULL) {
		p_AHardwareBuffer_release(slot->ahb);
		slot->ahb = NULL;
	}
}

static int anw_dequeueBuffer(struct ANativeWindow *window,
		struct ANativeWindowBuffer **buffer, int *fenceFd) {
	struct gbm_surface *surf = (struct gbm_surface *)window;
	struct gbm_surface_buffer *slot = NULL;
	pthread_mutex_lock(&slot_lock);
	for (int i = 0; i < GBM_SURFACE_NUM_BUFFERS; i++) {
		struct gbm_surface_buffer *s = &surf->buffers[i];
		if (s->state == SLOT_FREE && (slot == NULL || s->free_seq < slot->free_seq)) {
			slot = s;
		}
	}
	if (slot != NULL) {
		slot->state = SLOT_DEQUEUED;
	}
	pthread_mutex_unlock(&slot_lock);
	if (slot != NULL) {
		*buffer = &slot->anwb;
		if (fenceFd) {
			*fenceFd = -1; /* a FREE slot is off screen (released after its flip) */
		}
		return 0;
	}
	static unsigned long starved;
	if (starved++ < 5) {
		LOG("anw_dequeueBuffer: no free slot (all %d buffers busy)\n", GBM_SURFACE_NUM_BUFFERS);
	}
	return -ENOMEM;
}

static int anw_dequeueBuffer_DEPRECATED(struct ANativeWindow *window,
		struct ANativeWindowBuffer **buffer) {
	return anw_dequeueBuffer(window, buffer, NULL);
}

static struct gbm_surface_buffer *find_slot(struct gbm_surface *surf,
		struct ANativeWindowBuffer *buffer) {
	for (int i = 0; i < GBM_SURFACE_NUM_BUFFERS; i++) {
		if (&surf->buffers[i].anwb == buffer) {
			return &surf->buffers[i];
		}
	}
	return NULL;
}

static int anw_queueBuffer(struct ANativeWindow *window,
		struct ANativeWindowBuffer *buffer, int fenceFd) {
	struct gbm_surface *surf = (struct gbm_surface *)window;
	struct gbm_surface_buffer *slot = find_slot(surf, buffer);
	if (slot == NULL) {
		LOG("anw_queueBuffer: unknown buffer %p\n", (void *)buffer);
		return -EINVAL;
	}
	pthread_mutex_lock(&slot_lock);
	if (slot->state != SLOT_DEQUEUED) {
		int st = slot->state;
		pthread_mutex_unlock(&slot_lock);
		LOG("anw_queueBuffer: buffer %p not dequeued (state %d)\n", (void *)buffer, st);
		if (fenceFd >= 0) {
			close(fenceFd);
		}
		return -EINVAL;
	}
	/* Keep the GPU-completion fence; the wait happens in gbm_surface_lock_front_buffer().
	 * Dropping it let KMS scan out a buffer the Mali was still rendering. */
	slot_drop_fence(slot);
	slot->acquire_fence = fenceFd;
	slot->queue_seq = ++surf->next_seq;
	slot->state = SLOT_QUEUED;
	pthread_mutex_unlock(&slot_lock);
	return 0;
}

static int anw_queueBuffer_DEPRECATED(struct ANativeWindow *window,
		struct ANativeWindowBuffer *buffer) {
	return anw_queueBuffer(window, buffer, -1);
}

static int anw_cancelBuffer(struct ANativeWindow *window,
		struct ANativeWindowBuffer *buffer, int fenceFd) {
	struct gbm_surface *surf = (struct gbm_surface *)window;
	struct gbm_surface_buffer *slot = find_slot(surf, buffer);
	if (slot == NULL) {
		LOG("anw_cancelBuffer: unknown buffer %p\n", (void *)buffer);
		return -EINVAL;
	}
	if (fenceFd >= 0) {
		close(fenceFd);
	}
	pthread_mutex_lock(&slot_lock);
	if (slot->state == SLOT_DEQUEUED) {
		slot_set_free(slot);
	}
	pthread_mutex_unlock(&slot_lock);
	return 0;
}

static int anw_cancelBuffer_DEPRECATED(struct ANativeWindow *window,
		struct ANativeWindowBuffer *buffer) {
	return anw_cancelBuffer(window, buffer, -1);
}

static int anw_lockBuffer_DEPRECATED(struct ANativeWindow *window,
		struct ANativeWindowBuffer *buffer) {
	(void)window; (void)buffer;
	return 0; /* documented as essentially a no-op in the real ABI (system/window.h) */
}

static int anw_query(const struct ANativeWindow *window, int what, int *value) {
	const struct gbm_surface *surf = (const struct gbm_surface *)window;
	switch (what) {
	case NATIVE_WINDOW_WIDTH: *value = (int)surf->width; return 0;
	case NATIVE_WINDOW_HEIGHT: *value = (int)surf->height; return 0;
	case NATIVE_WINDOW_FORMAT: *value = (int)fourcc_to_ahb_format(surf->format); return 0;
	case NATIVE_WINDOW_MIN_UNDEQUEUED_BUFFERS: *value = GBM_SURFACE_NUM_BUFFERS - 1; return 0;
	case NATIVE_WINDOW_CONCRETE_TYPE: *value = NATIVE_WINDOW_SURFACE; return 0;
	case NATIVE_WINDOW_TRANSFORM_HINT: *value = 0; return 0;
	case NATIVE_WINDOW_DEFAULT_WIDTH: *value = (int)surf->width; return 0;
	case NATIVE_WINDOW_DEFAULT_HEIGHT: *value = (int)surf->height; return 0;
	case NATIVE_WINDOW_BUFFER_AGE: *value = 0; return 0;
	case NATIVE_WINDOW_IS_VALID: *value = 1; return 0;
	default:
		return -ENOENT; /* real ANativeWindow implementations return this for
		                 * unsupported queries too — EGL/mutter handle it */
	}
}

/* Most perform() opcodes here are acknowledged (return 0) without acting on
 * the trailing varargs — we never read them, which is safe in C, and this
 * matches the real ABI's intent (many of these are hints/optional-negotiation
 * that a minimal window is allowed to just accept and ignore). */
static int anw_perform(struct ANativeWindow *window, int operation, ...) {
	(void)window;
	switch (operation) {
	case NATIVE_WINDOW_API_CONNECT:
	case NATIVE_WINDOW_API_DISCONNECT:
	case NATIVE_WINDOW_SET_USAGE:
	case NATIVE_WINDOW_SET_USAGE64:
	case NATIVE_WINDOW_SET_BUFFERS_GEOMETRY:
	case NATIVE_WINDOW_SET_BUFFERS_DIMENSIONS:
	case NATIVE_WINDOW_SET_BUFFERS_FORMAT:
	case NATIVE_WINDOW_SET_BUFFERS_TRANSFORM:
	case NATIVE_WINDOW_SET_BUFFER_COUNT:
	case NATIVE_WINDOW_SET_BUFFERS_TIMESTAMP:
	case NATIVE_WINDOW_SET_SCALING_MODE:
	case NATIVE_WINDOW_SET_CROP:
	case NATIVE_WINDOW_SET_BUFFERS_DATASPACE:
	case NATIVE_WINDOW_SET_SURFACE_DAMAGE:
		return 0;
	default:
		LOG("anw_perform: unhandled operation %d\n", operation);
		return -ENOENT;
	}
}

static int anw_setSwapInterval(struct ANativeWindow *window, int interval) {
	(void)window; (void)interval;
	return 0;
}

static struct gbm_surface *gbm_surface_create_common(struct gbm_device *dev, uint32_t width,
		uint32_t height, uint32_t format) {
	if (!format_ok(format)) {
		LOG("gbm_surface_create: unsupported format 0x%08x, refusing\n", format);
		errno = EINVAL;
		return NULL;
	}

	struct gbm_surface *surf = calloc(1, sizeof(*surf));
	if (surf == NULL) {
		return NULL;
	}
	surf->dev = dev;
	surf->width = width;
	surf->height = height;
	surf->format = format;

	surf->anw.common.magic = ANDROID_NATIVE_WINDOW_MAGIC;
	surf->anw.common.version = sizeof(struct ANativeWindow);
	surf->anw.common.incRef = anw_incref;
	surf->anw.common.decRef = anw_decref;
	/* minSwapInterval/maxSwapInterval are const-qualified in the real ABI
	 * (system/window.h) — calloc() above already zero-initialized both,
	 * which is a valid, honest state (no variable swap interval support). */
	surf->anw.setSwapInterval = anw_setSwapInterval;
	surf->anw.dequeueBuffer_DEPRECATED = anw_dequeueBuffer_DEPRECATED;
	surf->anw.lockBuffer_DEPRECATED = anw_lockBuffer_DEPRECATED;
	surf->anw.queueBuffer_DEPRECATED = anw_queueBuffer_DEPRECATED;
	surf->anw.query = anw_query;
	surf->anw.perform = anw_perform;
	surf->anw.cancelBuffer_DEPRECATED = anw_cancelBuffer_DEPRECATED;
	surf->anw.dequeueBuffer = anw_dequeueBuffer;
	surf->anw.queueBuffer = anw_queueBuffer;
	surf->anw.cancelBuffer = anw_cancelBuffer;

	for (int i = 0; i < GBM_SURFACE_NUM_BUFFERS; i++) {
		if (!gbm_surface_fill_slot(surf, &surf->buffers[i])) {
			LOG("gbm_surface_create: failed allocating buffer %d/%d\n",
				i + 1, GBM_SURFACE_NUM_BUFFERS);
			for (int j = 0; j < i; j++) {
				gbm_surface_free_slot(&surf->buffers[j]);
			}
			free(surf);
			errno = ENOMEM;
			return NULL;
		}
	}

	LOG("gbm_surface_create: real ANativeWindow-backed surface %ux%u format=0x%08x "
		"at %p (%d real AHardwareBuffer-backed buffers)\n",
		width, height, format, (void *)surf, GBM_SURFACE_NUM_BUFFERS);
	return surf;
}

struct gbm_surface *gbm_surface_create(struct gbm_device *dev, uint32_t width,
		uint32_t height, uint32_t format, uint32_t flags) {
	(void)flags;
	return gbm_surface_create_common(dev, width, height, format);
}

struct gbm_surface *gbm_surface_create_with_modifiers(struct gbm_device *dev, uint32_t width,
		uint32_t height, uint32_t format, const uint64_t *modifiers, unsigned int count) {
	bool ok = (count == 0);
	for (unsigned int i = 0; i < count && !ok; i++) {
		if (modifiers[i] == DRM_FORMAT_MOD_LINEAR || modifiers[i] == DRM_FORMAT_MOD_INVALID) {
			ok = true;
		}
	}
	if (!ok) {
		LOG("gbm_surface_create_with_modifiers: no LINEAR modifier in requested set, "
			"refusing\n");
		errno = ENOTSUP;
		return NULL;
	}
	return gbm_surface_create_common(dev, width, height, format);
}

/* Real MTK gralloc handles carry >1 fd (03-graphics-stack.md's documented
 * "gralloc_extra" quirk: a zero-size anon_inode:gralloc_extra metadata fd
 * sits BEFORE the real buffer fd) — fstat-scan for the one with real size
 * rather than assuming index 0, exactly as that finding says. */
static int find_real_dmabuf_fd(const native_handle_t *handle) {
	int best_fd = -1;
	off_t best_size = 0;
	for (int i = 0; i < handle->numFds; i++) {
		int fd = handle->data[i];
		struct stat st;
		if (fstat(fd, &st) < 0) {
			continue;
		}
		if (st.st_size > best_size) {
			best_size = st.st_size;
			best_fd = fd;
		}
	}
	return best_fd; /* -1 if every fd was zero-size / fstat failed on all of them */
}

/* Blocks until the GPU has finished writing the buffer, so KMS never scans out a
 * half-drawn frame. Measured 1–3 ms per frame on 2026-09-25; only failures are logged.
 * Takes ownership of (and closes) the fence fd; called WITHOUT slot_lock held. */
static void wait_and_close_fence(int fence) {
	if (fence < 0) {
		return;
	}
	struct pollfd pfd = { .fd = fence, .events = POLLIN };
	int pr;
	do {
		pr = poll(&pfd, 1, 250); /* sync_file fds report POLLIN once signaled */
	} while (pr < 0 && errno == EINTR);
	if (pr <= 0) {
		static unsigned long failures;
		if (__atomic_fetch_add(&failures, 1, __ATOMIC_RELAXED) < 10) {
			LOG("fence wait failed/timed out (pr=%d errno=%d), scanning out anyway\n", pr, errno);
		}
	}
	close(fence);
}

/* Imports the slot's dma-buf on the KMS fd once and keeps the bo for the slot's
 * lifetime (GEM handles are per drm_file; the fd never changes for a surface). */
static struct gbm_bo *slot_get_bo(struct gbm_surface *surface, struct gbm_surface_buffer *slot) {
	if (slot->bo != NULL) {
		return slot->bo;
	}
	const native_handle_t *handle = slot->anwb.handle;
	int real_fd = find_real_dmabuf_fd(handle);
	if (real_fd < 0) {
		LOG("slot_get_bo: no non-zero-size fd found in handle (numFds=%d)\n", handle->numFds);
		errno = EINVAL;
		return NULL;
	}

	int dup_fd = fcntl(real_fd, F_DUPFD_CLOEXEC, 0);
	if (dup_fd < 0) {
		LOG("slot_get_bo: dup failed: %s\n", strerror(errno));
		return NULL;
	}

	struct drm_prime_handle ph = { .fd = dup_fd, .flags = 0 };
	if (ioctl(surface->dev->fd, DRM_IOCTL_PRIME_FD_TO_HANDLE, &ph) < 0) {
		LOG("slot_get_bo: DRM_IOCTL_PRIME_FD_TO_HANDLE failed on drm_fd=%d dmabuf_fd=%d: %s\n",
			surface->dev->fd, dup_fd, strerror(errno));
		close(dup_fd);
		return NULL;
	}

	struct gbm_bo *bo = calloc(1, sizeof(*bo));
	if (bo == NULL) {
		struct drm_gem_close gc = { .handle = ph.handle };
		ioctl(surface->dev->fd, DRM_IOCTL_GEM_CLOSE, &gc);
		close(dup_fd);
		return NULL;
	}
	bo->dev = surface->dev;
	bo->dmabuf_fd = dup_fd;
	bo->gem_handle = ph.handle;
	bo->width = (uint32_t)slot->anwb.width;
	bo->height = (uint32_t)slot->anwb.height;
	bo->stride = (uint32_t)slot->anwb.stride * 4; /* AHardwareBuffer stride is in PIXELS;
	                                                * gbm_bo_get_stride() is in BYTES
	                                                * (XRGB/ARGB8888 only) */
	bo->format = surface->format;
	bo->size = (size_t)bo->stride * bo->height;
	bo->owner_slot = slot;
	slot->bo = bo;

	LOG("slot_get_bo: slot %ld imported %ux%u dmabuf_fd=%d gem_handle=%u stride=%u\n",
		(long)(slot - surface->buffers), bo->width, bo->height, bo->dmabuf_fd,
		bo->gem_handle, bo->stride);
	return bo;
}

struct gbm_bo *gbm_surface_lock_front_buffer(struct gbm_surface *surface) {
	/* The front buffer is the most recently queued one (real libgbm semantics). Any
	 * older queued-but-never-locked buffer is stale and goes back to the free pool. */
	struct gbm_surface_buffer *slot = NULL;
	pthread_mutex_lock(&slot_lock);
	for (int i = 0; i < GBM_SURFACE_NUM_BUFFERS; i++) {
		struct gbm_surface_buffer *s = &surface->buffers[i];
		if (s->state == SLOT_QUEUED && (slot == NULL || s->queue_seq > slot->queue_seq)) {
			slot = s;
		}
	}
	if (slot == NULL) {
		pthread_mutex_unlock(&slot_lock);
		LOG("gbm_surface_lock_front_buffer: no queued buffer\n");
		errno = EINVAL;
		return NULL;
	}
	for (int i = 0; i < GBM_SURFACE_NUM_BUFFERS; i++) {
		struct gbm_surface_buffer *s = &surface->buffers[i];
		if (s != slot && s->state == SLOT_QUEUED) {
			slot_drop_fence(s);
			slot_set_free(s);
		}
	}
	/* LOCKED before the lock is dropped: nothing else may touch this slot while we import
	 * it and wait for the GPU (dequeue only takes FREE slots). */
	slot->state = SLOT_LOCKED;
	int fence = slot->acquire_fence;
	slot->acquire_fence = -1;
	pthread_mutex_unlock(&slot_lock);

	struct gbm_bo *bo = slot_get_bo(surface, slot); /* main thread only; ioctls unlocked */
	if (bo == NULL) {
		pthread_mutex_lock(&slot_lock);
		slot->acquire_fence = fence; /* back to QUEUED; mutter reports the failure */
		slot->state = SLOT_QUEUED;
		pthread_mutex_unlock(&slot_lock);
		return NULL;
	}
	wait_and_close_fence(fence);
	return bo;
}

static void surface_free_now(struct gbm_surface *surface) {
	for (int i = 0; i < GBM_SURFACE_NUM_BUFFERS; i++) {
		gbm_surface_free_slot(&surface->buffers[i]);
	}
	LOG("gbm_surface_destroy: freed %p\n", (void *)surface);
	free(surface);
}

static bool surface_has_locked(const struct gbm_surface *surface) {
	for (int i = 0; i < GBM_SURFACE_NUM_BUFFERS; i++) {
		if (surface->buffers[i].state == SLOT_LOCKED) {
			return true;
		}
	}
	return false;
}

static void surface_bo_release(struct gbm_bo *bo) {
	struct gbm_surface_buffer *slot = (struct gbm_surface_buffer *)bo->owner_slot;
	struct gbm_surface *surf = slot->surf;
	bool free_surface = false;
	pthread_mutex_lock(&slot_lock);
	if (slot->state == SLOT_LOCKED) {
		slot_set_free(slot); /* bo stays imported for the next time this slot is shown */
	}
	if (surf->destroyed && !surface_has_locked(surf)) {
		free_surface = true;
	}
	pthread_mutex_unlock(&slot_lock);
	if (free_surface) {
		surface_free_now(surf); /* last on-screen buffer of a destroyed surface released */
	}
}

void gbm_surface_release_buffer(struct gbm_surface *surface, struct gbm_bo *bo) {
	(void)surface;
	if (bo == NULL || bo->owner_slot == NULL) {
		return;
	}
	surface_bo_release(bo);
}

int gbm_surface_has_free_buffers(struct gbm_surface *surface) {
	int any = 0;
	pthread_mutex_lock(&slot_lock);
	for (int i = 0; i < GBM_SURFACE_NUM_BUFFERS; i++) {
		if (surface->buffers[i].state == SLOT_FREE) {
			any = 1;
			break;
		}
	}
	pthread_mutex_unlock(&slot_lock);
	return any;
}

/* 2026-09-26 audit #3: mutter 50.4 destroys the gbm_surface in
 * meta_onscreen_native_dispose() (meta-onscreen-native.c:3570), and a MetaDrmBufferGbm that
 * is still on screen calls gbm_surface_release_buffer(surface, bo) from its own finalize
 * (meta-drm-buffer-gbm.c:402) whenever its last reference goes — which can be after the
 * onscreen is gone (monitor reconfiguration, 60<->120 Hz mode switch). Freeing everything
 * here made that later call a use-after-free of both the surface and the bo. Now the surface
 * and every LOCKED slot stay alive until their release; the rest is freed immediately. The
 * EGLSurface on top of our ANativeWindow is already destroyed by then (cogl's parent dispose
 * runs first), so no dequeue can arrive afterwards. */
void gbm_surface_destroy(struct gbm_surface *surface) {
	if (surface == NULL) {
		return;
	}
	/* One critical section decides everything: mark destroyed, and either take the whole
	 * surface (nothing on screen) or detach the resources of every slot that is not LOCKED
	 * so they are freed now (4 x ~22 MB would otherwise wait for the last release). The
	 * detached slots are parked as DEQUEUED with no buffer so they are never handed out.
	 * After the unlock nothing here touches `surface` again unless we own it outright. */
	struct gbm_surface_buffer detached[GBM_SURFACE_NUM_BUFFERS];
	int ndetached = 0;
	bool free_all;
	pthread_mutex_lock(&slot_lock);
	surface->destroyed = true;
	free_all = !surface_has_locked(surface);
	if (!free_all) {
		for (int i = 0; i < GBM_SURFACE_NUM_BUFFERS; i++) {
			struct gbm_surface_buffer *s = &surface->buffers[i];
			if (s->state == SLOT_LOCKED) {
				continue;
			}
			detached[ndetached++] = *s;
			s->ahb = NULL;
			s->bo = NULL;
			s->acquire_fence = -1;
			s->state = SLOT_DEQUEUED;
		}
	}
	pthread_mutex_unlock(&slot_lock);
	if (free_all) {
		surface_free_now(surface);
		return;
	}
	for (int i = 0; i < ndetached; i++) {
		gbm_surface_free_slot(&detached[i]); /* own copies: no access to `surface` */
	}
	LOG("gbm_surface_destroy: %p freed %d idle buffer(s), rest deferred until the on-screen "
		"buffer is released\n", (void *)surface, ndetached);
}

/* ================= EGL platform-GBM bypass =================
 *
 * Same pattern as dmaheap_shim.c's wlr_renderer_autocreate(): the Mali blob has no
 * EGL_KHR_platform_gbm / EGL_MESA_platform_gbm client extension, only
 * EGL_KHR_platform_android (04-gpu-acceleration.md, live-verified 2026-09-17). mutter's
 * meta_render_device_gbm_create_egl_display() (source-read) checks for those extensions
 * via eglQueryString(EGL_NO_DISPLAY, EGL_EXTENSIONS) and, if present, calls
 * eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm_device, NULL). We (1) inject the
 * extension strings so the capability check passes, and (2) intercept the actual
 * eglGetPlatformDisplay[EXT] call and silently substitute the real, proven-working
 * eglGetDisplay(EGL_DEFAULT_DISPLAY) path when the platform is GBM. Every other
 * eglGetPlatformDisplay caller (there are none elsewhere in this session — labwc/wlroots
 * doesn't use this entry point, see dmaheap_shim.c) passes straight through untouched.
 */

static void *resolve_real(const char *name) {
	void *fn = dlsym(RTLD_NEXT, name);
	if (fn == NULL) {
		LOG("dlsym(RTLD_NEXT, %s) failed: %s\n", name, dlerror());
	}
	return fn;
}

EGLDisplay eglGetPlatformDisplay(EGLenum platform, void *native_display,
		const EGLAttrib *attrib_list) {
	typedef EGLDisplay (*fn_t)(EGLenum, void *, const EGLAttrib *);
	static fn_t real = NULL;
	static bool looked_up = false;
	if (!looked_up) {
		real = (fn_t)resolve_real("eglGetPlatformDisplay");
		looked_up = true;
	}

	if (platform == EGL_PLATFORM_GBM_KHR) {
		LOG("eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm=%p) -> rerouting to "
			"eglGetDisplay(EGL_DEFAULT_DISPLAY) (proven hybris/Mali path)\n",
			native_display);
		return eglGetDisplay(EGL_DEFAULT_DISPLAY);
	}

	if (real == NULL) {
		return EGL_NO_DISPLAY;
	}
	return real(platform, native_display, attrib_list);
}

EGLDisplay eglGetPlatformDisplayEXT(EGLenum platform, void *native_display,
		const EGLint *attrib_list) {
	typedef EGLDisplay (*fn_t)(EGLenum, void *, const EGLint *);
	static fn_t real = NULL;
	static bool looked_up = false;
	if (!looked_up) {
		real = (fn_t)resolve_real("eglGetPlatformDisplayEXT");
		looked_up = true;
	}

	if (platform == EGL_PLATFORM_GBM_KHR) {
		LOG("eglGetPlatformDisplayEXT(EGL_PLATFORM_GBM_KHR, gbm=%p) -> rerouting to "
			"eglGetDisplay(EGL_DEFAULT_DISPLAY) (proven hybris/Mali path)\n",
			native_display);
		return eglGetDisplay(EGL_DEFAULT_DISPLAY);
	}

	if (real == NULL) {
		return EGL_NO_DISPLAY;
	}
	return real(platform, native_display, attrib_list);
}

/* cogl_renderer_egl_has_client_extensions() (Cogl, called by mutter before it will even
 * attempt eglGetPlatformDisplay) reads EGL_EXTENSIONS off eglQueryString(EGL_NO_DISPLAY,
 * EGL_EXTENSIONS). The real hybris/Mali blob doesn't advertise EGL_KHR_platform_gbm or
 * EGL_MESA_platform_gbm there (04, live-verified) — inject them into the returned string
 * so that capability check passes; the real eglGetPlatformDisplay call for GBM never
 * reaches the driver anyway (interposed above), so advertising a platform we don't
 * actually implement in the driver is safe here, not a lie the driver has to honor. */
const char *eglQueryString(EGLDisplay dpy, EGLint name) {
	typedef const char *(*fn_t)(EGLDisplay, EGLint);
	static fn_t real = NULL;
	static bool looked_up = false;
	if (!looked_up) {
		real = (fn_t)resolve_real("eglQueryString");
		looked_up = true;
	}
	if (real == NULL) {
		return NULL;
	}

	const char *orig = real(dpy, name);
	if (dpy != EGL_NO_DISPLAY || name != EGL_EXTENSIONS || orig == NULL) {
		return orig;
	}
	if (strstr(orig, "EGL_KHR_platform_gbm") != NULL) {
		return orig; /* already advertised for real, nothing to add */
	}

	/* Built once under a lock: two threads racing the first call could otherwise return a
	 * half-written string (audit #3). */
	static char injected[4096];
	static bool built = false;
	static pthread_mutex_t built_lock = PTHREAD_MUTEX_INITIALIZER;
	pthread_mutex_lock(&built_lock);
	if (!built) {
		snprintf(injected, sizeof(injected), "%s EGL_KHR_platform_gbm EGL_MESA_platform_gbm",
			orig);
		built = true;
	}
	pthread_mutex_unlock(&built_lock);
	return injected;
}

/* 2026-09-25 client GPU: mutter 50.4 binds its wl_display to EGL
 * (meta-wayland.c meta_wayland_init_egl -> meta-egl.c egl->eglBindWaylandDisplayWL,
 * resolved through eglGetProcAddress at meta-egl.c:1240). With the Wayland-enabled
 * libhybris that creates the android_wlegl global the hybris client platform needs.
 * We record whether that bind succeeded: the spawn interposers below only hand apps
 * the hybris client environment when it did, because a hybris Wayland client aborts
 * when the compositor does not advertise android_wlegl. If the bind is absent or fails,
 * apps keep the Mesa/llvmpipe path. */
/* 2026-09-26 live finding: mutter resolves these through eglGetProcAddress when MetaEgl is
 * created, BEFORE any EGL display exists. libhybris only answers the Wayland-extension
 * names from its window-system plugin (egl.c eglGetProcAddress -> ws_eglGetProcAddress),
 * which is loaded by the first eglGetDisplay, and returns NULL before that — so mutter
 * cached NULL ("EGL proc 'egl->eglBindWaylandDisplayWL' not resolved"). We therefore hand
 * out fixed trampolines for the two Wayland-buffer entry points and resolve the real
 * functions on first call, when mutter already has an initialised display (it only calls
 * them after checking EGL_WL_bind_wayland_display in that display's extension string). */
struct wl_display;
struct wl_resource;
typedef __eglMustCastToProperFunctionPointerType (*fn_get_proc)(const char *);
typedef EGLBoolean (*fn_bind_wl_display)(EGLDisplay, struct wl_display *);
typedef EGLBoolean (*fn_query_wl_buffer)(EGLDisplay, struct wl_resource *, EGLint, EGLint *);
static fn_get_proc real_get_proc_address;
static fn_bind_wl_display real_bind_wl_display;
static fn_query_wl_buffer real_query_wl_buffer;
static bool wl_display_bound;

static fn_get_proc get_real_get_proc_address(void) {
	static bool looked_up = false;
	if (!looked_up) {
		real_get_proc_address = (fn_get_proc)resolve_real("eglGetProcAddress");
		looked_up = true;
	}
	return real_get_proc_address;
}

static EGLBoolean shim_bind_wl_display(EGLDisplay dpy, struct wl_display *wl_dpy) {
	if (real_bind_wl_display == NULL && get_real_get_proc_address() != NULL) {
		real_bind_wl_display = (fn_bind_wl_display)real_get_proc_address("eglBindWaylandDisplayWL");
	}
	EGLBoolean r = real_bind_wl_display != NULL ? real_bind_wl_display(dpy, wl_dpy) : EGL_FALSE;
	if (r) {
		__atomic_store_n(&wl_display_bound, true, __ATOMIC_RELEASE);
	}
	LOG("eglBindWaylandDisplayWL -> %s (client GPU environment %s)\n",
		r ? "OK" : (real_bind_wl_display ? "FAILED" : "UNAVAILABLE"),
		r ? "enabled" : "disabled; apps stay on Mesa");
	return r;
}

static EGLBoolean shim_query_wl_buffer(EGLDisplay dpy, struct wl_resource *buf, EGLint attr,
		EGLint *value) {
	fn_query_wl_buffer q = __atomic_load_n(&real_query_wl_buffer, __ATOMIC_ACQUIRE);
	if (q == NULL && get_real_get_proc_address() != NULL) {
		q = (fn_query_wl_buffer)real_get_proc_address("eglQueryWaylandBufferWL");
		__atomic_store_n(&real_query_wl_buffer, q, __ATOMIC_RELEASE);
	}
	return q != NULL ? q(dpy, buf, attr, value) : EGL_FALSE;
}

__eglMustCastToProperFunctionPointerType eglGetProcAddress(const char *procname) {
	if (get_real_get_proc_address() == NULL) {
		return NULL;
	}
	if (procname != NULL && strcmp(procname, "eglBindWaylandDisplayWL") == 0) {
		real_bind_wl_display = (fn_bind_wl_display)real_get_proc_address(procname); /* may be NULL now */
		return (__eglMustCastToProperFunctionPointerType)shim_bind_wl_display;
	}
	if (procname != NULL && strcmp(procname, "eglQueryWaylandBufferWL") == 0) {
		real_query_wl_buffer = (fn_query_wl_buffer)real_get_proc_address(procname);
		return (__eglMustCastToProperFunctionPointerType)shim_query_wl_buffer;
	}
	return real_get_proc_address(procname);
}

/* 2026-09-19 morning finding: mutter's meta-renderer-native.c/meta-onscreen-native.c
 * both need to map an EGLConfig back to a GBM fourcc format via
 * eglGetConfigAttrib(dpy, config, EGL_NATIVE_VISUAL_ID, &value) — real Mesa GBM/DRI
 * drivers populate this because they own both the EGL config list AND the GBM format
 * list and know how they correspond. The Mali/hybris blob isn't GBM-aware at all
 * (confirmed 2026-09-17: only advertises EGL_KHR_platform_android), so it never
 * populates EGL_NATIVE_VISUAL_ID for any config — every caller's format-matching loop
 * fails for every format tried. One caller (choose_egl_config_from_gbm_format(),
 * meta-renderer-native.c) handles this gracefully and just logs "Not using format
 * X: ..." and moves on; live-verified via a real crash that at least one other path
 * assumes this query cannot fail and calls g_assert_not_reached() when it does
 * (meta-onscreen-native.c's get_gbm_format_from_egl(), hit from the cursor-plane
 * format-selection path).
 *
 * Fix: synthesize a plausible answer instead of letting the query fail, using the
 * same logic real Mesa DRI drivers use — map RGB(A) channel sizes to the matching
 * fourcc. Only claims XRGB8888 (8/8/8/0) and ARGB8888 (8/8/8/8), the only two formats
 * this shim's dma-heap allocator (above) can actually satisfy — anything else falls
 * through to the real (failing) query rather than lying about a format nothing else
 * here could back. */
EGLBoolean eglGetConfigAttrib(EGLDisplay dpy, EGLConfig config, EGLint attribute, EGLint *value) {
	typedef EGLBoolean (*fn_t)(EGLDisplay, EGLConfig, EGLint, EGLint *);
	static fn_t real = NULL;
	static bool looked_up = false;
	if (!looked_up) {
		real = (fn_t)resolve_real("eglGetConfigAttrib");
		looked_up = true;
	}
	if (real == NULL) {
		return EGL_FALSE;
	}

	if (attribute == EGL_NATIVE_VISUAL_ID) {
		EGLint red = -1, green = -1, blue = -1, alpha = -1;
		bool ok = real(dpy, config, EGL_RED_SIZE, &red) &&
			real(dpy, config, EGL_GREEN_SIZE, &green) &&
			real(dpy, config, EGL_BLUE_SIZE, &blue) &&
			real(dpy, config, EGL_ALPHA_SIZE, &alpha);
		if (ok && red == 8 && green == 8 && blue == 8 && alpha == 8) {
			*value = GBM_FORMAT_ARGB8888;
			return EGL_TRUE;
		}
		if (ok && red == 8 && green == 8 && blue == 8 && alpha == 0) {
			*value = GBM_FORMAT_XRGB8888;
			return EGL_TRUE;
		}
	}

	return real(dpy, config, attribute, value);
}

/* ================= client-app env scrubbing (OPEN ITEM 3 fix, 2026-09-20) =================
 *
 * driver-build/02's "OPEN ITEM 3": app icons show and launch as real processes, but every
 * native GTK app (Calculator, Settings, ...) crashes instantly —
 *   egl.c:344: eglCreateWindowSurface: Assertion
 *   `((struct ANativeWindow *) win)->common.magic == ANDROID_NATIVE_WINDOW_MAGIC' failed.
 * Firefox survives (own EGL fallback); GTK4's Wayland EGL backend does not.
 *
 * Root cause (source-verified via the live gnome-shell-attempt.log, not guessed):
 * gnome-shell-session-runner.sh `export`s HYBRIS_EGLPLATFORM=null, LIBEGL=/LIBGLESV2=
 * .../libGLES_mali.so, and LD_LIBRARY_PATH=.../hybris-vendor:.../hybris because mutter's
 * OWN process needs them (this file's GBM/EGL bypass above only fires because they're
 * set). GLib's g_spawn_async_with_pipes() — what gnome-shell uses to launch every app from
 * a .desktop file's Exec= line — forks gnome-shell and calls execve() directly in the
 * child, before replacing its image; fork() duplicates the parent's environment, so every
 * one of these vars is still sitting right there in the child at the moment it execs the
 * target app, unless something strips them first. Client apps never call any gbm_*
 * function in this file — GTK4's Wayland backend calls
 * eglGetPlatformDisplay(EGL_PLATFORM_WAYLAND_KHR, wl_display, ...) directly against a
 * wl_surface — but HYBRIS_EGLPLATFORM=null forces the Mali blob's EGL implementation to
 * treat ITS EGLNativeWindowType argument as a real ANativeWindow* unconditionally, and the
 * same LD_LIBRARY_PATH ordering that correctly makes mutter resolve libEGL.so.1/
 * libGLESv2.so.2 to the Mali blob via hybris (04-gpu-acceleration.md's "Finding 3"
 * landmine) does the identical resolution for any child too, so a GTK app's
 * wl_egl_window* ends up handed to the Mali blob's Android-native-window code path and
 * trips the assertion above.
 *
 * Fix: interpose execve() — glibc's execvp/execvpe/execl* family all funnel down to this
 * same libc symbol, same interposition mechanism already proven for every other function
 * in this file — and, only for the newly-exec'd program, strip the hybris/Mali-specific
 * vars (plus this shim's own LD_PRELOAD, which is meaningless and NOT safety-neutral for
 * a generic client process — see below) and remove the hybris directories from
 * LD_LIBRARY_PATH, before calling through to the real execve. This does not touch
 * gnome-shell/mutter's own already-running process (which never calls execve, only
 * fork()) or this shim's own already-initialized GBM state — it only changes what NEW
 * child processes inherit. With LD_PRELOAD/LD_LIBRARY_PATH/HYBRIS_.../LIBEGL/LIBGLESV2 gone,
 * ld.so falls back to the chroot's own Mesa libEGL.so.1 (llvmpipe, already installed at
 * /usr/lib64/ for the pre-hybris smoke tests), giving GTK apps a normal
 * EGL_PLATFORM_WAYLAND_KHR path instead of a hard crash — software-rendered, not
 * GPU-accelerated, which is the known tradeoff of this direction (driver-build/02's
 * "direction 1"; "direction 2", a real Wayland-EGL-to-hybris client bridge, is real new
 * engineering for a future session if llvmpipe proves too slow to be usable).
 *
 * LD_PRELOAD is scrubbed outright (not partially, unlike LD_LIBRARY_PATH) because this
 * shim's own gbm_-family/eglGetPlatformDisplay/eglGetConfigAttrib interposers are mutter-
 * specific and have no defined-safe behavior for an arbitrary client app that never
 * expected them loaded — mutter itself never calls execve, so it never loses this
 * shim's protection.
 *
 * Independent audit (2026-09-20, before any live run, per the standing safety-process
 * memory): GO-WITH-CHANGES. Verified correct: exact-prefix env-name matching (no
 * substring false positives, e.g. a hypothetical HYBRIS_EGLPLATFORM_EXTRA is untouched),
 * no double-free/UAF/leak in build_scrubbed_envp/free_scrubbed_envp's borrowed-vs-malloc'd
 * pointer bookkeeping, correct errno save/restore and no use of freed/borrowed memory on
 * execve()'s success path (which never returns). One real, non-hypothetical gap found and
 * fixed as a result: GLib's g_spawn_*() (what gnome-shell actually uses to launch
 * .desktop Exec= apps) can take glibc's posix_spawn() fast path, which does NOT route
 * through the public execve() symbol — the execve-only interposer above would silently
 * never fire for exactly the crash scenario it targets if that path is taken at runtime.
 * Fixed by adding posix_spawn()/posix_spawnp() interposers below, same
 * build_scrubbed_envp() logic, belt-and-suspenders with the execve() interposer. Also
 * flagged (real but low risk, not fixed — inherent to interposing at this level): this
 * fires for every execve/posix_spawn the gnome-shell process makes, not just app
 * launches — e.g. mutter's own pkexec-wrapped backlight helper spawn (05-sensors-audio-
 * misc.md) will also get scrubbed, though a backlight helper needs none of these 8 vars
 * so no actual behavior change is expected there. Needs live verification like every
 * other item in this list.
 */

static const char *const SCRUB_VARS_FULL[] = {
	"HYBRIS_EGLPLATFORM",
	"HYBRIS_EGLPLATFORM_DIR",
	"HYBRIS_LINKER_DIR",
	"HYBRIS_LOGGING_TARGET",
	"HYBRIS_LOGGING_LEVEL",
	"LIBEGL",
	"LIBGLESV2",
	"LD_PRELOAD",
	"G_MESSAGES_DEBUG", /* never let compositor debug settings leak into every app */
	"MUTTER_DEBUG",
	NULL,
};

static bool env_name_matches(const char *entry, const char *name) {
	size_t name_len = strlen(name);
	return strncmp(entry, name, name_len) == 0 && entry[name_len] == '=';
}

static bool is_fully_scrubbed_var(const char *entry) {
	for (int i = 0; SCRUB_VARS_FULL[i] != NULL; i++) {
		if (env_name_matches(entry, SCRUB_VARS_FULL[i])) {
			return true;
		}
	}
	return false;
}

/* Removes exactly the two hybris path components this project's session scripts ever put
 * in LD_LIBRARY_PATH (see gnome-shell-session-runner.sh), keeping any other ':'-separated
 * entries intact and in order. Returns a malloc'd "LD_LIBRARY_PATH=..." string, or NULL if
 * nothing was left (caller then omits the var entirely rather than emitting an empty
 * one). */
static char *scrub_ld_library_path(const char *value) {
	char *copy = strdup(value);
	if (copy == NULL) {
		return NULL;
	}
	char result[4096];
	result[0] = '\0';
	bool any = false;
	char *saveptr = NULL;
	for (char *tok = strtok_r(copy, ":", &saveptr); tok != NULL;
			tok = strtok_r(NULL, ":", &saveptr)) {
		if (strcmp(tok, "/usr/local/lib/hybris-vendor") == 0 ||
				strcmp(tok, "/usr/local/lib/hybris") == 0 ||
				strcmp(tok, "/usr/local/lib/hybris-wl") == 0) {
			continue;
		}
		if (any) {
			strncat(result, ":", sizeof(result) - strlen(result) - 1);
		}
		strncat(result, tok, sizeof(result) - strlen(result) - 1);
		any = true;
	}
	free(copy);
	if (!any) {
		return NULL;
	}
	char *out = malloc(strlen("LD_LIBRARY_PATH=") + strlen(result) + 1);
	if (out != NULL) {
		sprintf(out, "LD_LIBRARY_PATH=%s", result);
	}
	return out;
}

/* Builds a new NULL-terminated envp array for the child's execve() call. Entries not
 * touched are borrowed pointers from the original envp (valid for the lifetime of this
 * call — execve() never modifies argv/envp, and on success the process image is replaced
 * so nothing needs to outlive it; on failure the caller frees the array, not the borrowed
 * strings). Returns NULL on allocation failure, in which case the caller falls back to the
 * real, unscrubbed execve() rather than failing the exec outright. */
/* 2026-09-25 client GPU (libhybris wayland platform through glvnd): the session runner
 * exports GBMSHIM_CHILD_<NAME>=<value> in gnome-shell's own environment. For every spawned
 * program those entries are removed and re-emitted as <NAME>=<value>, unless the child
 * environment already sets <NAME> (after the scrub above, so a scrubbed LIBEGL is
 * re-added from the policy). No GBMSHIM_CHILD_ variables in the runner = old behaviour
 * (apps on Mesa llvmpipe). Injected strings are strdup'd, so free_scrubbed_envp()'s
 * borrowed-vs-malloc'd rule is unchanged. */
#define CHILD_ENV_PREFIX "GBMSHIM_CHILD_"

static bool env_has_name(char *const env[], int n, const char *entry) {
	const char *eq = strchr(entry, '=');
	size_t len = eq ? (size_t)(eq - entry) : strlen(entry);
	for (int i = 0; i < n; i++) {
		if (strncmp(env[i], entry, len) == 0 && env[i][len] == '=') {
			return true;
		}
	}
	return false;
}

static char **build_scrubbed_envp(char *const envp[]) {
	int count = 0;
	while (envp[count] != NULL) {
		count++;
	}
	char **out = calloc((size_t)count + 1, sizeof(char *));
	if (out == NULL) {
		return NULL;
	}
	int j = 0;
	const size_t plen = strlen(CHILD_ENV_PREFIX);
	for (int i = 0; i < count; i++) {
		if (strncmp(envp[i], CHILD_ENV_PREFIX, plen) == 0) {
			continue; /* policy entries are injected below, never passed through as-is */
		}
		if (is_fully_scrubbed_var(envp[i])) {
			continue;
		}
		if (env_name_matches(envp[i], "LD_LIBRARY_PATH")) {
			char *scrubbed = scrub_ld_library_path(envp[i] + strlen("LD_LIBRARY_PATH="));
			if (scrubbed != NULL) {
				out[j++] = scrubbed;
			}
			continue;
		}
		out[j++] = envp[i];
	}
	/* Inject policy entries. j + injected <= count always holds: every injected entry
	 * replaces a GBMSHIM_CHILD_ entry that was skipped above. Only once mutter's
	 * eglBindWaylandDisplayWL succeeded (see shim_bind_wl_display). */
	bool inject = __atomic_load_n(&wl_display_bound, __ATOMIC_ACQUIRE);
	for (int i = 0; inject && i < count; i++) {
		if (strncmp(envp[i], CHILD_ENV_PREFIX, plen) != 0) {
			continue;
		}
		const char *entry = envp[i] + plen;
		if (entry[0] == '\0' || entry[0] == '=' || strchr(entry, '=') == NULL) {
			continue;
		}
		if (env_has_name(out, j, entry)) {
			continue; /* explicit setting in the child's own env wins */
		}
		char *copy = strdup(entry);
		if (copy != NULL) {
			out[j++] = copy;
		}
	}
	out[j] = NULL;
	return out;
}

/* Frees only the entries build_scrubbed_envp() malloc'd itself (the scrubbed
 * LD_LIBRARY_PATH replacement, if any) — every other entry is a borrowed pointer into the
 * original envp and must not be freed. */
static void free_scrubbed_envp(char **scrubbed, char *const orig_envp[]) {
	for (int i = 0; scrubbed[i] != NULL; i++) {
		bool borrowed = false;
		for (int k = 0; orig_envp[k] != NULL; k++) {
			if (scrubbed[i] == orig_envp[k]) {
				borrowed = true;
				break;
			}
		}
		if (!borrowed) {
			free(scrubbed[i]);
		}
	}
	free(scrubbed);
}

/* 2026-09-26 audit #3: the real spawn functions are resolved once in a constructor, in the
 * parent, instead of lazily on first use — GLib's fork+exec path calls execve() in the CHILD,
 * and keeping dlsym() out of the post-fork child is simply the more robust choice. (Correction
 * from the independent audit: glibc 2.43 re-initialises the loader locks and malloc in the
 * child (posix/fork.c), so the old lazy lookup was not an actual deadlock.) If another
 * library's constructor spawns before ours has run, fall back to the lazy lookup. */
typedef int (*fn_execve_t)(const char *, char *const[], char *const[]);
typedef int (*fn_posix_spawn_t)(pid_t *, const char *, const posix_spawn_file_actions_t *,
	const posix_spawnattr_t *, char *const[], char *const[]);
static fn_execve_t real_execve;
static fn_posix_spawn_t real_posix_spawn;
static fn_posix_spawn_t real_posix_spawnp;

__attribute__((constructor)) static void resolve_spawn_functions(void) {
	real_execve = (fn_execve_t)resolve_real("execve");
	real_posix_spawn = (fn_posix_spawn_t)resolve_real("posix_spawn");
	real_posix_spawnp = (fn_posix_spawn_t)resolve_real("posix_spawnp");
}

int execve(const char *pathname, char *const argv[], char *const envp[]) {
	fn_execve_t real = real_execve ? real_execve : (fn_execve_t)resolve_real("execve");
	if (real == NULL) {
		errno = ENOSYS;
		return -1;
	}
	if (envp == NULL) {
		return real(pathname, argv, envp);
	}

	char **scrubbed = build_scrubbed_envp(envp);
	if (scrubbed == NULL) {
		return real(pathname, argv, envp);
	}
	int ret = real(pathname, argv, scrubbed);
	int saved_errno = errno; /* only reached if execve failed — it doesn't return on success */
	free_scrubbed_envp(scrubbed, envp);
	errno = saved_errno;
	return ret;
}

/* Independent-audit finding (see this section's header comment): glibc's posix_spawn()/
 * posix_spawnp() do not necessarily route through the public execve() symbol internally
 * (a well-known LD_PRELOAD limitation), and GLib's g_spawn_*() — what gnome-shell actually
 * uses for .desktop Exec= launches — can take that fast path. Interposing both here too,
 * same scrub logic, so the fix fires regardless of which path GLib takes at runtime.
 * Unlike execve(), posix_spawn() always returns in the calling process (it creates a
 * separate child rather than replacing the caller's own image), so the scrubbed envp can
 * be freed unconditionally after the call, no success/failure branch needed. */
int posix_spawn(pid_t *pid, const char *path, const posix_spawn_file_actions_t *file_actions,
		const posix_spawnattr_t *attrp, char *const argv[], char *const envp[]) {
	fn_posix_spawn_t real = real_posix_spawn ? real_posix_spawn
		: (fn_posix_spawn_t)resolve_real("posix_spawn");
	if (real == NULL) {
		return ENOSYS; /* posix_spawn reports errors as its return value, not via errno */
	}
	if (envp == NULL) {
		return real(pid, path, file_actions, attrp, argv, envp);
	}
	char **scrubbed = build_scrubbed_envp(envp);
	if (scrubbed == NULL) {
		return real(pid, path, file_actions, attrp, argv, envp);
	}
	int ret = real(pid, path, file_actions, attrp, argv, scrubbed);
	free_scrubbed_envp(scrubbed, envp);
	return ret;
}

int posix_spawnp(pid_t *pid, const char *file, const posix_spawn_file_actions_t *file_actions,
		const posix_spawnattr_t *attrp, char *const argv[], char *const envp[]) {
	fn_posix_spawn_t real = real_posix_spawnp ? real_posix_spawnp
		: (fn_posix_spawn_t)resolve_real("posix_spawnp");
	if (real == NULL) {
		return ENOSYS;
	}
	if (envp == NULL) {
		return real(pid, file, file_actions, attrp, argv, envp);
	}
	char **scrubbed = build_scrubbed_envp(envp);
	if (scrubbed == NULL) {
		return real(pid, file, file_actions, attrp, argv, envp);
	}
	int ret = real(pid, file, file_actions, attrp, argv, scrubbed);
	free_scrubbed_envp(scrubbed, envp);
	return ret;
}
