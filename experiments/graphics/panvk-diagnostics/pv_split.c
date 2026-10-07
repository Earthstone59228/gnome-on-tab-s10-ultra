/* pv_split.c -- LD_PRELOAD Vulkan interposer for PanVK (2026-10-06, UN-AUDITED).
 *
 * Problem: PanVK reports provokingVertexModePerPipeline = VK_FALSE (it fixes the provoking-vertex
 * mode once per render pass, from the first draw, and patches the tiler/FB descriptors).  Blender's
 * Vulkan backend binds pipelines with FIRST_VERTEX and LAST_VERTEX modes inside the same dynamic
 * rendering instance (validation: VUID-vkCmdBindPipeline-pipelineBindPoint-04881).  Flat varyings
 * of the UI widget shader then come from the wrong vertex -> garbage widgets on the Mali only.
 *
 * Modes (env PVSPLIT, default "split"):
 *   off         pass-through
 *   log         count mismatches only
 *   force_last  rewrite every pipeline to LAST_VERTEX (cheap, wrong for pipelines needing FIRST)
 *   force_first rewrite every pipeline to FIRST_VERTEX
 *   split       end + restart the dynamic rendering (loadOp LOAD) when a draw needs a different
 *               mode than the current rendering instance
 * PVSPLIT_LOG=1 prints the first events; statistics are printed at exit.
 */
#define _GNU_SOURCE
#include <vulkan/vulkan.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { PV_UNSET = 0, PV_FIRST = 1, PV_LAST = 2 };
enum { M_OFF, M_LOG, M_FORCE_LAST, M_FORCE_FIRST, M_SPLIT, M_SPLITALL, M_SPLITPIPE };

static int cfg_mode = -1, cfg_log;
static PFN_vkGetDeviceProcAddr real_gdpa;
static PFN_vkGetInstanceProcAddr real_gipa;
static pthread_mutex_t lk = PTHREAD_MUTEX_INITIALIZER;

static PFN_vkCmdBindPipeline r_bind;
static PFN_vkCmdBeginRendering r_begin;
static PFN_vkCmdEndRendering r_end;
static PFN_vkCmdPipelineBarrier r_barrier;
static PFN_vkBeginCommandBuffer r_begincb;
static PFN_vkCreateGraphicsPipelines r_create;
static PFN_vkCmdDraw r_draw;
static PFN_vkCmdDrawIndexed r_drawi;
static PFN_vkCmdDrawIndirect r_drawind;
static PFN_vkCmdDrawIndexedIndirect r_drawiind;
static PFN_vkCmdDrawIndirectCount r_drawindc;
static PFN_vkCmdDrawIndexedIndirectCount r_drawiindc;

static PFN_vkCmdPushConstants r_pc;
static PFN_vkCmdBindDescriptorSets r_bds;
static PFN_vkCmdBindVertexBuffers r_bvb;
static PFN_vkCmdBindIndexBuffer r_bib;
static unsigned replay_mask; /* 1 ds, 2 pc, 4 vb, 8 ib, 16 pipe */
static int no_barrier, fence_mode;
static void scoped_barrier(VkCommandBuffer cb, int after_end) {
    if (!r_barrier) r_barrier = (PFN_vkCmdPipelineBarrier)dlsym(RTLD_NEXT, "vkCmdPipelineBarrier");
    if (!r_barrier) return;
    const VkPipelineStageFlags frag = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT | VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
    const VkPipelineStageFlags vt = VK_PIPELINE_STAGE_DRAW_INDIRECT_BIT | VK_PIPELINE_STAGE_VERTEX_INPUT_BIT | VK_PIPELINE_STAGE_VERTEX_SHADER_BIT;
    const VkPipelineStageFlags xc = VK_PIPELINE_STAGE_TRANSFER_BIT | VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT;
    const VkAccessFlags all = VK_ACCESS_MEMORY_WRITE_BIT | VK_ACCESS_MEMORY_READ_BIT;
    VkMemoryBarrier mb = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = all, .dstAccessMask = all};
    VkPipelineStageFlags src, dst;
    if (fence_mode & 4) { src = frag; dst = vt; }            /* graphics only: previous fragment -> next vertex/tiler */
    else if (!after_end) { src = xc; dst = frag | vt; }      /* before begin: transfer/compute -> graphics */
    else { src = frag | vt; dst = xc; }                       /* after end: graphics -> transfer/compute */
    r_barrier(cb, src, dst, 0, 1, &mb, 0, NULL, 0, NULL);
}
static void full_barrier(VkCommandBuffer cb) {
    if (!r_barrier) r_barrier = (PFN_vkCmdPipelineBarrier)dlsym(RTLD_NEXT, "vkCmdPipelineBarrier");
    if (!r_barrier) return;
    VkMemoryBarrier mb = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = VK_ACCESS_MEMORY_WRITE_BIT | VK_ACCESS_MEMORY_READ_BIT, .dstAccessMask = VK_ACCESS_MEMORY_WRITE_BIT | VK_ACCESS_MEMORY_READ_BIT};
    r_barrier(cb, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, 0, 1, &mb, 0, NULL, 0, NULL);
}
static unsigned long st_pipes, st_draws, st_mismatch, st_splits, st_unsplittable, st_passes;

