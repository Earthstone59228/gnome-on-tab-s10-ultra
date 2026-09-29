/* libui_compat_layer.so for libhybris on gts10u (Android 16, BP4A) — bionic library.
 *
 * libhybris' gralloc.c (upstream, "GRALLOC_COMPAT" / version 2 path, used on every
 * Android >= 10 device) expects a bionic helper library named libui_compat_layer.so that
 * exports the graphic_buffer_allocator_* / graphic_buffer_mapper_* C functions. Upstream
 * source: libhybris/compat/ui/ui_compatibility_layer.cpp, normally built inside the
 * device's AOSP tree (Halium). We have no AOSP tree for this One UI build, and the upstream
 * C++ passes a libc++ std::string by value into GraphicBufferAllocator::allocate(), whose
 * ABI (std::__1 vs the NDK's std::__ndk1) cannot be reproduced safely with the NDK.
 *
 * This file implements the same 7 C entry points with only:
 *   - the public NDK AHardwareBuffer API (allocate / describe / getNativeHandle / release),
 *     the same path gnome_gbm_shim.so already uses live in the compositor;
 *   - four NON-virtual GraphicBufferMapper member functions whose mangled names were
 *     checked against the device's /system/lib64/libui.so export table (nm -D, md5
 *     71101e7661641d4386886ca63973923c) and whose parameters contain no std:: types;
 *   - the exported Singleton<GraphicBufferMapper>::sInstance pointer (GraphicBuffer's
 *     constructor creates the singleton, so one tiny AHardwareBuffer allocation guarantees
 *     it exists; we never construct or size the class ourselves).
 * Semantics follow upstream ui_compatibility_layer.cpp function-for-function.
 */
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <android/hardware_buffer.h>
#include <android/log.h>

typedef struct native_handle native_handle_t;
typedef const native_handle_t *buffer_handle_t;
typedef int32_t status_t;

/* LL-NDK (vndk/hardware_buffer.h), exported by libnativewindow.so; used live by gnome_gbm_shim. */
extern const native_handle_t *AHardwareBuffer_getNativeHandle(const AHardwareBuffer *buffer);

#define LOG_TAG "hybris-ui-compat"
#define ALOGW(...) __android_log_print(ANDROID_LOG_WARN, LOG_TAG, __VA_ARGS__)

#define OK_STATUS 0
#define NO_INIT_STATUS (-ENODEV)
#define BAD_VALUE_STATUS (-EINVAL)
#define NO_MEMORY_STATUS (-ENOMEM)

struct android_rect { int32_t left, top, right, bottom; }; /* android::Rect == ARect layout */

/* android::Singleton<android::GraphicBufferMapper>::sInstance (data, exported by libui.so). */
extern void *gbm_singleton_instance
	__asm__("_ZN7android9SingletonINS_19GraphicBufferMapperEE9sInstanceE");

status_t gbm_import_buffer(void *self, buffer_handle_t raw, uint32_t w, uint32_t h,
		uint32_t layers, int32_t format, uint64_t usage, uint32_t stride,
		buffer_handle_t *out)
	__asm__("_ZN7android19GraphicBufferMapper12importBufferEPK13native_handlejjjimjPS3_");
status_t gbm_import_no_validate(void *self, buffer_handle_t raw, buffer_handle_t *out)
	__asm__("_ZN7android19GraphicBufferMapper22importBufferNoValidateEPK13native_handlePS3_");
status_t gbm_free_buffer(void *self, buffer_handle_t handle)
	__asm__("_ZN7android19GraphicBufferMapper10freeBufferEPK13native_handle");
status_t gbm_lock(void *self, buffer_handle_t handle, uint32_t usage,
		const struct android_rect *bounds, void **vaddr)
	__asm__("_ZN7android19GraphicBufferMapper4lockEPK13native_handlejRKNS_4RectEPPv");
/* 2nd arg is android::base::unique_fd*; nullptr = wait for and close the fence itself
 * (android16-release GraphicBufferMapper::unlock). */
status_t gbm_unlock(void *self, buffer_handle_t handle, void *out_fence_or_null)
	__asm__("_ZN7android19GraphicBufferMapper6unlockEPK13native_handlePNS_4base14unique_fd_implINS4_13DefaultCloserEEE");

/* ---- handle -> AHardwareBuffer table for buffers we allocated ---- */
struct owned { buffer_handle_t handle; AHardwareBuffer *ahb; };
static struct owned *g_owned;
static size_t g_owned_len, g_owned_cap;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;

static int owned_add(buffer_handle_t h, AHardwareBuffer *ahb) {
	pthread_mutex_lock(&g_lock);
	if (g_owned_len == g_owned_cap) {
		size_t cap = g_owned_cap ? g_owned_cap * 2 : 32;
		struct owned *n = realloc(g_owned, cap * sizeof(*n));
		if (n == NULL) {
			pthread_mutex_unlock(&g_lock);
			return -1;
		}
		g_owned = n;
		g_owned_cap = cap;
	}
	g_owned[g_owned_len].handle = h;
	g_owned[g_owned_len].ahb = ahb;
	g_owned_len++;
	pthread_mutex_unlock(&g_lock);
	return 0;
}

static AHardwareBuffer *owned_take(buffer_handle_t h) {
	AHardwareBuffer *ahb = NULL;
	pthread_mutex_lock(&g_lock);
	for (size_t i = 0; i < g_owned_len; i++) {
		if (g_owned[i].handle == h) {
			ahb = g_owned[i].ahb;
			g_owned[i] = g_owned[--g_owned_len];
			break;
		}
	}
	pthread_mutex_unlock(&g_lock);
	return ahb;
}

