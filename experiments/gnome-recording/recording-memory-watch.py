#!/usr/bin/python3
"""Bounded test monitor. Reads metadata only; aborts only a verified recorder PID."""
import argparse, json, os, signal, time
from pathlib import Path

RECORDER = b'/usr/share/gnome-shell/org.gnome.Shell.Screencast'

def identity(pid):
    root = Path('/proc') / str(pid)
    argv = (root / 'cmdline').read_bytes().split(b'\0')
    if RECORDER not in argv:
        raise ValueError('PID is not the GNOME recording service')
    fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
    return fields[19]  # field 22: starttime, after pid/comm

def meminfo():
    return {line.split(':',1)[0]: int(line.split()[1])
            for line in Path('/proc/meminfo').read_text().splitlines()
            if len(line.split()) >= 2}

def rss(pid):
    for line in (Path('/proc') / str(pid) / 'status').read_text().splitlines():
        if line.startswith('VmRSS:'):
            return int(line.split()[1])
    return 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pid', type=int)
    ap.add_argument('--seconds', type=int, default=45)
    ap.add_argument('--min-available-kib', type=int, default=1048576)
    args = ap.parse_args()
    if args.pid <= 1 or not 1 <= args.seconds <= 120:
        ap.error('invalid PID or duration')
    start = identity(args.pid)
    for second in range(args.seconds):
        try:
            if identity(args.pid) != start:
                print('Recorder PID changed; monitor exiting', flush=True)
                return
            mem = meminfo()
            sample = dict(second=second, recorder_rss_kib=rss(args.pid),
                          available_kib=mem['MemAvailable'], swap_free_kib=mem['SwapFree'])
            print(json.dumps(sample), flush=True)
            if mem['MemAvailable'] < args.min_available_kib:
                # Fail safely without signalling gnome-shell, framework, or supervisor.
                if identity(args.pid) == start:
                    os.kill(args.pid, signal.SIGKILL)
                    print('ABORT: recorder killed at low available memory; output may be incomplete', flush=True)
                return
        except FileNotFoundError:
            print('Recorder exited; monitor exiting', flush=True)
            return
        time.sleep(1)
    print('Monitor duration ended; no process signalled', flush=True)

if __name__ == '__main__':
    main()