static void *sym(const char *n) {
    void *p = dlsym(RTLD_NEXT, n);
    if (!p) { fprintf(stderr, "pv_split: missing %s\n", n); abort(); }
    return p;
}

static void init_cfg(void) {
    if (cfg_mode >= 0) return;
    const char *m = getenv("PVSPLIT");
    cfg_mode = M_SPLIT;
    if (m) {
        if (!strcmp(m, "off")) cfg_mode = M_OFF;
        else if (!strcmp(m, "log")) cfg_mode = M_LOG;
        else if (!strcmp(m, "force_last")) cfg_mode = M_FORCE_LAST;
        else if (!strcmp(m, "force_first")) cfg_mode = M_FORCE_FIRST;
        else if (!strcmp(m, "splitall")) cfg_mode = M_SPLITALL;   /* diagnostic: serialise every draw */
        else if (!strcmp(m, "splitpipe")) cfg_mode = M_SPLITPIPE; /* diagnostic: serialise on pipeline change */
    }
    const char *rp = getenv("PVREPLAY");
    if (rp) { if (strstr(rp, "ds")) replay_mask |= 1; if (strstr(rp, "pc")) replay_mask |= 2; if (strstr(rp, "vb")) replay_mask |= 4; if (strstr(rp, "ib")) replay_mask |= 8; if (strstr(rp, "pipe")) replay_mask |= 16; }
    const char *fm = getenv("PVFENCE"); fence_mode = fm ? atoi(fm) : 0; /* bit0: ALL->ALL barrier before every BeginRendering, bit1: after every EndRendering (3 = both) */
    const char *nb = getenv("PVNOBARRIER"); no_barrier = nb && nb[0] == '1';
    const char *l = getenv("PVSPLIT_LOG");
    cfg_log = l && l[0] == '1';
}

static void report(void) {
    fprintf(stderr, "pv_split: mode=%d pipelines=%lu draws=%lu passes=%lu mismatches=%lu splits=%lu unsplittable=%lu\n",
            cfg_mode, st_pipes, st_draws, st_passes, st_mismatch, st_splits, st_unsplittable);
}
__attribute__((destructor)) static void fini(void) { if (cfg_mode > M_OFF) report(); }
__attribute__((constructor)) static void ctor(void) { init_cfg(); fprintf(stderr, "pv_split: loaded, mode=%d\n", cfg_mode); }

/* ---- pipeline -> provoking mode map ---- */
#define PM_SIZE (1u << 18)
static struct { uint64_t k; uint8_t m; } pmap[PM_SIZE];

static void pm_set(uint64_t k, int m) {
    uint32_t h = (uint32_t)((k * 0x9E3779B97F4A7C15ull) >> 46) & (PM_SIZE - 1);
    for (uint32_t i = 0; i < 64; i++) {
        uint32_t s = (h + i) & (PM_SIZE - 1);
        if (pmap[s].k == k || pmap[s].k == 0) { pmap[s].k = k; pmap[s].m = (uint8_t)m; return; }
    }
}
static int pm_get(uint64_t k) {
    uint32_t h = (uint32_t)((k * 0x9E3779B97F4A7C15ull) >> 46) & (PM_SIZE - 1);
    for (uint32_t i = 0; i < 64; i++) {
        uint32_t s = (h + i) & (PM_SIZE - 1);
        if (pmap[s].k == k) return pmap[s].m;
        if (pmap[s].k == 0) return PV_UNSET;
    }
    return PV_UNSET;
}

