#define GL_GLEXT_PROTOTYPES
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GL/gl.h>
#include <GL/glext.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <unistd.h>
extern void **glXChooseFBConfig(Display*, int, const int*, int*);
extern XVisualInfo *glXGetVisualFromFBConfig(Display*, void*);
extern void *glXCreateContextAttribsARB(Display*,void*,void*,int,const int*);
extern int glXMakeCurrent(Display*,unsigned long,void*);
extern void glXSwapBuffers(Display*,unsigned long);
extern void glXDestroyContext(Display*,void*);
static GLuint shader(GLenum type,const char *src){
 GLuint s=glCreateShader(type); GLint ok=0; char log[8192]={0};
 glShaderSource(s,1,&src,0); glCompileShader(s); glGetShaderiv(s,GL_COMPILE_STATUS,&ok);
 glGetShaderInfoLog(s,sizeof(log),0,log); printf("SHADER type=%x ok=%d log=%s\n",type,ok,log);
 if(!ok) exit(3); return s;
}
int main(void){
 setbuf(stdout,0);
 Display *d=XOpenDisplay(0); if(!d)return 2;
 int n; const int attrs[]={0x8010,1,0x8011,1,5,1,8,8,9,8,10,8,12,24,13,8,0};
 void **configs=glXChooseFBConfig(d,DefaultScreen(d),attrs,&n);if(!configs||!n)return 2;
 XVisualInfo *visual=glXGetVisualFromFBConfig(d,configs[0]);if(!visual)return 2;
 XSetWindowAttributes wa={0};wa.colormap=XCreateColormap(d,RootWindow(d,visual->screen),visual->visual,AllocNone);wa.event_mask=StructureNotifyMask;
 Window win=XCreateWindow(d,RootWindow(d,visual->screen),0,0,64,64,0,visual->depth,InputOutput,visual->visual,CWColormap|CWEventMask,&wa);XMapWindow(d,win);XSync(d,0);
 const int ca[]={0x2091,3,0x2092,3,0x9126,1,0}; void *c=glXCreateContextAttribsARB(d,configs[0],0,1,ca);
 if(!c||!glXMakeCurrent(d,win,c)){puts("MAKECURRENT FAIL");return 2;}
 FILE *maps=fopen("/proc/self/maps","r"); char line[1024]; while(maps && fgets(line,sizeof(line),maps)){if(strstr(line,"libMobileGL")||strstr(line,"libGLES_mali")||strstr(line,"libEGL")||strstr(line,"libhybris-common")) printf("MAP %s",line);} if(maps)fclose(maps);
 printf("FRONTEND_RENDERER=%s\nFRONTEND_VERSION=%s\n",glGetString(GL_RENDERER),glGetString(GL_VERSION));
 GLint limit;
 const GLenum caps[]={GL_MAX_VERTEX_SHADER_STORAGE_BLOCKS,GL_MAX_FRAGMENT_SHADER_STORAGE_BLOCKS,GL_MAX_COMPUTE_SHADER_STORAGE_BLOCKS};
 for(int i=0;i<3;i++){limit=-1;glGetIntegerv(caps[i],&limit);printf("SSBO_LIMIT %x=%d\n",caps[i],limit);}
 GLint extcount=0;glGetIntegerv(GL_NUM_EXTENSIONS,&extcount);for(int i=0;i<extcount;i++){const char *ext=(const char*)glGetStringi(GL_EXTENSIONS,i);if(ext&&(strstr(ext,"shader_draw_parameters")||strstr(ext,"clip_control")||strstr(ext,"get_texture_sub_image")))printf("EXT %s\n",ext);}
 const char *vs="#version 330 core\nlayout(location=0) in vec2 position; void main(){gl_Position=vec4(position,0.0,1.0);}";
 const char *fs="#version 330 core\nout vec4 color; void main(){color=vec4(1.0,0.0,0.0,1.0);}";
 GLuint v=shader(GL_VERTEX_SHADER,vs),f=shader(GL_FRAGMENT_SHADER,fs),prog=glCreateProgram();
 glAttachShader(prog,v);glAttachShader(prog,f);glLinkProgram(prog);GLint ok;glGetProgramiv(prog,GL_LINK_STATUS,&ok);char log[8192]={0};glGetProgramInfoLog(prog,sizeof(log),0,log);printf("LINK ok=%d log=%s\n",ok,log);if(!ok)return 3;
 GLuint vao,b; float verts[]={-1,-1,1,-1,0,1};glGenVertexArrays(1,&vao);glBindVertexArray(vao);glGenBuffers(1,&b);glBindBuffer(GL_ARRAY_BUFFER,b);glBufferData(GL_ARRAY_BUFFER,sizeof(verts),verts,GL_STATIC_DRAW);glVertexAttribPointer(0,2,GL_FLOAT,GL_FALSE,0,0);glEnableVertexAttribArray(0);
 int pass=1;
 for(int frame=0;frame<3;frame++){
 int size=frame==0?64:frame==1?97:64;
 XResizeWindow(d,win,size,size);XSync(d,0);if(!glXMakeCurrent(d,win,c))return 2;
 glViewport(0,0,size,size);glClearColor(0,0,1,1);glClear(GL_COLOR_BUFFER_BIT);glUseProgram(prog);glDrawArrays(GL_TRIANGLES,0,3);glXSwapBuffers(d,win);XSync(d,0);
 XImage *img=XGetImage(d,win,0,0,size,size,AllPlanes,ZPixmap);
 unsigned long center=XGetPixel(img,size/2,size/2),corner=XGetPixel(img,0,0);GLenum err=glGetError();
 printf("XPIXEL frame=%d size=%d center=%lx corner=%lx error=%x\n",frame,size,center,corner,err);
 pass=pass&&!err&&(center&0xffffff)==0xff0000&&(corner&0xffffff)==0x0000ff;XDestroyImage(img);
 glXMakeCurrent(d,0,0);if(!glXMakeCurrent(d,win,c))return 2;
 }
 glDeleteBuffers(1,&b);glDeleteVertexArrays(1,&vao);glDeleteProgram(prog);glDeleteShader(v);glDeleteShader(f);glXMakeCurrent(d,0,0);glXDestroyContext(d,c);XDestroyWindow(d,win);XFree(visual);XFree(configs);XCloseDisplay(d);printf("RESULT=%s\n",pass?"PASS":"FAIL");return pass?0:4;
}
