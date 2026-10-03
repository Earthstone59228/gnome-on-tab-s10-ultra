/* Stub gbm.h for the fake-libgbm shim build (recreated 2026-09-20; the
 * original was lost with /tmp cleanup). The shim defines its own struct
 * gbm_device/gbm_bo/gbm_surface layouts internally, so the real mesa libgbm
 * header must NOT be included (struct redefinitions). What the shim needs
 * from gbm.h: the GBM_FORMAT_* fourccs (identical to DRM_FORMAT_* values)
 * and union gbm_bo_handle — both reproduced verbatim from the real header. */
#ifndef __STUB_GBM_H__
#define __STUB_GBM_H__
#include <stdint.h>

#define __gbm_fourcc_code(a, b, c, d) ((uint32_t)(a) | ((uint32_t)(b) << 8) | \
	((uint32_t)(c) << 16) | ((uint32_t)(d) << 24))

#define GBM_FORMAT_XRGB8888 __gbm_fourcc_code('X', 'R', '2', '4')
#define GBM_FORMAT_ARGB8888 __gbm_fourcc_code('A', 'R', '2', '4')

union gbm_bo_handle {
	void *ptr;
	int32_t s32;
	uint32_t u32;
	int64_t s64;
	uint64_t u64;
};
#endif
