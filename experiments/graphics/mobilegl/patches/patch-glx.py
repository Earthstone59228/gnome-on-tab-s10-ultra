from pathlib import Path
p=Path('/tmp/mobilegl-20261001/MobileGL/MG_Impl/GLXImpl/GLXImpl.cpp');s=p.read_text()
s=s.replace('#include <Init.h>', '''#include <Init.h>
#include "../GLImpl/Framebuffer/GL_Framebuffer.h"
#include "../GLImpl/Getter/GL_Getter.h"
#include "../GLImpl/Buffer/GL_Buffer.h"
#include "../GLImpl/RenderState/GL_RenderState.h"''')
s=s.replace('int (*Sync)(Display*, int) = nullptr;', '''int (*Sync)(Display*, int) = nullptr;
            decltype(&::XCreateImage) CreateImage = nullptr;
            decltype(&::XCreateGC) CreateGC = nullptr;
            decltype(&::XFreeGC) FreeGC = nullptr;
            decltype(&::XPutImage) PutImage = nullptr;''')
s=s.replace('if (!fns->Valid()) {', '''if (fns->Library) {
                    fns->CreateImage = reinterpret_cast<decltype(fns->CreateImage)>(dlsym(fns->Library, "XCreateImage"));
                    fns->CreateGC = reinterpret_cast<decltype(fns->CreateGC)>(dlsym(fns->Library, "XCreateGC"));
                    fns->FreeGC = reinterpret_cast<decltype(fns->FreeGC)>(dlsym(fns->Library, "XFreeGC"));
                    fns->PutImage = reinterpret_cast<decltype(fns->PutImage)>(dlsym(fns->Library, "XPutImage"));
                }
                if (!fns->Valid()) {''')
s=s.replace('Uint32 Width = 0;', 'EGLConfig Config = nullptr;\n            Uint32 Width = 0;',1)
s=s.replace('        // The backends never query', '''        Bool CopyPresentation() {
            const char* value = std::getenv("MOBILEGL_X11_COPY");
            return value && std::strcmp(value, "1") == 0;
        }

        // The backends never query''')
mark='''            if (EGLImpl::ResizePlatformWindowSurface(surface.Display, surface.Surface,'''
s=s.replace(mark, '''            if (CopyPresentation()) {
                const EGLint attrs[] = {EGL_WIDTH, static_cast<EGLint>(width),
                                        EGL_HEIGHT, static_cast<EGLint>(height), EGL_NONE};
                EGLSurface replacement = EGLImpl::CreatePbufferSurface(surface.Display, surface.Config, attrs);
                if (replacement == EGL_NO_SURFACE) return;
                EGLSurface previous = surface.Surface;
                surface.Surface = replacement;
                surface.Width = width;
                surface.Height = height;
                if (t_current.Draw == drawable) {
                    auto* current = TryGetContext(t_current.Context);
                    if (current) EGLImpl::MakeCurrent(surface.Display, replacement, replacement, current->Context);
                }
                EGLImpl::DestroySurface(surface.Display, previous);
                return;
            }
            if (EGLImpl::ResizePlatformWindowSurface(surface.Display, surface.Surface,''')
s=s.replace('''            EGLSurface surface = EGLImpl::CreatePlatformWindowSurface(
                context.Display, context.Config, reinterpret_cast<void*>(drawable), attribs);''', '''            const EGLint pbufferAttribs[] = {EGL_WIDTH, static_cast<EGLint>(width),
                                             EGL_HEIGHT, static_cast<EGLint>(height), EGL_NONE};
            EGLSurface surface = CopyPresentation()
                ? EGLImpl::CreatePbufferSurface(context.Display, context.Config, pbufferAttribs)
                : EGLImpl::CreatePlatformWindowSurface(
                    context.Display, context.Config, reinterpret_cast<void*>(drawable), attribs);''')
