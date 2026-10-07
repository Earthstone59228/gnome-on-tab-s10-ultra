#!/usr/bin/env python3
"""Host-only independent installation/rollback admission tests."""
import os,pathlib,subprocess,tempfile
ROOT=pathlib.Path(__file__).resolve().parent
original=(ROOT/'original.gresource').read_bytes();candidate=(ROOT/'patched-v3.gresource').read_bytes()
for script,kind in [('install','ps_failure'),('install','absolute_gjs'),('install','late_active'),('install','idle'),('rollback','ps_failure'),('rollback','absolute_gjs'),('rollback','unknown_current'),('rollback','idle')]:
 with tempfile.TemporaryDirectory() as td:
  t=pathlib.Path(td);r=t/'resource';s=t/'staged';b=t/'resource.pre-mtk-hwenc-20261007';r.write_bytes(original if script=='install' else candidate);r.chmod(0o644);s.write_bytes(candidate)
  if script=='rollback':b.write_bytes(original)
  if kind=='unknown_current':r.write_bytes(b'unknown resource')
  prior=r.read_bytes();binp=t/'bin';binp.mkdir();ps=binp/'ps';counter=t/'counter'
  ps.write_text('#!/bin/sh\nn=0\n[ ! -f "'+str(counter)+'" ] || n=$(cat "'+str(counter)+'")\nn=$((n+1)); echo "$n" > "'+str(counter)+'"\n'+('exit 1\n' if kind=='ps_failure' else ('echo "gjs /usr/bin/gjs -m /usr/share/gnome-shell/org.gnome.Shell.Screencast"\n' if kind=='absolute_gjs' else ('[ "$n" -lt 2 ] || echo "gjs gjs -m /usr/share/gnome-shell/org.gnome.Shell.Screencast"\nexit 0\n' if kind=='late_active' else 'echo "NAME ARGS"\n'))));ps.chmod(0o755)
  path=ROOT/(script+'-recorder-device.sh');text=path.read_text().replace('R=/data/fedora/usr/share/gnome-shell/org.gnome.Shell.Screencast.src.gresource','R='+str(r)).replace('S=/data/local/tmp/gnome-screencast-mtk-v3.gresource','S='+str(s))
  f=t/'run';f.write_text(text);env=dict(os.environ,PATH=str(binp)+':'+os.environ['PATH']);rc=subprocess.run(['sh',str(f)],env=env,capture_output=True).returncode
  if kind=='idle':
   assert rc==0,(script,kind,rc)
   assert r.read_bytes()==(candidate if script=='install' else original)
   assert r.stat().st_mode&0o777==0o644
  else:assert rc!=0 and r.read_bytes()==prior,(script,kind,rc,'unsafe mutation/acceptance')
  print(script+'/'+kind+': PASS')
