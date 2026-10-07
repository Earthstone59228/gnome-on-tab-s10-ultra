// pv_fence.c — PVFENCE=5 only. Build: gcc -O2 -shared -fPIC -o pv_fence.so pv_fence.c -ldl
#define _GNU_SOURCE
#include <vulkan/vulkan.h>
#include <dlfcn.h>
#include <string.h>
static PFN_vkCmdBeginRendering r_begin, r_begin_khr; static PFN_vkCmdPipelineBarrier r_bar;
static PFN_vkGetDeviceProcAddr r_gdpa; static PFN_vkGetInstanceProcAddr r_gipa;
static void fence(VkCommandBuffer cb, const VkRenderingInfo *i) {
  if (i->flags & VK_RENDERING_RESUMING_BIT) return;             /* no sync cmds between suspend/resume */
  if (!r_bar) r_bar = (PFN_vkCmdPipelineBarrier)dlsym(RTLD_NEXT, "vkCmdPipelineBarrier");
  if (!r_bar) return;
  VkMemoryBarrier mb = { VK_STRUCTURE_TYPE_MEMORY_BARRIER, 0, 0, 0 };   /* exec-only; set access if the falsifier says "cache" */
  r_bar(cb, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT |
            VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT,
        VK_PIPELINE_STAGE_DRAW_INDIRECT_BIT | VK_PIPELINE_STAGE_VERTEX_INPUT_BIT | VK_PIPELINE_STAGE_VERTEX_SHADER_BIT,
        0, 1, &mb, 0, NULL, 0, NULL);
}
static VKAPI_ATTR void VKAPI_CALL w_begin(VkCommandBuffer cb, const VkRenderingInfo *i) { fence(cb, i); r_begin(cb, i); }
static VKAPI_ATTR void VKAPI_CALL w_begin_khr(VkCommandBuffer cb, const VkRenderingInfo *i) { fence(cb, i); r_begin_khr(cb, i); }
static PFN_vkVoidFunction hook(const char *n, PFN_vkVoidFunction p) {
  if (!p || !n) return p;
  if (!strcmp(n, "vkCmdBeginRendering"))    { r_begin = (void *)p;     return (PFN_vkVoidFunction)w_begin; }
  if (!strcmp(n, "vkCmdBeginRenderingKHR")) { r_begin_khr = (void *)p; return (PFN_vkVoidFunction)w_begin_khr; }
  return p;
}
VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(VkDevice d, const char *n) {
  if (!r_gdpa) r_gdpa = (PFN_vkGetDeviceProcAddr)dlsym(RTLD_NEXT, "vkGetDeviceProcAddr");
  return r_gdpa ? hook(n, r_gdpa(d, n)) : NULL;
}
VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(VkInstance i, const char *n) {
  if (!r_gipa) r_gipa = (PFN_vkGetInstanceProcAddr)dlsym(RTLD_NEXT, "vkGetInstanceProcAddr");
  if (!r_gipa) return NULL;
  if (n && !strcmp(n, "vkGetDeviceProcAddr")) { if (!r_gdpa) r_gdpa = (void *)r_gipa(i, n); return (PFN_vkVoidFunction)vkGetDeviceProcAddr; }
  return hook(n, r_gipa(i, n));
}
VKAPI_ATTR void VKAPI_CALL vkCmdBeginRendering(VkCommandBuffer cb, const VkRenderingInfo *i) {
  if (!r_begin) r_begin = (PFN_vkCmdBeginRendering)dlsym(RTLD_NEXT, "vkCmdBeginRendering");
  fence(cb, i); r_begin(cb, i);
}
