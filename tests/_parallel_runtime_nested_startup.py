"""Trusted, single-threaded fixture startup; strings are never executed on import.

The child inherits a parent-death guard installed before exec. It clears the
guard only after the outer holder has retained both pidfds and acknowledged them.
Startup controls remain independent of product containment evidence.
"""

NESTED_CHILD_SOURCE = """
import ctypes, os, select, signal, sys, time
libc = ctypes.CDLL(None, use_errno=True)
flag = ctypes.c_int(0)
release, confirm, parent = map(int, sys.argv[1:4])
if libc.prctl(2, ctypes.byref(flag), 0, 0, 0) != 0 or flag.value != signal.SIGKILL or os.getppid() != parent:
    os._exit(4)
os.write(confirm, b"G")
if not select.select([release], [], [], 3.0)[0] or os.read(release, 1) != b"R":
    os._exit(5)
if libc.prctl(1, 0, 0, 0, 0) != 0:
    os._exit(6)
flag.value = -1
if libc.prctl(2, ctypes.byref(flag), 0, 0, 0) != 0 or flag.value != 0:
    os._exit(7)
os.write(confirm, b"U")
os.close(release)
os.close(confirm)
time.sleep(120)
"""

NESTED_SOURCE = """
import array, ctypes, json, os, select, signal, socket, subprocess, sys, time
CHILD_SOURCE = __CHILD_SOURCE__
root = sys.argv[1]
mode = sys.argv[4] if len(sys.argv) > 4 else "normal"
libc = ctypes.CDLL(None, use_errno=True)
flag = ctypes.c_int(0)
if libc.prctl(36, 1, 0, 0, 0) != 0 or libc.prctl(37, ctypes.byref(flag), 0, 0, 0) != 0 or flag.value != 1:
    sys.exit(3)
parent = os.getpid()
child, child_fd, handed_off, descriptors, pipes = None, None, False, [], set()
def started(pid):
    with open("/proc/%d/stat" % pid) as handle:
        return int(handle.read().rsplit(")", 1)[1].split()[19])
def save(name, body):
    temporary = os.path.join(root, name + ".tmp")
    with open(temporary, "w") as handle:
        json.dump(body, handle)
    os.replace(temporary, os.path.join(root, name))
def guard_before_exec():
    # This producer has one thread; libc was loaded before its fork.
    if mode == "before_guard":
        save("nested-child-startup.json", {"pid":os.getpid(), "start":started(os.getpid()), "parent":parent})
        limit = time.monotonic() + 3.0
        while os.getppid() == parent and time.monotonic() < limit:
            time.sleep(0.01)
    # Set first, then check original parent: death before either operation is closed.
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        os._exit(8)
    if mode != "before_guard":
        save("nested-child-startup.json", {"pid":os.getpid(), "start":started(os.getpid()), "parent":parent})
def read_confirmation(fd, expected):
    if not select.select([fd], [], [], 3.0)[0] or os.read(fd, 1) != expected:
        raise RuntimeError("child did not confirm its startup guard state")
try:
    release_r, release_w = os.pipe()
    pipes.update((release_r, release_w))
    confirm_r, confirm_w = os.pipe()
    pipes.update((confirm_r, confirm_w))
    child = subprocess.Popen([sys.executable, "-c", CHILD_SOURCE,
                              str(release_r), str(confirm_w), str(parent)],
                             pass_fds=(release_r, confirm_w), preexec_fn=guard_before_exec,
                             start_new_session=True)
    for descriptor in (release_r, confirm_w):
        pipes.remove(descriptor)
        os.close(descriptor)
    for pid in (parent, child.pid):
        descriptors.append(os.pidfd_open(pid))
    child_fd = descriptors[1]
    read_confirmation(confirm_r, b"G")
    body = {"token":sys.argv[3], "job_root":root, "mode":mode,
            "nested":{"pid":parent, "start":started(parent)},
            "child":{"pid":child.pid, "start":started(child.pid)},
            "control_fds":{"release":release_r, "confirm":confirm_w}}
    save("nested-started.json", body)
    if mode == "before_handoff":
        save("nested.before-handoff.json", body)
        limit = time.monotonic() + 3.0
        while not os.path.exists(os.path.join(root, "allow-handoff")) and time.monotonic() < limit:
            time.sleep(0.01)
        if not os.path.exists(os.path.join(root, "allow-handoff")):
            raise RuntimeError("controlled pre-handoff pause expired")
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.settimeout(3.0)
        connection.connect(sys.argv[2])
        connection.sendmsg([json.dumps(body).encode()],
                           [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", descriptors))])
        save("nested.after-sendmsg.json", body)
        if connection.recv(32) != b"owned":
            raise RuntimeError("outer fixture ownership was not acknowledged")
    # Only this ACK can clear the guard. The receiver already holds both instances.
    os.write(release_w, b"R")
    read_confirmation(confirm_r, b"U")
    handed_off = True
    save("nested-child.json", body["child"])
    time.sleep(120)
finally:
    for descriptor in pipes:
        os.close(descriptor)
    if not handed_off and child is not None:
        if child_fd is not None:
            try:
                signal.pidfd_send_signal(child_fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait(timeout=3.0)
    for descriptor in descriptors:
        os.close(descriptor)
""".replace("__CHILD_SOURCE__", repr(NESTED_CHILD_SOURCE))
