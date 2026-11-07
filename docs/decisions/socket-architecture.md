# Decision: fixed private user-service socket

- Decided: 2026-07, re-confirmed 2026-11-07 for 0.8.0 (commit 9f3b6ca).
- Probe: `python3 tests/probe_socket_architecture.py` (writes
  `tests/evidence/socket-architecture/gate-<UTC>/`).
- Result on 2026-11-07 (`gate-20261107T193604Z`): the production fixed
  socket passed; selected candidate `fixed_private_user_service_socket`;
  cancellation exercised with the real server, the production handler and
  lookup, and external fake processes; no failed cases.

This is a one-time architecture decision, not per-release evidence. Issue
#22 plans to replace `dank-lookup backend-request` with a systemd
socket-activated server that QML reaches directly. That change needs a new
probe of the new architecture; this record then becomes history.