/* ---- per command buffer state ---- */
typedef struct {
    VkRenderingInfo info;
    VkRenderingAttachmentInfo color[8], depth, stencil;
} SavedPass;

typedef struct {
    VkCommandBuffer cb;
    int in_render, splittable, pass_mode, bound_mode, converted, pass_draws;
    uint64_t bound_pipe, last_pipe;
    /* replayable state */
    VkPipeline gpipe;
    VkPipelineLayout pc_layout; VkShaderStageFlags pc_stages; uint32_t pc_off, pc_size; uint8_t pc_data[256]; int pc_valid;
    VkPipelineLayout ds_layout; uint32_t ds_first, ds_count; VkDescriptorSet ds_sets[4]; int ds_valid;
    uint32_t vb_first, vb_count; VkBuffer vb_buf[16]; VkDeviceSize vb_off[16]; int vb_valid;
    VkBuffer ib_buf; VkDeviceSize ib_off; VkIndexType ib_type; int ib_valid;
    uint64_t sig;
    SavedPass sp;
} CbState;

#define CB_SLOTS 512
static CbState cbs[CB_SLOTS];

static CbState *cb_get(VkCommandBuffer cb) {
    uint32_t h = (uint32_t)(((uintptr_t)cb >> 4) * 2654435761u) & (CB_SLOTS - 1);
    for (uint32_t i = 0; i < CB_SLOTS; i++) {
        CbState *s = &cbs[(h + i) & (CB_SLOTS - 1)];
        if (s->cb == cb) return s;
        if (!s->cb) { s->cb = cb; return s; }
    }
    return NULL;
}

/* signatures of rendering setups known to mix provoking modes */
static uint64_t need_sig[256];
static int sig_known(uint64_t sig) {
    for (int i = 0; i < 256; i++) { if (need_sig[i] == sig) return 1; if (!need_sig[i]) return 0; }
    return 0;
}
static void sig_add(uint64_t sig) {
    for (int i = 0; i < 256; i++) { if (need_sig[i] == sig) return; if (!need_sig[i]) { need_sig[i] = sig; return; } }
}

static int mode_of_create(const VkGraphicsPipelineCreateInfo *ci, VkPipelineRasterizationProvokingVertexStateCreateInfoEXT **out) {
    *out = NULL;
    if (!ci->pRasterizationState) return PV_FIRST;
    for (const VkBaseInStructure *p = ci->pRasterizationState->pNext; p; p = p->pNext) {
        if (p->sType == VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_PROVOKING_VERTEX_STATE_CREATE_INFO_EXT) {
            *out = (VkPipelineRasterizationProvokingVertexStateCreateInfoEXT *)p;
            return (*out)->provokingVertexMode == VK_PROVOKING_VERTEX_MODE_LAST_VERTEX_EXT ? PV_LAST : PV_FIRST;
        }
    }
    return PV_FIRST;
}

static VKAPI_ATTR VkResult VKAPI_CALL w_create(VkDevice d, VkPipelineCache c, uint32_t n,
                                               const VkGraphicsPipelineCreateInfo *ci, const VkAllocationCallbacks *a,
                                               VkPipeline *out) {
    int modes[64];
    if (cfg_mode == M_FORCE_LAST || cfg_mode == M_FORCE_FIRST) {
        for (uint32_t i = 0; i < n; i++) {
            VkPipelineRasterizationProvokingVertexStateCreateInfoEXT *pv;
            mode_of_create(&ci[i], &pv);
            if (pv) pv->provokingVertexMode = cfg_mode == M_FORCE_LAST ? VK_PROVOKING_VERTEX_MODE_LAST_VERTEX_EXT
                                                                       : VK_PROVOKING_VERTEX_MODE_FIRST_VERTEX_EXT;
        }
    }
    for (uint32_t i = 0; i < n && i < 64; i++) {
        VkPipelineRasterizationProvokingVertexStateCreateInfoEXT *pv;
        modes[i] = mode_of_create(&ci[i], &pv);
    }
    VkResult r = r_create(d, c, n, ci, a, out);
    pthread_mutex_lock(&lk);
    for (uint32_t i = 0; i < n && i < 64; i++)
        if (out[i]) { pm_set((uint64_t)(uintptr_t)out[i], modes[i]); st_pipes++; }
    pthread_mutex_unlock(&lk);
    return r;
}

