# GNOME screen recording on the MediaTek hardware H.264 encoder (experiment)

Patches GNOME Shell's screencast service to add one pipeline: Mutter DMA-BUF capture ->
`v4l2h264enc` (`/dev/video3`), keeping the upstream software fallbacks. See `RESULTS.md`
(measurements and limits), `INVESTIGATION.md`, `AUDIT.md`. `screencastService-dma.patch` is the
diff against GNOME Shell's stock service. `install-recorder-device.sh` and
`rollback-recorder-device.sh` are hash-pinned and refuse unexpected states.

Software OpenH264 used ~560% CPU; this path ~19% of one core in a 60 s motion test. Plain
MemFd capture with hardware encoding caused ~1 GiB/s compositor memory growth and a session crash;
only the DMA-BUF path is safe. Not verified: DRM content, rotation/area capture, long runs, battery.
