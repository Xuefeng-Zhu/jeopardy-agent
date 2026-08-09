# Security

Jeopardy Agent processes organizer-controlled prompts, files, archives, HTML,
and model tool calls. Treat all task material as untrusted.

## Trust boundaries

| Boundary | Trusted for |
|---|---|
| Event API | Board state, task metadata, submission outcomes, and model proxy routing |
| Agent controller | Scheduling, cancellation, verification, and submission authority |
| Model | Proposing tool calls and evidence; never direct submission authority |
| Task files and web pages | Input data only; never instructions that override runtime policy |
| Hosted container | OS-level isolation from the operator's workstation |

## Implemented controls

### Credentials and transport

- Required credentials come from environment variables.
- The API client uses the `X-Api-Key` header.
- Missing account-specific board data is treated as an authentication failure.
- The documented configuration uses HTTPS.
- The Python subprocess environment intentionally omits the team and model
  credentials.

### Filesystem and archives

- Tool paths are resolved under one task workspace.
- Archive extraction rejects path traversal, links, excessive member counts,
  and expanded data beyond 16 MB.
- Downloads are written to a temporary file and atomically replaced only after
  a non-empty, successful transfer.
- Stable task directories prevent retries from multiplying downloaded data.

### Network

- Model HTTP tools accept only HTTP(S).
- The initial request URL must use the configured event hostname.
- Named sessions preserve cookies only within one task runtime.
- Response bodies are capped and stored as internal artifacts; model context
  receives compact JSON shape, form/link metadata, or text samples.

### Model and answer integrity

- The model receives no submit tool.
- `record_answer` is the only model-side completion path.
- Artifact extraction preserves exact values without model retyping.
- Safe-regex checks reject backreferences, lookarounds, nested quantifiers,
  quantified alternations, repeated unbounded wildcards, and oversized
  patterns.
- High-value scored candidates require an answer source and a distinct
  verification-artifact source. The runtime asks for an independently derived
  check but cannot prove semantic independence from artifact IDs alone.
- A failed recipe canary invalidates queued siblings from the same generation.

### Resource bounds

- Tool responses, artifact bodies, file reads, regex lines, and prompt content
  are capped.
- Python execution has a timeout and best-effort CPU, address-space, file-size,
  file-descriptor, and process-count limits where the platform supports them.
- Process groups are killed on timeout or cancellation.
- Archive file count and expansion are bounded.
- Model turns and response tokens are bounded.
- CPU-heavy handlers and the submission lane have explicit concurrency limits.

Effective limits and model-tool schema constraints include:

| Resource | Limit |
|---|---:|
| Serialized tool response | 8,000 characters before compaction |
| Stored artifact text | 250,000 characters |
| HTTP body read before decoding | 250,000 bytes |
| Cumulative targeted reads per caller-supplied path string and task runtime | 12,000 bytes |
| One `read_file` window | 4,000 bytes |
| File considered by `search_files` | 8,000,000 bytes |
| Archive members | 256 files |
| Expanded archive data | 16,000,000 bytes |
| `.json` file structurally parsed by `inspect_file` | 1,000,000 bytes |
| Regex pattern | 300 characters |
| One searched physical line | 4,096 characters |
| Schema-declared `write_file` content | 200,000 characters |
| Schema-declared `run_python` source | 100,000 characters |
| `run_python` wall timeout | 60 seconds |
| Python address space, file output, descriptors, processes | Best effort: 1.5 GiB, 32 MiB, 64, 64 |

The HTTP tool schema accepts timeout values through 60 seconds, but the current
implementation passes a five-second Requests connect/read timeout. This is an
inactivity timeout, not a five-second total wall-clock deadline for a streamed
response.
The `write_file` and `run_python` source lengths rely on model-tool schema
validation; their handlers do not repeat those length checks. Read accounting
uses the path spelling supplied by the caller, so aliases such as `foo` and
`./foo` receive separate 12,000-byte budgets even when they resolve to the same
physical file.

## Known limitations

### `run_python` is not a complete workstation sandbox

The Python tool uses isolated interpreter mode, a task working directory,
scrubbed credentials, resource limits, and a disposable process. It does not
implement a complete OS filesystem sandbox. Arbitrary Python may still read
paths permitted to the agent process.

Run this agent in the supplied hosted container or another disposable,
least-privilege environment. Do not run model-driven tasks on a workstation
that contains sensitive files accessible to the Python process.

### Redirects are not a complete same-host boundary

The runtime validates the hostname of an initial model-tool request and the
explicit links selected by deterministic handlers. HTTP redirects are followed
by default, and the final redirect target is not revalidated. The same hostname
is also accepted with alternate ports or HTTP.

Treat the configured event service and its redirects as trusted network
infrastructure. Run the agent in an isolated environment without access to
sensitive internal services. A hard same-host boundary would require disabling
automatic redirects and validating every `Location` hop before following it.

### Structural verification is not an oracle

The verifier can reject malformed or weak candidates, but it cannot compare an
answer with the hidden ground truth. Deterministic handlers must therefore
match complete generator invariants and fail inconclusively on ambiguity.

### In-memory trust resets on restart

Recipe success, canary state, and replay caches do not persist. Restarting a
hosted process loses those learned safety signals.

## Secret-response procedure

If a credential is exposed:

1. Stop the affected agent if it is actively using the credential.
2. Rotate or replace the key through the event's supported mechanism.
3. Remove it from the working tree and generated archives.
4. Inspect the entire Git history and hosted logs.
5. Treat prior commits and already-shared archives as compromised even after a
   cleanup commit.
6. Verify the replacement with a one-tile dry run.

Do not attempt to recover or display a missing key from logs or Git history.
