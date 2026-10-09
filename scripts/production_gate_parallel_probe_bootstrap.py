"""Admission-before-fork/exec source for isolated H/G/payload probe stages.

NOT integrated or execution-admitted. An independent owner endpoint is mandatory:
each stage must receive both its pinned parent's ACK and a root-owner ACK for the
exact self pidfd before proceeding. A measured payload receives neither keys nor
control descriptors. Root loss is containment-attempt evidence, never owner proof.
"""

from scripts.production_gate_parallel_probe_custody import CUSTODY_SOURCE

CONTROL_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
CONTROL_ENVIRONMENT.update(
    dict.fromkeys(
        (
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "RAYON_NUM_THREADS",
            "BLIS_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
        ),
        "1",
    )
)

ADMISSION_SOURCE = r"""
import array, ctypes, errno, hashlib, hmac, json, math, os, select, signal, socket, subprocess, sys, time

MAX_FRAME = 65536
role, parent_fd = sys.argv[1], int(sys.argv[2])
parent_pid, parent_start = int(sys.argv[3]), int(sys.argv[4])
job_deadline, cleanup_deadline = float(sys.argv[5]), float(sys.argv[6])
if role not in ('holder', 'guardian', 'payload') or not all(
        math.isfinite(v) for v in (job_deadline, cleanup_deadline)):
    raise SystemExit(70)
me, stopped, sequence = os.getpid(), [], 0
root = None
config, key = {}, b''


def stat(pid):
    try:
        with open('/proc/%d/stat' % pid, 'rb') as stream:
            fields = stream.read().decode('utf-8', 'replace').rsplit(')', 1)[1].split()
        return int(fields[19]), int(fields[1]), fields[0]
    except (OSError, ValueError, IndexError):
        return None


def parent_matches():
    found = stat(parent_pid)
    return os.getppid() == parent_pid and found is not None and found[0] == parent_start


libc = ctypes.CDLL(None, use_errno=True)
guard = signal.SIGKILL if role == 'payload' else signal.SIGTERM
guard_readback = ctypes.c_int(0)
if (libc.prctl(1, guard, 0, 0, 0) != 0
        or libc.prctl(2, ctypes.byref(guard_readback), 0, 0, 0) != 0
        or guard_readback.value != guard or not parent_matches()):
    raise SystemExit(70)  # BEFORE startup IO and BEFORE any fork or target side effect
start_info = stat(me)
if start_info is None:
    raise SystemExit(70)
start = start_info[0]
if role != 'payload':
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    def stop_signal(number, _frame):
        if not stopped:
            stopped.append(number)
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, stop_signal)


def subreaper_on():
    flag = ctypes.c_int(0)
    return libc.prctl(37, ctypes.byref(flag), 0, 0, 0) == 0 and flag.value == 1


if role != 'payload' and (libc.prctl(36, 1, 0, 0, 0) != 0 or not subreaper_on()):
    raise SystemExit(70)
parent = socket.socket(fileno=parent_fd)
parent.setblocking(False)
if len(sys.orig_argv) < 5 or sys.orig_argv[1:4] != ['-I', '-S', '-c']:
    raise SystemExit(70)
source, control_python = sys.orig_argv[4], os.path.realpath(sys.executable)
source_sha256 = hashlib.sha256(source.encode()).hexdigest()
control_environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}
for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS',
             'RAYON_NUM_THREADS', 'BLIS_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
    control_environment[name] = '1'


def check_success():
    if stopped or not parent_matches() or time.monotonic() >= min(job_deadline, cleanup_deadline):
        raise RuntimeError('original probe admission/success budget refused')


def unique_pairs(pairs):
    values = {}
    for name, value in pairs:
        if name in values:
            raise ValueError('duplicate frame field')
        values[name] = value
    return values


def decode(raw):
    if not raw or len(raw) > MAX_FRAME:
        raise RuntimeError('missing/oversize control frame')
    body = json.loads(raw, object_pairs_hook=unique_pairs,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    if not isinstance(body, dict):
        raise RuntimeError('control frame is not an object')
    return body


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def send_bytes(channel, raw, *, fd=None):
    if len(raw) > MAX_FRAME:
        raise RuntimeError('control frame bound exceeded')
    ancillary = [] if fd is None else [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))]
    while True:
        check_success()
        try:
            size = channel.sendmsg([raw], ancillary)
            if size != len(raw):
                raise RuntimeError('partial control frame')
            return
        except BlockingIOError:
            select.select([], [channel], [], 0.01)


def receive_bytes(channel):
    while True:
        check_success()
        try:
            raw, _ancillary, flags, _address = channel.recvmsg(MAX_FRAME + 1)
        except BlockingIOError:
            select.select([channel], [], [], 0.01)
            continue
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise RuntimeError('truncated control frame')
        return decode(raw)


def signed_body(phase, extra, role_override=None, instance_override=None):
    global sequence
    sequence += 1
    pid, stamp = (me, start) if instance_override is None else instance_override
    body = dict(extra, phase=phase, scope=config['scope'], nonce=config['nonce'],
                role=role if role_override is None else role_override,
                pid=pid, start=stamp, source_sha256=source_sha256,
                program_sha256=config['program_sha256'], sequence=sequence)
    body['mac'] = hmac.new(key, canonical(body), hashlib.sha256).hexdigest()
    return body


def send_authenticated(channel, phase, extra, *, role_override=None, instance_override=None, fd=None):
    body = signed_body(phase, extra, role_override, instance_override)
    send_bytes(channel, canonical(body), fd=fd)


def receive_authenticated(channel, expected_role, pid, stamp, phase):
    body = receive_bytes(channel)
    tag = body.pop('mac', None)
    if not isinstance(tag, str) or not hmac.compare_digest(
            tag, hmac.new(key, canonical(body), hashlib.sha256).hexdigest()):
        raise RuntimeError('control frame authentication failed')
    expected = dict(scope=config['scope'], nonce=config['nonce'], role=expected_role,
                    pid=pid, start=stamp, phase=phase, source_sha256=source_sha256,
                    program_sha256=config['program_sha256'])
    if any(body.get(name) != value for name, value in expected.items()):
        raise RuntimeError('control frame instance/phase mismatch')
    return body


def send_initial(channel, body):
    send_bytes(channel, canonical(body))


def register_root():
    global root
    root = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    root.setblocking(False)
    result = root.connect_ex(config['root_endpoint'])
    if result not in (0, errno.EINPROGRESS, errno.EAGAIN):
        raise OSError(result, 'independent owner connection')
    while result:
        check_success()
        if select.select([], [root], [], 0.01)[1]:
            result = root.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    import struct
    peer, _uid, _gid = struct.unpack('3i', root.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    owner = stat(peer)
    if peer != config['root_pid'] or owner is None or owner[0] != config['root_start']:
        raise RuntimeError('independent root-owner instance mismatch')
    fd = os.pidfd_open(me)
    try:
        send_authenticated(root, 'register', {'parent_pid': parent_pid, 'parent_start': parent_start,
                           'job_deadline': job_deadline, 'cleanup_deadline': cleanup_deadline,
                           'guard': guard, 'subreaper': role != 'payload'}, fd=fd)
        ack = receive_authenticated(root, role, me, start, 'root_ack')
        if (ack.get('retained') is not True or ack.get('root_pid') != config['root_pid']
                or ack.get('root_start') != config['root_start']):
            raise RuntimeError('independent root retention was not acknowledged')
    finally:
        os.close(fd)  # root must ACK retention of its own SCM_RIGHTS duplicate


def send_diagnostic(phase, extra):
    if root is None:
        return
    try:
        raw = canonical(signed_body(phase, extra))
        if len(raw) <= MAX_FRAME:
            root.send(raw)  # single nonblocking failure update; never a new success allowance
    except (OSError, ValueError):
        pass  # custody continues; a missing diagnostic is not root proof


def service_control():
    global cleanup_deadline
    for channel in (parent, root):
        if channel is None:
            continue
        try:
            raw = channel.recv(MAX_FRAME + 1)
        except BlockingIOError:
            continue
        except OSError:
            request_stop('control channel unreadable')
            continue
        if not raw:
            request_stop('control owner channel lost')
            continue
        try:
            body = decode(raw)
            tag = body.pop('mac', None)
            if not isinstance(tag, str) or not hmac.compare_digest(
                    tag, hmac.new(key, canonical(body), hashlib.sha256).hexdigest()):
                raise RuntimeError('stop frame authentication failed')
            if (body.get('scope') != config['scope'] or body.get('nonce') != config['nonce']
                    or body.get('role') != role or body.get('pid') != me or body.get('start') != start
                    or body.get('phase') != 'stop'):
                raise RuntimeError('stop frame binding failed')
            request_stop('authenticated owner stop')
            bound = body.get('cleanup_deadline', cleanup_deadline)
            if type(bound) not in (int, float) or not math.isfinite(bound):
                raise RuntimeError('stop cleanup bound is unknown')
            cleanup_deadline = min(cleanup_deadline, bound)
        except (ValueError, RuntimeError):
            request_stop('malformed control frame')
"""

