/* Real desktop GL shader/FBO/resize/compute tests, no display takeover. */
#include <epoxy/egl.h>
#include <epoxy/gl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void fail(const char *s) { fprintf(stderr, "FAIL: %s (GL=%x EGL=%x)\n", s, glGetError(), eglGetError()); exit(1); }
static GLuint shader(GLenum type, const char *src) {
    GLuint s=glCreateShader(type); glShaderSource(s,1,&src,NULL); glCompileShader(s);
    GLint ok=0; glGetShaderiv(s,GL_COMPILE_STATUS,&ok);
    if (!ok) { char log[8192]; glGetShaderInfoLog(s,sizeof log,NULL,log); fprintf(stderr,"%s\n",log); fail("shader compile"); }
    return s;
}
static GLuint program(GLuint a, GLuint b) {
    GLuint p=glCreateProgram(); glAttachShader(p,a); if(b) glAttachShader(p,b); glLinkProgram(p);
    GLint ok=0; glGetProgramiv(p,GL_LINK_STATUS,&ok);
    if(!ok) { char log[8192]; glGetProgramInfoLog(p,sizeof log,NULL,log); fprintf(stderr,"%s\n",log); fail("program link"); }
    glDeleteShader(a); if(b) glDeleteShader(b); return p;
}
static void maps(void) {
    FILE *f=fopen("/proc/self/maps","r"); char line[4096];
    if(!f) return;
    while(fgets(line,sizeof line,f)) if(strstr(line,"panfrost")||strstr(line,"gallium")||strstr(line,"zink")||strstr(line,"vulkan")||strstr(line,"libEGL")) printf("MAP %s",line);
    fclose(f);
}
int main(int argc, char **argv) {
    int want_gpu=argc>1 && !strcmp(argv[1],"gpu");
    PFNEGLGETPLATFORMDISPLAYEXTPROC getdisplay=(void*)eglGetProcAddress("eglGetPlatformDisplayEXT");
    if(!getdisplay) { fprintf(stderr,"FAIL: no platform display entry\n"); return 1; }
    EGLDisplay d=getdisplay(EGL_PLATFORM_SURFACELESS_MESA,NULL,NULL);
    EGLint major,minor;
    if(!eglInitialize(d,&major,&minor)) { fprintf(stderr,"FAIL: EGL initialize %x\n",eglGetError()); return 1; }
    const EGLint ca[]={EGL_SURFACE_TYPE,EGL_PBUFFER_BIT,EGL_RENDERABLE_TYPE,EGL_OPENGL_BIT,EGL_NONE};
    EGLConfig cfg; EGLint n;
    if(!eglChooseConfig(d,ca,&cfg,1,&n)||!n||!eglBindAPI(EGL_OPENGL_API)) { fprintf(stderr,"FAIL: EGL config/API\n"); return 1; }
    EGLint attrs[]={EGL_CONTEXT_MAJOR_VERSION,4,EGL_CONTEXT_MINOR_VERSION,3,EGL_CONTEXT_OPENGL_PROFILE_MASK,EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT,EGL_NONE};
    EGLContext ctx=eglCreateContext(d,cfg,EGL_NO_CONTEXT,attrs);
    int compute=1;
    if(ctx==EGL_NO_CONTEXT) { compute=0; attrs[1]=3;attrs[3]=3;ctx=eglCreateContext(d,cfg,EGL_NO_CONTEXT,attrs); }
    const EGLint pa[]={EGL_WIDTH,16,EGL_HEIGHT,16,EGL_NONE};
    EGLSurface surf=eglCreatePbufferSurface(d,cfg,pa);
    if(ctx==EGL_NO_CONTEXT||surf==EGL_NO_SURFACE||!eglMakeCurrent(d,surf,surf,ctx)) { fprintf(stderr,"FAIL: EGL context/current %x\n",eglGetError());return 1; }
    const char *renderer=(const char*)glGetString(GL_RENDERER);
    printf("EGL=%d.%d\nGL_VERSION=%s\nGL_RENDERER=%s\n",major,minor,glGetString(GL_VERSION),renderer); fflush(stdout);
    maps();
    if(!renderer || !strstr(renderer,"zink")) fail("renderer is not Zink");
    if(want_gpu && (!strstr(renderer,"Mali")||strstr(renderer,"llvmpipe")||strstr(renderer,"lavapipe"))) fail("hardware requested but Mali not selected");
    if(!want_gpu && !strstr(renderer,"llvmpipe") && !strstr(renderer,"lavapipe")) fail("CPU control requested but software backend not selected");
    GLuint p=program(shader(GL_VERTEX_SHADER,"#version 330 core\nvoid main(){vec2 v[3]=vec2[3](vec2(-1,-1),vec2(3,-1),vec2(-1,3));gl_Position=vec4(v[gl_VertexID],0,1);}"),
        shader(GL_FRAGMENT_SHADER,"#version 330 core\nuniform vec2 size;out vec4 c;void main(){bool x=gl_FragCoord.x<size.x/2;bool y=gl_FragCoord.y<size.y/2;c=vec4(x?1:0,y?1:0,x==y?1:0,1);}"));
    GLuint vao,fbo,tex;glGenVertexArrays(1,&vao);glBindVertexArray(vao);glGenFramebuffers(1,&fbo);glBindFramebuffer(GL_FRAMEBUFFER,fbo);
    glGenTextures(1,&tex);glBindTexture(GL_TEXTURE_2D,tex);glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_NEAREST);glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_NEAREST);
    const int sizes[][2]={{64,64},{127,93},{256,128},{64,64}};
    glUseProgram(p);glDisable(GL_DITHER);
    for(unsigned k=0;k<sizeof sizes/sizeof sizes[0];k++) {
        int w=sizes[k][0],h=sizes[k][1];
        glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA8,w,h,0,GL_RGBA,GL_UNSIGNED_BYTE,NULL);
        glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,tex,0);
        if(glCheckFramebufferStatus(GL_FRAMEBUFFER)!=GL_FRAMEBUFFER_COMPLETE) fail("FBO incomplete");
        glViewport(0,0,w,h);glUniform2f(glGetUniformLocation(p,"size"),(float)w,(float)h);glDrawArrays(GL_TRIANGLES,0,3);
        unsigned char *pixels=malloc((size_t)w*h*4);if(!pixels) fail("allocation");
        glReadPixels(0,0,w,h,GL_RGBA,GL_UNSIGNED_BYTE,pixels);
        unsigned errors=0;
        for(int y=0;y<h;y++) for(int x=0;x<w;x++) {
            int left=x+0.5f<w/2.f,bottom=y+0.5f<h/2.f;unsigned char expected[]={left?255:0,bottom?255:0,left==bottom?255:0,255};
            if(memcmp(pixels+4*((size_t)y*w+x),expected,4))errors++;
        }
        free(pixels);printf("PIXELS %dx%d mismatches=%u\n",w,h,errors);fflush(stdout);
        if(errors||glGetError()!=GL_NO_ERROR) fail("shader/FBO resize pixels");
    }
    if(compute) {
        GLuint cp=program(shader(GL_COMPUTE_SHADER,"#version 430 core\nlayout(local_size_x=32)in;layout(std430,binding=0)buffer B{uint data[];};void main(){uint i=gl_GlobalInvocationID.x;data[i]=i*3u+7u;}"),0);
        GLuint buf;glGenBuffers(1,&buf);glBindBuffer(GL_SHADER_STORAGE_BUFFER,buf);glBufferData(GL_SHADER_STORAGE_BUFFER,128*sizeof(GLuint),NULL,GL_DYNAMIC_READ);glBindBufferBase(GL_SHADER_STORAGE_BUFFER,0,buf);
        glUseProgram(cp);glDispatchCompute(4,1,1);glMemoryBarrier(GL_BUFFER_UPDATE_BARRIER_BIT);
        GLuint *data=glMapBufferRange(GL_SHADER_STORAGE_BUFFER,0,128*sizeof(GLuint),GL_MAP_READ_BIT);if(!data)fail("SSBO mapping");
        unsigned errors=0;for(unsigned i=0;i<128;i++)if(data[i]!=i*3+7)errors++;
        if(!glUnmapBuffer(GL_SHADER_STORAGE_BUFFER))fail("SSBO contents lost");
        printf("COMPUTE words=128 mismatches=%u\n",errors);if(errors||glGetError()!=GL_NO_ERROR)fail("compute result");
        glDeleteBuffers(1,&buf);glDeleteProgram(cp);
    } else puts("COMPUTE SKIP: GL 4.3 context unavailable");
    glDeleteTextures(1,&tex);glDeleteFramebuffers(1,&fbo);glDeleteVertexArrays(1,&vao);glDeleteProgram(p);
    eglMakeCurrent(d,EGL_NO_SURFACE,EGL_NO_SURFACE,EGL_NO_CONTEXT);eglDestroySurface(d,surf);eglDestroyContext(d,ctx);eglTerminate(d);
    printf("PASS graphics%s backend=%s\n",compute?"+compute":" (compute not tested)",want_gpu?"Mali":"software-control");return 0;
}
