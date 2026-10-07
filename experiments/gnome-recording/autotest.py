#!/usr/bin/python3
import argparse, json, os, subprocess, sys, time
from pathlib import Path
import gi
gi.require_version('Gio','2.0')
from gi.repository import Gio, GLib

ap=argparse.ArgumentParser()
ap.add_argument('--gnome-pid',type=int,required=True)
ap.add_argument('--width',type=int)
ap.add_argument('--height',type=int)
ap.add_argument('--seconds',type=int,default=10)
ap.add_argument('--animate',action='store_true')
a=ap.parse_args()
assert 1 <= a.seconds <= 90
assert (a.width is None) == (a.height is None)
assert Path('/proc/%d/comm'%a.gnome_pid).read_text().strip()=='gnome-shell'
env={}
for pair in Path('/proc/%d/environ'%a.gnome_pid).read_bytes().split(b'\0'):
 if b'=' in pair:
  k,v=pair.split(b'=',1); env[k.decode()]=v.decode()
for key in ['DBUS_SESSION_BUS_ADDRESS','XDG_RUNTIME_DIR','WAYLAND_DISPLAY']:
 if key in env: os.environ[key]=env[key]
conn=Gio.bus_get_sync(Gio.BusType.SESSION,None)

def mem():
 return {l.split(':',1)[0]:int(l.split()[1]) for l in Path('/proc/meminfo').read_text().splitlines() if len(l.split())>=2}
def stat(pid):
 f=Path('/proc/%d/stat'%pid).read_text().rsplit(')',1)[1].split()
 return (int(f[11])+int(f[12]))/os.sysconf('SC_CLK_TCK')
def rss(pid):
 for l in Path('/proc/%d/status'%pid).read_text().splitlines():
  if l.startswith('VmRSS:'): return int(l.split()[1])
 return 0
def call(method,params,rettype,timeout=15000):
 return conn.call_sync('org.gnome.Shell.Screencast','/org/gnome/Shell/Screencast',
  'org.gnome.Shell.Screencast',method,params,GLib.VariantType.new(rettype),
  Gio.DBusCallFlags.NONE,timeout,None).unpack()

before=mem()
baseline_rss=rss(a.gnome_pid)
print(json.dumps(dict(event='before',available_kib=before['MemAvailable'],swap_free_kib=before['SwapFree'],gnome_rss_kib=rss(a.gnome_pid))),flush=True)
if before['MemAvailable'] < 3145728: raise RuntimeError('Need at least 3GiB available before testing')
started=False
motion=None
try:
 if a.animate:
  motion_env=dict(os.environ,GDK_BACKEND='wayland',GSK_RENDERER='cairo')
  motion=subprocess.Popen([sys.executable,'/tmp/gnome-hw-motion.py'],env=motion_env,stdout=subprocess.DEVNULL)
  time.sleep(1)
  if motion.poll() is not None: raise RuntimeError('Motion test window failed to launch')
 template='/tmp/gnome-hw-autotest-%d'%int(time.time())
 options={'framerate':GLib.Variant('u',30)}
 if a.width is None:
  result=call('Screencast',GLib.Variant('(sa{sv})',(template,options)),'(bs)')
 else:
  screensize=conn.call_sync('org.gnome.Shell.Introspect','/org/gnome/Shell/Introspect',
   'org.freedesktop.DBus.Properties','Get',GLib.Variant('(ss)',('org.gnome.Shell.Introspect','ScreenSize')),
   GLib.VariantType.new('(v)'),Gio.DBusCallFlags.NONE,5000,None).unpack()[0]
  if not 1 <= a.width <= screensize[0] or not 1 <= a.height <= screensize[1]:
   raise ValueError('Area exceeds logical ScreenSize: %s'%(screensize,))
  result=call('ScreencastArea',GLib.Variant('(iiiisa{sv})',(0,0,a.width,a.height,template,options)),'(bs)')
 started=result[0]
 print(json.dumps(dict(event='start',success=result[0],output=result[1])),flush=True)
 if not started: raise RuntimeError('Recorder did not start')
 pid=conn.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus',
  'GetConnectionUnixProcessID',GLib.Variant('(s)',('org.gnome.Shell.Screencast',)),
  GLib.VariantType.new('(u)'),Gio.DBusCallFlags.NONE,5000,None).unpack()[0]
 paths=[]
 for fd in Path('/proc/%d/fd'%pid).iterdir():
  try: paths.append(os.readlink(fd))
  except FileNotFoundError: pass
 maps=Path('/proc/%d/maps'%pid).read_text()
 print(json.dumps(dict(event='encoder',pid=pid,video3_open=any(p.endswith('/dev/video3') for p in paths),openh264_loaded='libopenh264' in maps)),flush=True)
 if not any(p.endswith('/dev/video3') for p in paths) or 'libopenh264' in maps:
  raise RuntimeError('Hardware path unavailable; stop instead of measuring software fallback')
 last_t=time.monotonic(); last_cpu=stat(pid); last_gnome_cpu=stat(a.gnome_pid)
 for sec in range(a.seconds):
  time.sleep(1)
  m=mem(); now=time.monotonic(); cpu=stat(pid)
  print(json.dumps(dict(event='sample',second=sec+1,cpu_pct=round((cpu-last_cpu)/(now-last_t)*100,1),gnome_cpu_pct=round((stat(a.gnome_pid)-last_gnome_cpu)/(now-last_t)*100,1),available_kib=m['MemAvailable'],swap_free_kib=m['SwapFree'],recorder_rss_kib=rss(pid),gnome_rss_kib=rss(a.gnome_pid))),flush=True)
  last_t=now; last_cpu=cpu; last_gnome_cpu=stat(a.gnome_pid)
  if m['MemAvailable'] < 3145728 or rss(a.gnome_pid)-baseline_rss > 524288:
   print('Memory/RSS threshold reached; stopping recording',flush=True); break
finally:
 try:
  if started:
   stop_result=call('StopScreencast',None,'(b)',10000)
   print(json.dumps(dict(event='stop',result=stop_result)),flush=True)
   if stop_result != (True,): raise RuntimeError('Recorder did not confirm successful stop')
 finally:
  if motion is not None:
   try: motion.wait(timeout=3)
   except subprocess.TimeoutExpired:
    motion.kill(); motion.wait(timeout=3)
print(json.dumps(dict(event='after',available_kib=mem()['MemAvailable'],gnome_alive=Path('/proc/%d/comm'%a.gnome_pid).exists())),flush=True)
