#!/usr/bin/env python3
"""Host model tests: run the real shell helper with mocked Android commands."""
import json, os, pathlib, subprocess, tempfile
ROOT=pathlib.Path(__file__).resolve().parent
MOCK='''#!/usr/bin/env python3
import json, os, sys
p=os.environ['MODEL']; d=json.load(open(p)); args=sys.argv[1:]; name=os.path.basename(sys.argv[0])
if name=='sleep': sys.exit(0)
if name=='pidof': sys.exit(1)
if name=='getprop': print('running' if d['up'] else 'stopped'); sys.exit(0)
args=args[1:]; cmd=os.path.basename(args.pop(0))
if cmd=='settings':
 assert args[:2]==['--user','0']; args=args[2:]; op=args[0]; key=args[2] if len(args)>2 else None
 if op=='list':
  for k,v in d['settings'].items(): print(k+'='+v)
 elif op=='get': print(d['settings'].get(key,'null'))
 elif op=='put': d['settings'][key]=args[3]
 elif op=='delete': d['settings'].pop(key,None)
elif cmd=='cmd':
 if args[:2]==['activity','get-current-user']: print('0')
 else:
  assert args[:2]==['input_method','ime']; op=args[2]; ime=args[-1]
  if op=='set':
   if d.get('setfail'): sys.exit(1)
   d['settings']['default_input_method']=ime
   d['settings']['selected_input_method_subtype']='-1'
   d['settings']['input_methods_subtype_history']='changed'
  elif op=='enable': d['settings']['enabled_input_methods']+=':'+ime
elif cmd=='dumpsys':
 if args[0]=='input_method':
  ime=d.get('bound_override',d['settings']['default_input_method']); hw='true' if d.get('handwriting') else 'false'
  print('  UserId=0\\n    mBindingController:')
  for k,v in [('mSelectedMethodId',ime),('mCurId',ime),('mHasMainConnection','true'),('mCurMethod','com.android.server.inputmethod.IInputMethodInvoker@abc'),('mSupportsStylusHw',hw),('mSupportsConnectionlessStylusHw','false'),('mImeWindowVis','0')]: print('      '+k+'='+v)
  print('    Input Methods:')
json.dump(d,open(p,'w'))
'''
def run_case(kind):
 with tempfile.TemporaryDirectory() as t:
  t=pathlib.Path(t); bindir=t/'bin'; bindir.mkdir()
  for name in ['runcon','getprop','pidof','sleep']:
   f=bindir/name; f.write_text(MOCK); f.chmod(0o755)
  state={'up':True,'settings':{'default_input_method':'com.samsung.android.honeyboard/.service.HoneyBoardService','enabled_input_methods':'com.samsung.android.honeyboard/.service.HoneyBoardService;1;2','selected_input_method_subtype':'65537','input_methods_subtype_history':''}}
  if kind=='down':state['up']=False
  if kind=='handwriting':state['handwriting']=True
  if kind=='setfail':state['setfail']=True
  if kind=='absent_history':state['settings'].pop('input_methods_subtype_history')
  model=t/'model'; model.write_text(json.dumps(state)); original=state['settings'].copy()
  helper=t/'helper'; helper.write_text((ROOT/'stage/fedora-session-ime.sh').read_text().replace('T=/data/local/tmp',f'T={t}'))
  env=dict(os.environ,MODEL=str(model),PATH=str(bindir)+':'+os.environ['PATH'])
  def invoke(*args):return subprocess.run(['sh',str(helper),*args],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE).returncode
  rc=invoke('prepare'); snap=t/'.fedora-session-ime'
  if kind=='down':assert rc and not snap.exists() and json.loads(model.read_text())['settings']==original
  elif kind in ('handwriting','setfail'):assert rc and snap.exists()
  else:
   assert rc==0 and snap.exists()
   assert invoke('prepare')!=0 # stale originals must never be overwritten
   state=json.loads(model.read_text());state['up']=False;model.write_text(json.dumps(state))
   assert invoke('restore','final')!=0 and snap.exists() # no UI return while SF stopped
   state['up']=True;model.write_text(json.dumps(state))
   assert invoke('restore')==0 and snap.exists()
   assert json.loads(model.read_text())['settings']==original
   if kind=='wrong_binding':
    state=json.loads(model.read_text());state['bound_override']='org.fedora.sessionime/.InertIme';model.write_text(json.dumps(state))
    assert invoke('restore','final')!=0 and snap.exists()
    assert json.loads(model.read_text())['settings']==original
    state=json.loads(model.read_text());state.pop('bound_override');model.write_text(json.dumps(state))
   assert invoke('restore','final')==0 and not snap.exists()
   assert json.loads(model.read_text())['settings']==original
for kind in ['success','absent_history','wrong_binding','down','handwriting','setfail']:
 run_case(kind);print(kind+': PASS')

# Exercise the actual abort function with transient/permanent restoration failures.
def test_abort(failures):
 text=(ROOT/'stage/fedora-session-gnome-shell.sh').read_text()
 function=text[text.index('abort_before_sf() {'):text.index('\n}\n',text.index('abort_before_sf() {'))+3]
 with tempfile.TemporaryDirectory() as t:
  t=pathlib.Path(t)
  script=t/'abort.sh'
  prelude=f"T={t}\nPH={t}/phase\nWPID=123\nAWPID=\nIHON=\nPGPID=\n"
  prelude+='log() { :; }\nsleep() { :; }\nruncon() { :; }\nphase() { echo "$1" > "$PH"; }\n'
  prelude+=f'attempt=0\nsh() {{ attempt=$((attempt + 1)); [ "$attempt" -gt {failures} ]; }}\n'
  prelude+='kill() { echo called > "$T/watchdog-killed"; }\n'
  script.write_text(prelude+function+'\nabort_before_sf test\n')
  assert subprocess.run(['/bin/sh',str(script)]).returncode==1
  assert (t/'watchdog-killed').exists()==(failures<3)
  assert (t/'phase').exists()==(failures>=3)
for failures in [0,1,99]:
 test_abort(failures);print(f'abort failures={failures}: PASS')