/* GraphicBufferMapper singleton; created as a side effect of any GraphicBuffer. */
static void *mapper(void) {
	void *m = __atomic_load_n(&gbm_singleton_instance, __ATOMIC_ACQUIRE);
	if (m != NULL) {
		return m;
	}
	AHardwareBuffer_Desc d;
	memset(&d, 0, sizeof(d));
	d.width = 1;
	d.height = 1;
	d.layers = 1;
	d.format = AHARDWAREBUFFER_FORMAT_R8G8B8A8_UNORM;
	d.usage = AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE;
	AHardwareBuffer *tmp = NULL;
	if (AHardwareBuffer_allocate(&d, &tmp) == 0 && tmp != NULL) {
		AHardwareBuffer_release(tmp);
	}
	m = __atomic_load_n(&gbm_singleton_instance, __ATOMIC_ACQUIRE);
	if (m == NULL) {
		ALOGW("GraphicBufferMapper singleton still NULL after warm-up allocation");
	}
	return m;
}

status_t graphic_buffer_allocator_allocate(uint32_t width, uint32_t height, int32_t format,
		uint32_t layerCount, uint64_t usage, buffer_handle_t *handle, uint32_t *stride,
		uint64_t graphicBufferId, const char *requestorName) {
	(void)graphicBufferId;
	(void)requestorName;
	if (handle == NULL || stride == NULL) {
		return BAD_VALUE_STATUS;
	}
	AHardwareBuffer_Desc d;
	memset(&d, 0, sizeof(d));
	d.width = width;
	d.height = height;
	d.layers = layerCount ? layerCount : 1;
	d.format = (uint32_t)format; /* HAL_PIXEL_FORMAT_* == AHARDWAREBUFFER_FORMAT_* values */
	/* hybris passes gralloc usage as a 32-bit int into this uint64_t, so bit 31 would
	 * sign-extend into every vendor-high usage bit (audit 2026-09-25). hybris only ever has
	 * 32-bit usage, so mask to the low 32 bits. Same bit values as AHARDWAREBUFFER_USAGE_*. */
	d.usage = usage & 0xffffffffULL;
	if (usage >> 32) {
		ALOGW("allocate: masked usage 0x%llx -> 0x%llx", (unsigned long long)usage,
			(unsigned long long)d.usage);
	}
	AHardwareBuffer *ahb = NULL;
	int r = AHardwareBuffer_allocate(&d, &ahb);
	if (r != 0 || ahb == NULL) {
		ALOGW("AHardwareBuffer_allocate(%ux%u fmt=%d usage=0x%llx) failed: %d", width, height,
			format, (unsigned long long)usage, r);
		return r ? r : NO_MEMORY_STATUS;
	}
	AHardwareBuffer_Desc real;
	memset(&real, 0, sizeof(real));
	AHardwareBuffer_describe(ahb, &real);
	buffer_handle_t h = AHardwareBuffer_getNativeHandle(ahb);
	if (h == NULL || owned_add(h, ahb) != 0) {
		AHardwareBuffer_release(ahb);
		return NO_MEMORY_STATUS;
	}
	*handle = h;
	*stride = real.stride;
	return OK_STATUS;
}

status_t graphic_buffer_allocator_free(buffer_handle_t handle) {
	AHardwareBuffer *ahb = owned_take(handle);
	if (ahb == NULL) {
		/* Not allocated here: hybris' client ServerWaylandBuffer imports its handle but
		 * releases it with was_allocated=1 (wayland_window_common.cpp:596/646). AOSP
		 * GraphicBufferAllocator::free is just mMapper.freeBuffer, so do the same. */
		void *m = mapper();
		return m != NULL ? gbm_free_buffer(m, handle) : NO_INIT_STATUS;
	}
	AHardwareBuffer_release(ahb);
	return OK_STATUS;
}

status_t graphic_buffer_mapper_import_buffer(buffer_handle_t rawHandle, uint32_t width,
		uint32_t height, uint32_t layerCount, int32_t format, uint64_t usage, uint32_t stride,
		buffer_handle_t *outHandle) {
	void *m = mapper();
	if (m == NULL) {
		return NO_INIT_STATUS;
	}
	return gbm_import_buffer(m, rawHandle, width, height, layerCount, format, usage, stride,
		outHandle);
}

status_t graphic_buffer_mapper_import_buffer_no_size(buffer_handle_t rawHandle,
		buffer_handle_t *outHandle) {
	void *m = mapper();
	if (m == NULL) {
		return NO_INIT_STATUS;
	}
	return gbm_import_no_validate(m, rawHandle, outHandle);
}

status_t graphic_buffer_mapper_free_buffer(buffer_handle_t handle) {
	void *m = mapper();
	if (m == NULL) {
		return NO_INIT_STATUS;
	}
	return gbm_free_buffer(m, handle);
}

status_t graphic_buffer_mapper_lock(buffer_handle_t handle, uint32_t usage,
		const struct android_rect *bounds, void **vaddr, int32_t *outBytesPerPixel,
		int32_t *outBytesPerStride) {
	if (outBytesPerPixel) *outBytesPerPixel = -1;
	if (outBytesPerStride) *outBytesPerStride = -1;
	void *m = mapper();
	if (m == NULL || bounds == NULL) {
		return m == NULL ? NO_INIT_STATUS : BAD_VALUE_STATUS;
	}
	return gbm_lock(m, handle, usage, bounds, vaddr);
}

status_t graphic_buffer_mapper_unlock(buffer_handle_t handle) {
	void *m = mapper();
	if (m == NULL) {
		return NO_INIT_STATUS;
	}
	return gbm_unlock(m, handle, NULL);
}