static VKAPI_ATTR VkResult VKAPI_CALL w_begincb(VkCommandBuffer cb, const VkCommandBufferBeginInfo *bi) {
    pthread_mutex_lock(&lk);
    CbState *s = cb_get(cb);
    if (s) { s->in_render = 0; s->bound_mode = PV_UNSET; }
    pthread_mutex_unlock(&lk);
    return r_begincb(cb, bi);
}

static VKAPI_ATTR void VKAPI_CALL w_bind(VkCommandBuffer cb, VkPipelineBindPoint bp, VkPipeline p) {
    if (bp == VK_PIPELINE_BIND_POINT_GRAPHICS && cfg_mode > M_OFF) {
        pthread_mutex_lock(&lk);
        int m = pm_get((uint64_t)(uintptr_t)p);
        CbState *s = cb_get(cb);
        if (s) { s->bound_mode = m; s->bound_pipe = (uint64_t)(uintptr_t)p; s->gpipe = p; }
        pthread_mutex_unlock(&lk);
    }
    r_bind(cb, bp, p);
}

static void copy_att(VkRenderingAttachmentInfo *dst, const VkRenderingAttachmentInfo *src, int convert, int *bad) {
    *dst = *src;
    if (src->pNext) *bad = 1;
    dst->pNext = NULL;
    if (convert && dst->storeOp == VK_ATTACHMENT_STORE_OP_DONT_CARE) dst->storeOp = VK_ATTACHMENT_STORE_OP_STORE;
}

static VKAPI_ATTR void VKAPI_CALL w_begin(VkCommandBuffer cb, const VkRenderingInfo *info) {
    if (fence_mode & 1) { if (fence_mode & 12) scoped_barrier(cb, 0); else full_barrier(cb); }
    if (cfg_mode != M_SPLIT && cfg_mode != M_SPLITALL && cfg_mode != M_SPLITPIPE) { r_begin(cb, info); if (cfg_mode > M_OFF) { pthread_mutex_lock(&lk); CbState *s = cb_get(cb); if (s) { s->in_render = 1; s->pass_mode = PV_UNSET; st_passes++; } pthread_mutex_unlock(&lk); } return; }
    pthread_mutex_lock(&lk);
    CbState *s = cb_get(cb);
    if (!s) { pthread_mutex_unlock(&lk); r_begin(cb, info); return; }
    int bad = (info->pNext != NULL) || info->flags != 0 || info->colorAttachmentCount > 8;
    uint64_t sig = 1469598103934665603ull;
    sig = (sig ^ info->renderArea.extent.width) * 1099511628211ull;
    sig = (sig ^ info->renderArea.extent.height) * 1099511628211ull;
    if (!bad) {
        for (uint32_t i = 0; i < info->colorAttachmentCount; i++)
            sig = (sig ^ (uint64_t)(uintptr_t)info->pColorAttachments[i].imageView) * 1099511628211ull;
        if (info->pDepthAttachment) sig = (sig ^ (uint64_t)(uintptr_t)info->pDepthAttachment->imageView) * 1099511628211ull;
    }
    int convert = sig_known(sig) || cfg_mode == M_SPLITALL || cfg_mode == M_SPLITPIPE;
    SavedPass *sp = &s->sp;
    sp->info = *info;
    sp->info.pNext = NULL;
    if (!bad) {
        for (uint32_t i = 0; i < info->colorAttachmentCount; i++) copy_att(&sp->color[i], &info->pColorAttachments[i], convert, &bad);
        sp->info.pColorAttachments = sp->color;
        if (info->pDepthAttachment) { copy_att(&sp->depth, info->pDepthAttachment, convert, &bad); sp->info.pDepthAttachment = &sp->depth; }
        if (info->pStencilAttachment) { copy_att(&sp->stencil, info->pStencilAttachment, convert, &bad); sp->info.pStencilAttachment = &sp->stencil; }
    }
    s->in_render = 1; s->splittable = !bad; s->pass_mode = PV_UNSET; s->sig = sig; s->converted = convert; s->pass_draws = 0; s->last_pipe = 0;
    st_passes++;
    pthread_mutex_unlock(&lk);
    r_begin(cb, bad ? info : &sp->info);
}

static VKAPI_ATTR void VKAPI_CALL w_end(VkCommandBuffer cb) {
    if (cfg_mode > M_OFF) { pthread_mutex_lock(&lk); CbState *s = cb_get(cb); if (s) s->in_render = 0; pthread_mutex_unlock(&lk); }
    r_end(cb);
    if (fence_mode & 2) { if (fence_mode & 8) scoped_barrier(cb, 1); else full_barrier(cb); }
}