s=s.replace('record.Display = context.Display;', 'record.Display = context.Display;\n            record.Config = context.Config;')
s=s.replace('''        SyncSurfaceSize(dpy, drawable, it->second);
        EGLImpl::SwapBuffers(it->second.Display, it->second.Surface);''', '''        auto& surface = it->second;
        if (CopyPresentation()) {
            if (t_current.Draw != drawable) return;
            const auto& x11 = X11();
            if (!x11.CreateImage || !x11.CreateGC || !x11.PutImage || !x11.FreeGC) return;
            GLXDrawableHandle root; int x, y; unsigned int w,h,border,depth;
            if (!x11.GetGeometry(dpy, drawable, &root, &x, &y, &w, &h, &border, &depth)) return;
            auto* visual = static_cast<Visual*>(x11.GetDefaultVisual(dpy, x11.GetDefaultScreen(dpy)));
            // XImage handles server byte order, masks, row pitch and depth. GLES
            // readback is bottom-up; the X image must be top-down.
            XImage* image = x11.CreateImage(dpy, visual, depth, 2 /* ZPixmap */, 0, nullptr,
                                            surface.Width, surface.Height, 32, 0);
            if (!image) return;
            image->data = static_cast<char*>(std::calloc(image->bytes_per_line, surface.Height));
            if (!image->data) { image->f.destroy_image(image); return; }
            std::vector<unsigned char> rgba(static_cast<size_t>(surface.Width) * surface.Height * 4);
            GLint framebuffer, packBuffer, alignment, rowLength, skipRows, skipPixels;
            GLImpl::GetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &framebuffer);
            GLImpl::GetIntegerv(GL_PIXEL_PACK_BUFFER_BINDING, &packBuffer);
            GLImpl::GetIntegerv(GL_PACK_ALIGNMENT, &alignment);
            GLImpl::GetIntegerv(GL_PACK_ROW_LENGTH, &rowLength);
            GLImpl::GetIntegerv(GL_PACK_SKIP_ROWS, &skipRows);
            GLImpl::GetIntegerv(GL_PACK_SKIP_PIXELS, &skipPixels);
            GLImpl::BindFramebuffer(GL_READ_FRAMEBUFFER, 0);
            GLImpl::BindBuffer(GL_PIXEL_PACK_BUFFER, 0);
            GLImpl::PixelStorei(GL_PACK_ALIGNMENT, 1);
            GLImpl::PixelStorei(GL_PACK_ROW_LENGTH, 0);
            GLImpl::PixelStorei(GL_PACK_SKIP_ROWS, 0);
            GLImpl::PixelStorei(GL_PACK_SKIP_PIXELS, 0);
            GLImpl::ReadPixels(0, 0, surface.Width, surface.Height, GL_RGBA, GL_UNSIGNED_BYTE, rgba.data());
            GLImpl::PixelStorei(GL_PACK_ALIGNMENT, alignment);
            GLImpl::PixelStorei(GL_PACK_ROW_LENGTH, rowLength);
            GLImpl::PixelStorei(GL_PACK_SKIP_ROWS, skipRows);
            GLImpl::PixelStorei(GL_PACK_SKIP_PIXELS, skipPixels);
            GLImpl::BindBuffer(GL_PIXEL_PACK_BUFFER, packBuffer);
            GLImpl::BindFramebuffer(GL_READ_FRAMEBUFFER, framebuffer);
            auto channel = [](unsigned char value, unsigned long mask) {
                if (!mask) return 0UL;
                unsigned int shift = std::countr_zero(mask);
                return ((static_cast<unsigned long>(value) * (mask >> shift) + 127) / 255 << shift) & mask;
            };
            for (Uint32 row=0; row<surface.Height; ++row) {
                const auto* src = rgba.data() + static_cast<size_t>(surface.Height-1-row)*surface.Width*4;
                for (Uint32 col=0; col<surface.Width; ++col) {
                    unsigned long pixel = channel(src[col*4], image->red_mask) |
                        channel(src[col*4+1], image->green_mask) | channel(src[col*4+2], image->blue_mask);
                    image->f.put_pixel(image,col,row,pixel);
                }
            }
            GC gc = x11.CreateGC(dpy, drawable, 0, nullptr);
            if (gc) {
                x11.PutImage(dpy, drawable, gc, image, 0, 0, 0, 0, surface.Width, surface.Height);
                x11.FreeGC(dpy, gc);
            }
            image->f.destroy_image(image);
            if (x11.Sync) x11.Sync(dpy, 0);
            if (std::getenv("MOBILEGL_X11_COPY_TRACE")) {
                static unsigned long frame = 0;
                if ((frame++ % 60) == 0) {
                    unsigned long hash = 2166136261UL;
                    for (auto v : rgba) hash = (hash ^ v) * 16777619UL;
                    std::fprintf(stderr, "MOBILEGL_X11_COPY frame=%lu size=%ux%u hash=%lx renderer=Mali\\n",
                                 frame, surface.Width, surface.Height, hash);
                }
            }
        }
        EGLImpl::SwapBuffers(surface.Display, surface.Surface);
        SyncSurfaceSize(dpy, drawable, surface);''')
p.write_text(s)
