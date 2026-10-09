"""Trusted isolated probe-stage custody source; never imported by measured payloads.

The holder/guardian are single-purpose, single-threaded subreapers. Their own
children are exclusively the admitted probe lineage. Unknown listings/instances
withhold proof; every signal uses a retained and revalidated pidfd. Bounded refusal
does not discard ownership: the stage reports UNPROVEN and continues custody until
its child wait is exhausted. There is no numeric-PID signalling fallback.
"""

CUSTODY_SOURCE = r"""
held, direct, forks, adopted, reaped, killed = {}, None, [], [], [], []
custody_errors, operation_errors = [], []
stop_at, unproven_sent = None, False
MAX_INSTANCES, MAX_ERRORS = 256, 64


def record_error(errors, text):
    text = str(text)[:240]
    if text not in errors and len(errors) < MAX_ERRORS:
        errors.append(text)


def entry_fd(entry):
    info = os.fstat(entry['fd'])
    if (info.st_dev, info.st_ino) != entry['binding']:
        raise RuntimeError('held descriptor binding changed')
    return entry['fd']


def exited_entry(entry):
    poller = select.poll()
    poller.register(entry_fd(entry), select.POLLIN)
    events = poller.poll(0)
    if any(bits & (select.POLLERR | select.POLLNVAL) for _, bits in events):
        raise RuntimeError('held pidfd state unknown')
    return any(bits & select.POLLIN for _, bits in events)


def pin_child(pid, *, process=None):
    first = stat(pid)
    if first is None or first[1] != me:
        raise RuntimeError('owned child start/parent identity unavailable')
    fd = os.pidfd_open(pid)
    entry = {'fd': fd, 'pid': pid, 'start': first[0], 'process': process,
             'binding': None, 'validated': False}
    held[pid] = entry  # retain ownership immediately after allocation
    info = os.fstat(fd)
    entry['binding'] = (info.st_dev, info.st_ino)
    # Retain first: validation failure cannot discard a possibly live instance.
    again = stat(pid)
    entry['validated'] = again is not None and again[:2] == first[:2]
    if not entry['validated']:
        raise RuntimeError('owned child changed or became unreadable while pinning')
    return entry


def children_now():
    found = set()
    for task in os.listdir('/proc/self/task'):
        path = '/proc/self/task/' + task + '/children'
        try:
            with open(path, encoding='ascii') as stream:
                text = stream.read()
        except FileNotFoundError:
            if os.path.exists('/proc/self/task/' + task):
                raise
            continue
        for item in text.split():
            if not item.isdigit():
                raise RuntimeError('owned children listing is malformed')
            found.add(int(item))
    return found


def exhausted():
    try:
        result = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return True
    # None means live children still exist. A waitable child is not exhausted.
    return False


def observe_children():
    try:
        kids = children_now()
    except (OSError, ValueError, RuntimeError) as error:
        record_error(custody_errors, 'children listing unknown: ' + str(error))
        return
    for pid in kids:
        if pid in held:
            continue
        if len(held) >= MAX_INSTANCES:
            record_error(custody_errors, 'owned instance bound exceeded')
            request_stop('owned instance bound exceeded')
            break
        try:
            process = direct if direct is not None and pid == direct.pid else None
            entry = pin_child(pid, process=process)
            pair = [pid, entry['start']]
            target = forks if process is not None else adopted
            if pair not in target:
                if len(forks) + len(adopted) >= MAX_INSTANCES:
                    record_error(custody_errors, 'instance accounting bound exceeded')
                    request_stop('instance accounting bound exceeded')
                else:
                    target.append(pair)
        except (OSError, ValueError, RuntimeError) as error:
            record_error(custody_errors, 'child pin unknown: ' + str(error))


def reap_entries():
    for pid, entry in list(held.items()):
        try:
            if not exited_entry(entry):
                continue
            result = os.waitid(os.P_PIDFD, entry_fd(entry), os.WEXITED | os.WNOHANG)
            if result is None:
                continue
            status = result.si_status if result.si_code == os.CLD_EXITED else -result.si_status
            if entry['process'] is not None:
                entry['process'].returncode = status
            pair = [pid, entry['start']]
            if pair not in reaped:
                reaped.append(pair)
            entry['reaped'] = True
            os.close(entry_fd(entry))
            del held[pid]
        except (OSError, ValueError, RuntimeError) as error:
            record_error(custody_errors, 'reaping/release unknown: ' + str(error))


def signal_entries(number):
    for entry in list(held.values()):
        try:
            if not entry.get('validated') or entry.get('reaped') or exited_entry(entry):
                continue
            signal.pidfd_send_signal(entry_fd(entry), number)
            pair = [entry['pid'], entry['start']]
            if number == signal.SIGKILL and pair not in killed:
                killed.append(pair)
        except (OSError, ValueError, RuntimeError) as error:
            record_error(custody_errors, 'owned signal failed: ' + str(error))


def request_stop(reason):
    global stop_at
    record_error(operation_errors, reason)
    if stop_at is None:
        stop_at = time.monotonic()


def spawn_stage(child_role):
    global direct
    peer, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    peer.setblocking(False)
    child.setblocking(False)
    try:
        check_success()
        direct = subprocess.Popen(
            [control_python, '-I', '-S', '-c', source, child_role,
             str(child.fileno()), str(me), str(start), repr(job_deadline), repr(cleanup_deadline)],
            pass_fds=(child.fileno(),), start_new_session=True,
            stdin=subprocess.DEVNULL, env=control_environment,
        )
        # The unreaped direct Popen instance is retained even if pin acquisition fails.
        child.close()
        entry = pin_child(direct.pid, process=direct)
        forks.append([direct.pid, entry['start']])
        send_initial(peer, dict(config, role=child_role))
        ready = receive_authenticated(peer, child_role, direct.pid, entry['start'], 'ready')
        guard = signal.SIGKILL if child_role == 'payload' else signal.SIGTERM
        if ready.get('guard') != guard or ready.get('subreaper') is not (child_role != 'payload'):
            raise RuntimeError('child guard/adoption readiness is unproven')
        check_success()
        send_authenticated(peer, 'parent_ack', {'retained': True, 'parent_pid': me, 'parent_start': start}, role_override=child_role,
                           instance_override=(direct.pid, entry['start']))
        return peer
    except BaseException:
        peer.close()  # no ACK: trusted child cannot fork/exec and sees EOF
        raise
    finally:
        child.close()


def sweep():
    global unproven_sent
    while True:
        observe_children()
        # Missing initial pidfd never permits a signal or child admission. Reaping
        # our original direct Popen remains valid while it is unreaped.
        if direct is not None and direct.pid not in held and direct.returncode is None:
            direct.poll()
        reap_entries()
        now = time.monotonic()
        if stopped or not parent_matches() or now >= min(job_deadline, cleanup_deadline):
            request_stop('stop, owner loss, or original deadline exhausted')
        if direct is not None and direct.returncode is not None:
            if direct.returncode != 0:
                request_stop('direct stage exited nonzero: ' + str(direct.returncode))
            if held:
                request_stop('descendants outlived direct stage')
        if stop_at is not None:
            scale = 6 if role == 'holder' else 1
            term_end = min(cleanup_deadline, stop_at + scale * (config.get('grace_seconds', 0.5) + 1.0))
            signal_entries(signal.SIGTERM if now < term_end else signal.SIGKILL)
        try:
            echild = exhausted()
        except OSError as error:
            echild = False
            record_error(custody_errors, 'exhausted wait unknown: ' + str(error))
        if echild and not held:
            return
        if now >= cleanup_deadline and not unproven_sent:
            unproven_sent = True
            send_diagnostic('unproven', {'remaining': list(held), 'reason': 'cleanup bound spent'})
        service_control()
        time.sleep(0.01)  # continuing custody; not a renewed success/cleanup allowance
"""
