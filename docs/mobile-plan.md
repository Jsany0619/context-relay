# Android remote companion implementation plan

**Goal:** Install an Android companion that can select existing Context Relay tasks, inspect their progress, send an explicit continuation, pause, answer current requests and use brief/review decisions from the phone.

**Architecture:** The Android UI connects over authenticated HTTPS to an explicitly enabled gateway inside the existing Windows process. One CommandWorker retains exclusive Manager access. The phone never opens Codex RPC or a second task database owner. Desktop imports remain the entry point for existing Codex chats; this does not control a concurrently active official Desktop chat.

**Stack:** Python standard library, existing SQLite/Tk/Manager, OpenSSL for a local identity certificate, native Android Java and Android SDK tools. No cloud account, Google runtime, public relay or new application framework is required for LAN use. An independently configured private network can carry the same HTTPS connection across networks.

## Decisions and limits

- Native Android is selected over a browser wrapper so certificate identity and credentials can be checked and stored with Android platform facilities. A full remote-desktop streaming product is outside this task.
- Follow TRAE's phone-control / computer-execution division, based on its public documentation; reuse no proprietary code, brand or assets.
- Remote access defaults off, exists only while the desktop manager runs, and does not alter task permission ceilings or automatically retry uncertain operations.
- Pairing text/deep link contains the HTTPS endpoint, SHA-256 of the exact local certificate and a single-use random secret valid for five minutes. Credentials use bearer authentication after pairing and are revocable on the PC. Certificate replacement requires new pairing; no trust-all certificate fallback.
- The Android app requests only network access. Pair by pasting the complete pairing text, opening a pairing link, or using the phone's existing scanner if it supports the link. No Google-dependent scanner or speech service is required; keyboard dictation remains an editable draft.
- First ship same-network access and compatibility with a private-network IP. Cross-network account sign-in, Android installation confirmation and actual Android hardware acceptance require the user's own device interaction. Do not describe emulator success as phone or mobile-network success.
- Do not publish private pairing material, tokens, keys, device information or task contents. Keep generated APK/build products separate from source and signing keys outside the repository.

## Protocol

Pair URI: `contextrelay://pair#BASE64URL(JSON)` where JSON is exactly `{version:1, endpoint:"https://HOST:PORT", certificate_sha256:"64 hex digits", secret:"random"}`.

- `POST /v1/pair`: `{secret, device_name}` -> `{device_id, token}`.
- All remaining endpoints require `Authorization: Bearer TOKEN`.
- `GET /v1/status`: online state and snapshot cursor.
- `GET /v1/tasks`: `{tasks:[task], cursor}`; `GET /v1/tasks/{id}`: task detail.
- `POST /v1/commands`: `{request_id, task_id, command, expected_etag, payload}` -> durable receipt, HTTP 202.
- `GET /v1/commands/{request_id}`: the authenticated device's receipt. Timeout is not failure proof; query, never invent a new request ID to retry.
- Commands: `start`, `pause`, `reconcile`, `answer`, `analyze`, `adopt_brief`, `accept_review`, `revise_from_review`. Payload shapes match the existing Manager operations. No arbitrary methods, filesystem paths, new permission settings or native RPC pass-through.
- Task DTO contains user-visible state, message, budget, current approval details and assessment candidates. Omit internal native thread IDs, authority receipts, source transcripts and snapshot paths. Truncated or incomplete approval evidence cannot be accepted.
- ETag binds the canonical task state and native connection nonce. Check it on acceptance and again immediately before execution on the Worker. Duplicate IDs with identical content return the stored receipt; differing content is rejected.
- A local sidecar operation ledger persists accepted/running/succeeded/failed/unknown states, token hashes and revocations. After restart, unfinished receipts become unknown and are never replayed. The ledger is excluded from task backup, so restoring historical backups cannot recreate remote credentials or replay phone commands.

## Work and verification

- [x] Gateway: HTTPS identity, pairing/revocation, bounded JSON interface, minimized snapshots, durable duplicate protection and worker-only dispatch. Wrong/expired credentials, stale state, request collision, restart uncertainty, missing approval evidence and shutdown checked using fake Manager operations and real TLS.
- [x] Desktop: Phone Connection window, local address/port, expiring pairing text, connected-device list and stop/revoke actions. Existing shortcut and worker reused; UI result isolation and queued-command revocation checked.
- [x] Android: task selection, reports, sealed drafts and request identity, explicit control/assessment commands, pinned certificate, redirect/cleartext rejection, Keystore storage and backup exclusion implemented.
- [x] Package: sideloadable APK built and verified, with an out-of-repository local test-signing identity. Build instructions and exact hash recorded.
- [x] Local integration: disposable projects, real Android emulator, TLS/Tk/Manager flows and one actual read-only Codex response verified. Uncertain results, duplicate identity, stale approval and pin mismatch have automated coverage; physical network loss is not a field-tested guarantee. See [validation](mobile-validation.md) for the separate evidence levels.
- [ ] Delivery: record exactly what passed, provide APK and PC launch steps, test actual phone pairing when accessible, and publish only authorized source/artifacts without local secrets.

## Primary references

- [TRAE mobile architecture and pairing](https://docs.trae.cn/work_get-started-with-trae-mobile)
- [Android network security configuration](https://developer.android.com/privacy-and-security/security-config)
- [Tailscale Windows setup](https://tailscale.com/docs/install/windows) and [Android setup](https://tailscale.com/docs/install/android)

Public cloud quick tunnels were considered but are not the default: they add a public endpoint and third-party traffic handling; their temporary hostnames and lack of uptime guarantees are also a poor default for this personal controller.
