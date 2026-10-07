#!/usr/bin/env python3
"""Independent adversarial tests; host-only, real staged helper, simulated Binder dumps."""
import json,os,pathlib,subprocess,tempfile
ROOT=pathlib.Path(__file__).resolve().parent
ns={'__file__':str(ROOT/'test_helper.py')}
exec((ROOT/'test_helper.py').read_text().split('def run_case')[0],ns)
mock=ns['MOCK'].replace("print('0')", "print(d.get('user','0'))")
mock=mock.replace("print('  UserId=0\\n    mBindingController:')", "print('  UserId='+d.get('dump_user','0')+'\\n    mBindingController:')\n  if d.get('installed_only'): print('    Input Methods:')")
mock=mock.replace("print(d.get('user','0'))", "print(d.get('user','0')); sys.exit(1 if d.get('output_error') else 0)")
mock=mock.replace("elif op=='get': print(d['settings'].get(key,'null'))", "elif op=='get':\n  print(d['settings'].get(key,'null'))\n  if d.get('settings_error'): sys.exit(1)")
mock=mock.replace("('mHasMainConnection','true')","('mHasMainConnection',d.get('connection','true'))")
mock=mock.replace("('mCurMethod','com.android.server.inputmethod.IInputMethodInvoker@abc')","('mCurMethod',d.get('method','com.android.server.inputmethod.IInputMethodInvoker@abc'))")
mock=mock.replace("('mImeWindowVis','0')","('mImeWindowVis',d.get('window','0'))")
mock=mock.replace("  print('    Input Methods:')\njson.dump", "  print('    Input Methods:')\n  if d.get('late_observer') and not ime.startswith('org.fedora.sessionime'):\n   d['settings']['input_methods_subtype_history']='late mutation'\njson.dump")
for kind,variation in [('foreground_user',{'user':'10'}),('dump_user',{'dump_user':'10'}),('disconnected',{'connection':'false'}),('null_method',{'method':'null'}),('visible',{'window':'1'}),('installed_only',{'installed_only':True}),('late_observer',{}),('output_error',{}),('settings_error',{})]:
 with tempfile.TemporaryDirectory() as td:
  t=pathlib.Path(td); b=t/'bin';b.mkdir()
  for name in ['runcon','getprop','pidof','sleep']:
   p=b/name;p.write_text(mock);p.chmod(0o755)
  original={'default_input_method':'com.samsung.android.honeyboard/.service.HoneyBoardService','enabled_input_methods':'com.samsung.android.honeyboard/.service.HoneyBoardService','selected_input_method_subtype':'65537','input_methods_subtype_history':''}
  state={'up':True,'settings':original.copy(),**variation};m=t/'model';m.write_text(json.dumps(state))
  h=t/'helper';h.write_text((ROOT/'stage/fedora-session-ime.sh').read_text().replace('T=/data/local/tmp',f'T={t}'))
  env=dict(os.environ,MODEL=str(m),PATH=str(b)+':'+os.environ['PATH'])
  def run(*args):return subprocess.run(['sh',str(h),*args],env=env,capture_output=True).returncode
  rc=run('prepare');snap=t/'.fedora-session-ime'
  if kind in ('output_error','settings_error'):
   assert rc==0
   state=json.loads(m.read_text());state[kind]=True;m.write_text(json.dumps(state))
   assert run('verify')!=0, 'expected output with failed Binder command was accepted'
  elif kind=='late_observer':
   assert rc==0
   state=json.loads(m.read_text());state['late_observer']=True;m.write_text(json.dumps(state))
   rc=run('restore','final');assert rc!=0 and snap.exists(), 'late observer overwrite accepted and originals deleted'
  else:assert rc!=0, f'{kind}: unsafe admission accepted'
  print(kind+': PASS')