static int ensure_ptrs(void) {
    if (!r_begin) r_begin = (PFN_vkCmdBeginRendering)dlsym(RTLD_NEXT, "vkCmdBeginRendering");
    if (!r_end) r_end = (PFN_vkCmdEndRendering)dlsym(RTLD_NEXT, "vkCmdEndRendering");
    if (!r_barrier) r_barrier = (PFN_vkCmdPipelineBarrier)dlsym(RTLD_NEXT, "vkCmdPipelineBarrier");
    if (!r_begin || !r_end || !r_barrier) {
        static int once; if (!once) { once = 1; fprintf(stderr, "pv_split: missing begin=%p end=%p barrier2=%p, splitting disabled\n", (void *)r_begin, (void *)r_end, (void *)r_barrier); }
        return 0;
    }
    return 1;
}

static void pre_draw_core(VkCommandBuffer cb) {
    if (cfg_mode <= M_OFF || cfg_mode == M_FORCE_LAST || cfg_mode == M_FORCE_FIRST) return;
    pthread_mutex_lock(&lk);
    st_draws++;
    if ((st_draws % 20000) == 0) report();
    CbState *s = cb_get(cb);
    if (s && s->in_render && (cfg_mode == M_SPLITALL || cfg_mode == M_SPLITPIPE)) {
        int need = s->pass_draws > 0 && (cfg_mode == M_SPLITALL || s->bound_pipe != s->last_pipe);
        s->pass_draws++; s->last_pipe = s->bound_pipe;
        if (!need || !s->splittable || !ensure_ptrs()) { pthread_mutex_unlock(&lk); return; }
        SavedPass res = s->sp;
        for (uint32_t i = 0; i < res.info.colorAttachmentCount; i++) res.color[i].loadOp = VK_ATTACHMENT_LOAD_OP_LOAD;
        res.info.pColorAttachments = res.color;
        if (res.info.pDepthAttachment) { res.depth.loadOp = VK_ATTACHMENT_LOAD_OP_LOAD; res.info.pDepthAttachment = &res.depth; }
        if (res.info.pStencilAttachment) { res.stencil.loadOp = VK_ATTACHMENT_LOAD_OP_LOAD; res.info.pStencilAttachment = &res.stencil; }
        st_splits++;
        pthread_mutex_unlock(&lk);
        r_end(cb);
        VkMemoryBarrier mb = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = VK_ACCESS_MEMORY_WRITE_BIT | VK_ACCESS_MEMORY_READ_BIT, .dstAccessMask = VK_ACCESS_MEMORY_WRITE_BIT | VK_ACCESS_MEMORY_READ_BIT};
        if (!no_barrier) r_barrier(cb, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, 0, 1, &mb, 0, NULL, 0, NULL);
        r_begin(cb, &res.info);
        return;
    }
    if (!s || !s->in_render || s->bound_mode == PV_UNSET) { pthread_mutex_unlock(&lk); return; }
    if (s->pass_mode == PV_UNSET) { s->pass_mode = s->bound_mode; pthread_mutex_unlock(&lk); return; }
    if (s->pass_mode == s->bound_mode) { pthread_mutex_unlock(&lk); return; }
    st_mismatch++;
    if (cfg_log && st_mismatch <= 20) fprintf(stderr, "pv_split: mismatch pass=%d bound=%d splittable=%d converted=%d\n", s->pass_mode, s->bound_mode, s->splittable, s->converted);
    if (cfg_mode == M_LOG) { pthread_mutex_unlock(&lk); return; }
    sig_add(s->sig);
    if (!s->splittable || !s->converted) {
        /* DONT_CARE stores were not converted for this pass: contents could be lost; learn and skip. */
        st_unsplittable++;
        s->pass_mode = s->bound_mode;
        pthread_mutex_unlock(&lk);
        return;
    }
    if (!ensure_ptrs()) { st_unsplittable++; s->pass_mode = s->bound_mode; pthread_mutex_unlock(&lk); return; }
    SavedPass res = s->sp;
    for (uint32_t i = 0; i < res.info.colorAttachmentCount; i++) res.color[i].loadOp = VK_ATTACHMENT_LOAD_OP_LOAD;
    res.info.pColorAttachments = res.color;
    if (res.info.pDepthAttachment) { res.depth.loadOp = VK_ATTACHMENT_LOAD_OP_LOAD; res.info.pDepthAttachment = &res.depth; }
    if (res.info.pStencilAttachment) { res.stencil.loadOp = VK_ATTACHMENT_LOAD_OP_LOAD; res.info.pStencilAttachment = &res.stencil; }
    s->pass_mode = s->bound_mode;
    st_splits++;
    pthread_mutex_unlock(&lk);

    r_end(cb);
    VkMemoryBarrier mb = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT | VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT, .dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT | VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_READ_BIT | VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT};
    VkPipelineStageFlags fs = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
    r_barrier(cb, fs, fs, 0, 1, &mb, 0, NULL, 0, NULL);
    r_begin(cb, &res.info);
}

