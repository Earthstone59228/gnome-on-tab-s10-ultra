# Restore the original GNOME recorder

Stop any recording and wait for the recording service to exit. With the connected tablet, run:

```bash
adb -s <serial> shell "/data/local/tmp/root -c 'sh /data/local/tmp/rollback-gnome-dma-recorder.sh'"
```

The audited rollback verifies known original/candidate hashes and refuses while the recorder service is active. No GNOME restart is required. Original resource backup lives beside `/data/fedora/usr/share/gnome-shell/org.gnome.Shell.Screencast.src.gresource` with suffix `.pre-mtk-hwenc-20261007`.
