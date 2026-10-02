# Opt-in fixed-history profiling foundation

This separate command observes an explicitly supplied ordinary public-input
history. It does not change the default MCP server, qualification controller,
public API, 20-second RPC deadlines, or 1200-second row cutoff.

No qualified real profile has been captured. Issues #85/#86 require an admitted
reserved environment before this command launches an owner; #109/#110 remain
open. Historical 421 receipts remain attributed to their original source and
lack direct completed-trade per-owner imported-module telemetry.

Run the supported source or native interpreter from a clean exact commit:

```text
python -m scripts.profile_normal_red_link --expected-head <40-hex-commit> \
  --runtime source --transport local --rom <operator-ROM> --symbols <operator-SYM> \
  --fixture-root <operator-root> --history <private-history.json> \
  --runner-declaration <admitted-declaration.json> --capacity-policy <policy.json> \
  --output <fresh-directory-outside-checkout> --cprofile
```

Use the appropriate pinned environment for each runtime, never a mixed wheel
and source environment. Before any owner launch the command verifies clean
HEAD, five baseline controller blobs, actual imported PyBoy class/core file
origins and SHA256 hashes, version and revision. The owner wrapper repeats
actual runtime measurements inside each server process. Environment build flags
alone do not establish source execution. Unknown runtime origins fail closed.

History schema is `{"history_version":1,"operations":[...]}`. First operation
must be `{"operation":"link_up"}` (optional boolean `arm_barrier`). Later
operations: `step` with integer `count` 1..120; `press` with owner 0/1, public
button and duration 1..60; `release` with owner/button; `state`, `records`, or
`rom_events` with owner. No writes, private hooks, arbitrary calls, or additional
arguments. Maximum 50000 operations and 18000 requested pair frames. Histories
must be retained privately and compared byte-for-byte across measured rows;
completion alone establishes no trade or battle acceptance.

RPC files record monotonic wall time, controller process CPU, Linux server CPU
samples, actual returned step ticks/counters, sanitized errors and result hashes.
Receipts retain verified ROM/SYM/runtime pins and fixture size/digest, never
asset bytes. Cumulative CPU samples from overlapping RPC intervals cannot be
summed; aggregate by the single process/start identity and measured interval.
Controller CPU excludes server CPU. LOCAL uses one server for both owners:
its samples are pair-process CPU and must be deduplicated by PID/start identity,
never summed twice or ascribed to individual emulators. TCP has two processes.
Missing samples or PID reuse are explicitly unknown. Query payloads, party
records, states and ROM bytes are excluded from telemetry files.

Optional cProfile measures server-main-thread Python calls only. Owner-worker
call-stack attribution and compiled native instructions are explicitly
unavailable. Python 3.12 simultaneous main/thread cProfile instances compete for
a monitoring slot; this wrapper never intercepts worker execution. Process CPU
still includes workers, without assigning their CPU to Python functions. These
profiles introduce observer overhead and do not identify native instruction
hotspots. Output stays outside Git; MCP stdout remains protocol-only. Preserve
failure, cancellation, teardown, capacity and reservation receipts. Do not
upload private histories or assets.