static void replay_state(VkCommandBuffer cb) {
    CbState snap;
    pthread_mutex_lock(&lk);
    CbState *s = cb_get(cb);
    if (!s || !s->in_render) { pthread_mutex_unlock(&lk); return; }
    snap = *s;
    pthread_mutex_unlock(&lk);
    if ((replay_mask & 16) && snap.gpipe && r_bind) r_bind(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, snap.gpipe);
    if ((replay_mask & 1) && snap.ds_valid && r_bds) r_bds(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, snap.ds_layout, snap.ds_first, snap.ds_count, snap.ds_sets, 0, NULL);
    if ((replay_mask & 2) && snap.pc_valid && r_pc) r_pc(cb, snap.pc_layout, snap.pc_stages, snap.pc_off, snap.pc_size, snap.pc_data);
    if ((replay_mask & 4) && snap.vb_valid && r_bvb) r_bvb(cb, snap.vb_first, snap.vb_count, snap.vb_buf, snap.vb_off);
    if ((replay_mask & 8) && snap.ib_valid && r_bib) r_bib(cb, snap.ib_buf, snap.ib_off, snap.ib_type);
}
static void pre_draw(VkCommandBuffer cb) {
    pre_draw_core(cb);
    if (replay_mask) replay_state(cb);
}

#define DRAW_WRAP(NAME, PFN, REAL, PARAMS, ARGS) \
    static VKAPI_ATTR void VKAPI_CALL NAME PARAMS { pre_draw(cb); REAL ARGS; }
DRAW_WRAP(w_draw, PFN_vkCmdDraw, r_draw, (VkCommandBuffer cb, uint32_t a, uint32_t b, uint32_t c, uint32_t d), (cb, a, b, c, d))
DRAW_WRAP(w_drawi, PFN_vkCmdDrawIndexed, r_drawi, (VkCommandBuffer cb, uint32_t a, uint32_t b, uint32_t c, int32_t d, uint32_t e), (cb, a, b, c, d, e))
DRAW_WRAP(w_drawind, PFN_vkCmdDrawIndirect, r_drawind, (VkCommandBuffer cb, VkBuffer bu, VkDeviceSize o, uint32_t n, uint32_t s), (cb, bu, o, n, s))
DRAW_WRAP(w_drawiind, PFN_vkCmdDrawIndexedIndirect, r_drawiind, (VkCommandBuffer cb, VkBuffer bu, VkDeviceSize o, uint32_t n, uint32_t s), (cb, bu, o, n, s))
DRAW_WRAP(w_drawindc, PFN_vkCmdDrawIndirectCount, r_drawindc, (VkCommandBuffer cb, VkBuffer bu, VkDeviceSize o, VkBuffer cbuf, VkDeviceSize co, uint32_t m, uint32_t s), (cb, bu, o, cbuf, co, m, s))
DRAW_WRAP(w_drawiindc, PFN_vkCmdDrawIndexedIndirectCount, r_drawiindc, (VkCommandBuffer cb, VkBuffer bu, VkDeviceSize o, VkBuffer cbuf, VkDeviceSize co, uint32_t m, uint32_t s), (cb, bu, o, cbuf, co, m, s))

