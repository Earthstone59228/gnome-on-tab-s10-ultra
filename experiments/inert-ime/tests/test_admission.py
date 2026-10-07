#!/usr/bin/env python3
"""Host-only admission guards extracted verbatim; no Android or process kill."""
import os,pathlib,subprocess,tempfile
ROOT=pathlib.Path(__file__).resolve().parent
text=(ROOT/'stage/admit-dummy-ime.sh').read_text()
a=text.index('new_identity() {');b=text.index('\n}\n',a)+3
fn=text[a:b]
for case in ['success','same_pid','wrong_uid','wrong_name','wrong_remote','framework_restart']:
 with tempfile.TemporaryDirectory() as td:
  t=pathlib.Path(td);p=t/'proc/456';p.mkdir(parents=True)
  (p/'cmdline').write_bytes(b'org.fedora.sessionime\0' if case!='wrong_name' else b'system_server\0')
  (p/'status').write_text('Uid:\t'+('12345' if case!='wrong_uid' else '1000')+'\t0\t0\t0\n')
  helper=t/'helper';helper.write_text('#!/bin/sh\necho "'+('456 12345' if case!='wrong_remote' else '123 12345')+'"\n');helper.chmod(0o755)
  prefix=f'pid=123\nuid=12345\nss=777\nHELPER={helper}\n'
  prefix+='pidof() { case "$1" in org.fedora.sessionime) echo '+('123' if case=='same_pid' else '456')+';; system_server) echo '+('999' if case=='framework_restart' else '777')+';; esac; }\n'
  script=t/'test';script.write_text(prefix+fn.replace('/proc/',str(t/'proc')+'/')+'\nnew_identity\n')
  rc=subprocess.run(['/bin/sh',str(script)],capture_output=True).returncode
  assert (rc==0)==(case=='success'),case
  print('admission identity '+case+': PASS')
# Unreviewed stages stop before any device-specific operation.
for name in ['install-dummy-ime-apk.sh','admit-dummy-ime.sh','install-dummy-ime.sh']:
 rc=subprocess.run(['/bin/sh',str(ROOT/'stage'/name)],capture_output=True).returncode
 assert rc!=0,name
 print('absent review gate '+name+': PASS')