MAIN_SOURCE = r"""
child_channel = None
try:
    config = receive_bytes(parent)
    if (config.get('role') != role or config.get('source_sha256') != source_sha256
            or config.get('job_deadline') != job_deadline
            or config.get('cleanup_deadline') != cleanup_deadline):
        raise RuntimeError('initial source/budget binding failed')
    key = bytes.fromhex(config['secret'])
    if len(key) != 32 or not math.isfinite(config['grace_seconds']) or not 0 <= config['grace_seconds'] <= 5:
        raise RuntimeError('invalid private admission configuration')
    program = {'command': config['command'], 'environment': config['environment'], 'cwd': config['cwd']}
    if hashlib.sha256(canonical(program)).hexdigest() != config['program_sha256']:
        raise RuntimeError('measured program/environment binding failed')
    send_authenticated(parent, 'ready', {'guard': guard, 'subreaper': role != 'payload'})
    ack = receive_authenticated(parent, role, me, start, 'parent_ack')
    if (ack.get('retained') is not True or ack.get('parent_pid') != parent_pid
            or ack.get('parent_start') != parent_start):
        raise RuntimeError('original parent retention was not acknowledged')
    register_root()  # root has the exact self pidfd BEFORE any stage fork or target exec
    check_success()
    if role == 'payload':
        command, environment = config['command'], config['environment']
        if (not isinstance(command, list) or not command
                or any(not isinstance(value, str) or '\0' in value for value in command)
                or not isinstance(environment, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) or '\0' in k + v
                       for k, v in environment.items())):
            raise RuntimeError('measured command/environment malformed')
        if any(key.hex() in value for value in environment.values()):
            raise RuntimeError('probe key must not reach the measured environment')
        os.chdir(config['cwd'])
        check_success()
        parent.close()
        root.close()
        # Only stdin/out/err survive: Popen passed solely this private parent socket;
        # the root socket and self pidfd are closed. Probe keys are not target env.
        check_success()
        os.execvpe(command[0], command, environment)
    child_channel = spawn_stage('guardian' if role == 'holder' else 'payload')
except BaseException as error:
    record_error(operation_errors, type(error).__name__ + ': ' + str(error))
    request_stop('admission or launch refused')

if role == 'payload':
    raise SystemExit(70)  # failed exec/admission: no descendants were created here
sweep()
physical_empty = exhausted() and not held
now = time.monotonic()
proof = bool(physical_empty and subreaper_on() and not custody_errors and now < cleanup_deadline)
success = bool(proof and not operation_errors and direct is not None
               and direct.returncode == 0 and now < job_deadline)
body = {'parent_pid': parent_pid, 'parent_start': parent_start,
        'subreaper_start': True, 'subreaper_end': subreaper_on(),
        'physical_empty': physical_empty, 'echild': physical_empty,
        'proof': proof, 'success': success, 'forks': forks, 'adopted': adopted,
        'reaped': reaped, 'killed': killed, 'remaining': list(held),
        'custody_errors': custody_errors, 'operation_errors': operation_errors,
        'child_status': direct.returncode if direct is not None else None,
        'job_deadline': job_deadline, 'cleanup_deadline': cleanup_deadline}
send_diagnostic('final', body)
if child_channel is not None:
    child_channel.close()
parent.close()
if root is not None:
    root.close()
raise SystemExit(0 if success else 1)
"""

PROBE_NODE_SOURCE = ADMISSION_SOURCE + CUSTODY_SOURCE + MAIN_SOURCE
