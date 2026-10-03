#define GL_GLEXT_PROTOTYPES
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GL/gl.h>
#include <GL/glext.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static GLuint shader(GLenum type,const char *src){
 GLuint s=glCreateShader(type); GLint ok=0; char log[8192]={0};
 glShaderSource(s,1,&src,0); glCompileShader(s); glGetShaderiv(s,GL_COMPILE_STATUS,&ok);
 glGetShaderInfoLog(s,sizeof(log),0,log); printf("SHADER type=%x ok=%d log=%s\n",type,ok,log);
 if(!ok) exit(3); return s;
}
int main(void){
 setbuf(stdout,0);
 EGLDisplay d=eglGetDisplay(EGL_DEFAULT_DISPLAY); EGLint ma,mi,n;
 if(!eglInitialize(d,&ma,&mi)){printf("INIT FAIL %x\n",eglGetError());return 2;}
 const EGLint ca[]={EGL_SURFACE_TYPE,EGL_PBUFFER_BIT,EGL_RENDERABLE_TYPE,EGL_OPENGL_BIT,EGL_RED_SIZE,8,EGL_GREEN_SIZE,8,EGL_BLUE_SIZE,8,EGL_ALPHA_SIZE,8,EGL_NONE};
 EGLConfig cfg; if(!eglChooseConfig(d,ca,&cfg,1,&n)||!n){printf("CONFIG FAIL %x\n",eglGetError());return 2;}
 eglBindAPI(EGL_OPENGL_API);
 const EGLint ctxa[]={EGL_CONTEXT_MAJOR_VERSION,3,EGL_CONTEXT_MINOR_VERSION,3,EGL_CONTEXT_OPENGL_PROFILE_MASK,EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT,EGL_NONE};
 const EGLint pa[]={EGL_WIDTH,64,EGL_HEIGHT,64,EGL_NONE};
 EGLContext c=eglCreateContext(d,cfg,EGL_NO_CONTEXT,ctxa); EGLSurface p=eglCreatePbufferSurface(d,cfg,pa);
 if(!c||!p||!eglMakeCurrent(d,p,p,c)){printf("CONTEXT FAIL %x\n",eglGetError());return 2;}
 FILE *maps=fopen("/proc/self/maps","r"); char line[1024]; while(maps && fgets(line,sizeof(line),maps)){if(strstr(line,"libMobileGL")||strstr(line,"libGLES_mali")||strstr(line,"libEGL")||strstr(line,"libhybris-common")) printf("MAP %s",line);} if(maps)fclose(maps);
 printf("FRONTEND_RENDERER=%s\nFRONTEND_VERSION=%s\n",glGetString(GL_RENDERER),glGetString(GL_VERSION));
 const char *vs="#version 330 core\nlayout(location=0) in vec2 position; void main(){gl_Position=vec4(position,0.0,1.0);}";
 const char *fs="#version 330 core\nout vec4 color; void main(){color=vec4(1.0,0.0,0.0,1.0);}";
 GLuint v=shader(GL_VERTEX_SHADER,vs),f=shader(GL_FRAGMENT_SHADER,fs),prog=glCreateProgram();
 glAttachShader(prog,v);glAttachShader(prog,f);glLinkProgram(prog);GLint ok;glGetProgramiv(prog,GL_LINK_STATUS,&ok);char log[8192]={0};glGetProgramInfoLog(prog,sizeof(log),0,log);printf("LINK ok=%d log=%s\n",ok,log);if(!ok)return 3;
 GLuint vao,b; float verts[]={-1,-1,1,-1,0,1};glGenVertexArrays(1,&vao);glBindVertexArray(vao);glGenBuffers(1,&b);glBindBuffer(GL_ARRAY_BUFFER,b);glBufferData(GL_ARRAY_BUFFER,sizeof(verts),verts,GL_STATIC_DRAW);glVertexAttribPointer(0,2,GL_FLOAT,GL_FALSE,0,0);glEnableVertexAttribArray(0);
 glViewport(0,0,64,64);glClearColor(0,0,1,1);glClear(GL_COLOR_BUFFER_BIT);glUseProgram(prog);glDrawArrays(GL_TRIANGLES,0,3);glFinish();unsigned char center[4],corner[4];glReadPixels(32,32,1,1,GL_RGBA,GL_UNSIGNED_BYTE,center);glReadPixels(0,63,1,1,GL_RGBA,GL_UNSIGNED_BYTE,corner);GLenum err=glGetError();
 printf("PIXEL center=%u,%u,%u,%u corner=%u,%u,%u,%u error=%x\n",center[0],center[1],center[2],center[3],corner[0],corner[1],corner[2],corner[3],err);
 int pass=!err&&center[0]>240&&center[1]<10&&center[2]<10&&corner[0]<10&&corner[2]>240;
 glDeleteBuffers(1,&b);glDeleteVertexArrays(1,&vao);glDeleteProgram(prog);glDeleteShader(v);glDeleteShader(f);eglMakeCurrent(d,EGL_NO_SURFACE,EGL_NO_SURFACE,EGL_NO_CONTEXT);eglDestroySurface(d,p);eglDestroyContext(d,c);eglTerminate(d);printf("RESULT=%s\n",pass?"PASS":"FAIL");return pass?0:4;
}
