# StrataLeaf container drill evidence

Overall: PASS

## Environment caveats (read before trusting the checks)

- **Identity simulated**: the host could not switch to uid 9042; the drills ran as 1000:1000. `identity-gate` separately proves a mismatched uid/gid is refused, but no drill ran as the image account itself.
- **Pseudo-volume waiver applied**: data/state are on data=overlay, state=overlay, not a reviewed persistent volume. Checks that depend on volume durability across power loss, real ENOSPC, or remount are out of scope for this run. `space-pressure` uses RLIMIT_FSIZE, which shares SQLite's SQLITE_IOERR_WRITE path but is not a full-disk simulation.

## Drills

### identity-gate — PASS

_What this proves:_ uid/gid/root and volume permission gates

- PASS `correct_identity_accepted`
- PASS `root_refused`
- PASS `uid_mismatch_refused`
- PASS `world_writable_refused`

### network-fs-guard — PASS

_What this proves:_ network/pseudo filesystems rejected, local filesystems accepted

- PASS `network_fs_rejected`
- PASS `local_fs_accepted`
- PASS `data_volume_classified`
- PASS `state_volume_classified`

### normal-stop — PASS

_What this proves:_ clean SIGTERM stop: commit durability, checkpoint, recovery

- PASS `exit_code_zero`
- PASS `clean_marker_written`
- PASS `marker_after_close_reports_head`
- PASS `checkpoint_completed_before_close`
- PASS `wal_empty_at_rest`
- PASS `sigterm_not_sigkill`
- PASS `commits_recorded`
- PASS `recovery_integrity_ok`
- PASS `recovery_fingerprint_matches`
- PASS `log_contiguous_after_recovery`
- PASS `index_matches_documents_after_recovery`
- PASS `all_reported_commits_present`

### sigkill — PASS

_What this proves:_ SIGKILL stop: WAL recovery and bounded committed head

- PASS `killed_by_sigkill`
- PASS `no_clean_marker`
- PASS `wal_present_at_crash`
- PASS `recovery_integrity_ok`
- PASS `recovered_head_at_least_last_reported`
- PASS `recovered_head_is_bounded`
- PASS `commits_visible`
- PASS `every_reported_commit_present`
- PASS `log_contiguous_after_recovery`
- PASS `index_matches_documents_after_recovery`

### torn-commit — PASS

_What this proves:_ SIGKILL mid-transaction: durable uncommitted frames never become visible

- PASS `child_was_killed`
- PASS `uncommitted_frames_were_durable`
- PASS `head_unchanged`
- PASS `phantom_document_absent`
- PASS `confirmed_document_present`
- PASS `log_has_no_uncommitted_revision`
- PASS `index_has_no_phantom_row`
- PASS `integrity_ok`

### write-conflict — PASS

_What this proves:_ second writer gets a bounded busy error, then succeeds

- PASS `second_writer_blocked`
- PASS `error_is_busy`
- PASS `waited_about_busy_timeout`
- PASS `wrote_after_release`
- PASS `document_is_new_value`

### long-reader-checkpoint — PASS

_What this proves:_ long reader blocks checkpoint without an error; only readiness catches it

- PASS `checkpoint_did_not_raise`
- PASS `checkpoint_lagged_while_reader_held`
- PASS `wal_grew_while_blocked`
- PASS `naive_checks_all_passed`
- PASS `liveness_still_healthy`
- PASS `readiness_degraded_while_reader_held`
- PASS `checkpoint_completed_after_release`
- PASS `readiness_healthy_after_release`

### http-readiness — PASS

_What this proves:_ real /healthz stays 200 while /readyz returns 503 under a long read

- PASS `started_healthy`
- PASS `writes_accepted_while_reader_held`
- PASS `liveness_200_while_reader_held`
- PASS `readiness_503_while_reader_held`
- PASS `readiness_reports_degraded`
- PASS `service_exited_on_sigterm`
- PASS `restart_is_healthy`
- PASS `data_survived_restart`

### space-pressure — PASS

_What this proves:_ disk exhaustion analog preserves committed data and refuses low-space start

- PASS `write_failed_with_io_error`
- PASS `committed_head_survived`
- PASS `integrity_ok_after_failure`
- PASS `start_gate_refuses_low_space`
- PASS `no_partial_commit_visible`