static PFN_vkVoidFunction hook(const char *name, PFN_vkVoidFunction real) {
    if (!real || !name) return real;
    init_cfg();
    if (cfg_mode == M_OFF) return real;
#define H(N, W, R) if (!strcmp(name, N)) { R = (void *)real; if (cfg_log) fprintf(stderr, "pv_split: hooked %s\n", N); return (PFN_vkVoidFunction)W; }
    H("vkCreateGraphicsPipelines", w_create, r_create)
    H("vkBeginCommandBuffer", w_begincb, r_begincb)
    H("vkCmdBindPipeline", w_bind, r_bind)
    H("vkCmdBeginRendering", w_begin, r_begin)
    H("vkCmdBeginRenderingKHR", w_begin, r_begin)
    H("vkCmdEndRendering", w_end, r_end)
    H("vkCmdEndRenderingKHR", w_end, r_end)
    H("vkCmdDraw", w_draw, r_draw)
    H("vkCmdDrawIndexed", w_drawi, r_drawi)
    H("vkCmdDrawIndirect", w_drawind, r_drawind)
    H("vkCmdDrawIndexedIndirect", w_drawiind, r_drawiind)
    H("vkCmdDrawIndirectCount", w_drawindc, r_drawindc)
    H("vkCmdDrawIndirectCountKHR", w_drawindc, r_drawindc)
    H("vkCmdDrawIndexedIndirectCount", w_drawiindc, r_drawiindc)
    H("vkCmdDrawIndexedIndirectCountKHR", w_drawiindc, r_drawiindc)
#undef H
    return real;
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(VkDevice d, const char *name) {
    if (!real_gdpa) real_gdpa = sym("vkGetDeviceProcAddr");
    PFN_vkVoidFunction p = real_gdpa(d, name);
    return hook(name, p);
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(VkInstance i, const char *name) {
    if (!real_gipa) real_gipa = sym("vkGetInstanceProcAddr");
    if (name && !strcmp(name, "vkGetDeviceProcAddr")) { if (!real_gdpa) real_gdpa = (void *)real_gipa(i, name); return (PFN_vkVoidFunction)vkGetDeviceProcAddr; }
    PFN_vkVoidFunction p = real_gipa(i, name);
    return hook(name, p);
}

/* Direct (linked) calls bypass the proc-addr hooks; cover the exported entry points as well. */
VKAPI_ATTR VkResult VKAPI_CALL vkCreateGraphicsPipelines(VkDevice d, VkPipelineCache c, uint32_t n, const VkGraphicsPipelineCreateInfo *ci, const VkAllocationCallbacks *a, VkPipeline *out) {
    init_cfg();
    if (!r_create) r_create = sym("vkCreateGraphicsPipelines");
    if (cfg_mode == M_OFF) return r_create(d, c, n, ci, a, out);
    return w_create(d, c, n, ci, a, out);
}

/* Core entry points that the application links directly (not via vkGet*ProcAddr). */
#define LAZY(R, N) do { if (!(R)) (R) = sym(N); } while (0)
VKAPI_ATTR VkResult VKAPI_CALL vkBeginCommandBuffer(VkCommandBuffer cb, const VkCommandBufferBeginInfo *bi) { LAZY(r_begincb, "vkBeginCommandBuffer"); return w_begincb(cb, bi); }
VKAPI_ATTR void VKAPI_CALL vkCmdBindPipeline(VkCommandBuffer cb, VkPipelineBindPoint bp, VkPipeline p) { LAZY(r_bind, "vkCmdBindPipeline"); w_bind(cb, bp, p); }
VKAPI_ATTR void VKAPI_CALL vkCmdBeginRendering(VkCommandBuffer cb, const VkRenderingInfo *i) { LAZY(r_begin, "vkCmdBeginRendering"); w_begin(cb, i); }
VKAPI_ATTR void VKAPI_CALL vkCmdEndRendering(VkCommandBuffer cb) { LAZY(r_end, "vkCmdEndRendering"); w_end(cb); }
VKAPI_ATTR void VKAPI_CALL vkCmdDraw(VkCommandBuffer cb, uint32_t a, uint32_t b, uint32_t c, uint32_t d) { LAZY(r_draw, "vkCmdDraw"); w_draw(cb, a, b, c, d); }
VKAPI_ATTR void VKAPI_CALL vkCmdDrawIndexed(VkCommandBuffer cb, uint32_t a, uint32_t b, uint32_t c, int32_t d, uint32_t e) { LAZY(r_drawi, "vkCmdDrawIndexed"); w_drawi(cb, a, b, c, d, e); }
VKAPI_ATTR void VKAPI_CALL vkCmdDrawIndirect(VkCommandBuffer cb, VkBuffer bu, VkDeviceSize o, uint32_t n, uint32_t s) { LAZY(r_drawind, "vkCmdDrawIndirect"); w_drawind(cb, bu, o, n, s); }
VKAPI_ATTR void VKAPI_CALL vkCmdDrawIndexedIndirect(VkCommandBuffer cb, VkBuffer bu, VkDeviceSize o, uint32_t n, uint32_t s) { LAZY(r_drawiind, "vkCmdDrawIndexedIndirect"); w_drawiind(cb, bu, o, n, s); }

static VKAPI_ATTR void VKAPI_CALL w_pc(VkCommandBuffer cb, VkPipelineLayout l, VkShaderStageFlags st, uint32_t off, uint32_t size, const void *v) {
    if (replay_mask & 2) { pthread_mutex_lock(&lk); CbState *s = cb_get(cb); if (s && off + size <= 256) { s->pc_layout = l; s->pc_stages = st; s->pc_off = off; s->pc_size = size; memcpy(s->pc_data, v, size); s->pc_valid = 1; } pthread_mutex_unlock(&lk); }
    r_pc(cb, l, st, off, size, v);
}
static VKAPI_ATTR void VKAPI_CALL w_bds(VkCommandBuffer cb, VkPipelineBindPoint bp, VkPipelineLayout l, uint32_t first, uint32_t n, const VkDescriptorSet *sets, uint32_t dn, const uint32_t *doff) {
    if ((replay_mask & 1) && bp == VK_PIPELINE_BIND_POINT_GRAPHICS) { pthread_mutex_lock(&lk); CbState *s = cb_get(cb); if (s && n <= 4 && dn == 0) { s->ds_layout = l; s->ds_first = first; s->ds_count = n; memcpy(s->ds_sets, sets, n * sizeof(*sets)); s->ds_valid = 1; } else if (s) s->ds_valid = 0; pthread_mutex_unlock(&lk); }
    r_bds(cb, bp, l, first, n, sets, dn, doff);
}
static VKAPI_ATTR void VKAPI_CALL w_bvb(VkCommandBuffer cb, uint32_t first, uint32_t n, const VkBuffer *b, const VkDeviceSize *o) {
    if (replay_mask & 4) { pthread_mutex_lock(&lk); CbState *s = cb_get(cb); if (s && n <= 16) { s->vb_first = first; s->vb_count = n; memcpy(s->vb_buf, b, n * sizeof(*b)); memcpy(s->vb_off, o, n * sizeof(*o)); s->vb_valid = 1; } else if (s) s->vb_valid = 0; pthread_mutex_unlock(&lk); }
    r_bvb(cb, first, n, b, o);
}
static VKAPI_ATTR void VKAPI_CALL w_bib(VkCommandBuffer cb, VkBuffer b, VkDeviceSize o, VkIndexType t) {
    if (replay_mask & 8) { pthread_mutex_lock(&lk); CbState *s = cb_get(cb); if (s) { s->ib_buf = b; s->ib_off = o; s->ib_type = t; s->ib_valid = 1; } pthread_mutex_unlock(&lk); }
    r_bib(cb, b, o, t);
}
VKAPI_ATTR void VKAPI_CALL vkCmdPushConstants(VkCommandBuffer cb, VkPipelineLayout l, VkShaderStageFlags st, uint32_t off, uint32_t size, const void *v) { LAZY(r_pc, "vkCmdPushConstants"); w_pc(cb, l, st, off, size, v); }
VKAPI_ATTR void VKAPI_CALL vkCmdBindDescriptorSets(VkCommandBuffer cb, VkPipelineBindPoint bp, VkPipelineLayout l, uint32_t first, uint32_t n, const VkDescriptorSet *sets, uint32_t dn, const uint32_t *doff) { LAZY(r_bds, "vkCmdBindDescriptorSets"); w_bds(cb, bp, l, first, n, sets, dn, doff); }
VKAPI_ATTR void VKAPI_CALL vkCmdBindVertexBuffers(VkCommandBuffer cb, uint32_t first, uint32_t n, const VkBuffer *b, const VkDeviceSize *o) { LAZY(r_bvb, "vkCmdBindVertexBuffers"); w_bvb(cb, first, n, b, o); }
VKAPI_ATTR void VKAPI_CALL vkCmdBindIndexBuffer(VkCommandBuffer cb, VkBuffer b, VkDeviceSize o, VkIndexType t) { LAZY(r_bib, "vkCmdBindIndexBuffer"); w_bib(cb, b, o, t); }
