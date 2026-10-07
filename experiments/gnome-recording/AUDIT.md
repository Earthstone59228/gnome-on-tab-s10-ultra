# Independent v3 recorder source and installer audit

Keyboard mitigation was audited first by this independent agent after its builder finished. This optional recorder pass reviews the v3 candidate and final installer/rollback scripts; no device installation, recording, settings change or service restart was performed by the audit agent.

## Verdict

No unresolved blocking static defect was found in the final v3 source and corrected installer/rollback. Applying the exact pinned v3 resource with the corrected installer, while the recorder service is idle, is a reasonable reversible next action within the user's requested hardware recording work. This does not approve v1/v2, Android IME integration, arbitrary GNOME versions, every capture format or indefinite runtime.

Parent's runtime tests, independently reported in RESULTS.md and associated logs, provide actual DMA-path evidence beyond this static review. Parent reports one minute of synthetic motion at native 2960x1848, approximately 20 delivered fps, recorder mean18.9% of one core, plateauing compositor memory and successful start/stop with unchanged GNOME PID. Parent also reports a4-second motion check using the normal draw-cursor=true default, with hardware encoder active, stable compositor RSS and successful stop/stock rollback. I did not independently run those on-device tests. Battery temperature/energy improvements were not established by those measurements.

## Source scope

Compared `resource-src-v3/screencastService.js` directly against `screencastService.original.js`. The only changes add one DMA hardware pipeline and pass its optional producer-buffer settings to pipewiresrc. The path requires DMA_DRM DMABuf frames, uses 4–8 producer buffers and a leaky two-buffer raw queue, imports input through V4L2 DMABuf, and parses H.264 into fragmented MP4. Existing pipeline negotiation, error handling, crash blocklist and fallback pipelines are unchanged. No unsafe hardware MemFd fallback remains; no Android services, compositor code, kernel/module, input isolation or display ownership are modified.

Vendor import/fence/buffer lifetime behavior cannot be proved from the JavaScript pipeline string; it needed the parent’s runtime tests. Those tests cover the observed mode/workload and time window. An unavailable hardware negotiation can fall back to existing software recording, so hardware CPU savings are conditional on the selected path. Leaky raw queue may drop frames when encoding lags, consistent with measured delivery below the requested30fps maximum.

## Installer defects found and corrected

1. **High guard bypass on process inventory failure:** original `if ps ... | awk ...` ignored ps exit status in a conditional pipeline; empty awk input meant installation proceeded. Independent host model reproduced rc0 and resource replacement when ps returned1. Final scripts capture a successful process inventory first and reject both inventory and unexpected awk failures (`install-recorder-device.sh:11`, `rollback-recorder-device.sh:9`).
2. **Medium incomplete active-service match:** original matcher required argv0 literal `gjs`, missing `/usr/bin/gjs`. Final matcher uses exact process NAME `gjs` plus the recorder script path in args. It conservatively rejects active recorder instances regardless of that argv0 spelling.
3. **Medium rollback guard gap:** original rollback had no active-service or recognized-current-resource guard. Final rollback accepts only pinned original/candidate, verifies backup/temp/restored checksums, rejects process inventory failure/active recorder and rechecks immediately before rename.

4. **Low Android-shell portability issue found during guarded device attempt:** Android mksh aliases `hash` to `alias -t`; the generic helper name expanded as that alias, so the initial checksum comparison returned empty and safely refused installation. Host `/bin/sh` tests did not reveal this target-shell difference. Both scripts now use `recorder_file_sha256` consistently. Independent read-only Android alias-name checks found no collisions with the renamed recorder helpers or all keyboard helper names. Eight host cases and syntax checks pass again. Parent will verify actual helper execution before installation.

Final install verifies staged candidate, recognizes only pinned original/candidate, verifies/preserves the original backup, copies existing target metadata into an adjacent temporary, verifies candidate bytes, checks recorder idle again and atomically renames the resource. Neither installer restarts desktop or Android. Idempotent already-candidate install does no resource mutation. Final rollback preserves metadata and similarly replaces only the pinned resource via verified temporary. Backup remains available.

These idle checks are best effort, not a global exclusion lock against a concurrent user starting recording in the final check/rename gap. Atomic rename prevents a partially written resource; an already loaded service keeps its previous resource mapping. Apply during the observed idle session and verify final hash, rather than claiming arbitrary concurrent install/recording safety.

## Independent validation

`audit_installer_checks.py` runs real final shell scripts against host-only temporary resources and mocked process inventory. Eight cases pass:

- Install inventory failure rejects without replacing the resource.
- Install absolute `/usr/bin/gjs` active recorder rejects.
- Install recorder appearing on final check rejects.
- Idle install succeeds with original metadata preserved.
- Rollback inventory failure rejects without replacing the resource.
- Rollback absolute `/usr/bin/gjs` active recorder rejects.
- Rollback unrecognized current resource rejects.
- Idle rollback succeeds with expected original bytes and metadata.

Shell syntax passes for both scripts. This suite verifies guard/error propagation and resource-state changes, not Android toybox behavior or encoder correctness. Parent supplied actual tablet process format and runtime testing.

## Exact reviewed hashes

- v3 `screencastService.js`: `f99cb2bd5ae56005da003f829481fbc1223c3c7b591eab7a8d92cada38f48d4e`
- Installer: `9a87a22ce351b03cd24bc1720aab1811375c9a80c405c7e3b8ad29f9663c188e`
- Rollback: `e860fbb1bb66e6a165b8a56df131193993d907a4498a21d9f033c0fcd911064b`
- Candidate resource: `cb8d431acd9f1370ec7ff9f3f87d30703132b9025ecd17161363feceafd51b88`
- Original resource pinned in installer: `89f8954d2302e8c9eb22b4fcfe587ebddf2c51f1de64565358e2e43b6239a0a0`
