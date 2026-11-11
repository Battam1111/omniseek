"""omo_proc: the ONE place that asks the kernel about processes (macOS libproc and os.kill, /proc on Linux);
it never runs ps or lsof.

What it reads: whether a process is alive (a zombie counts as gone), its start time, its name, its parent and
ancestor chain, its children and process group (from the process table), the list of process ids, the files a
process has open or mapped, its current directory, its state (the first letter of ps STAT), its memory (rusage:
footprint and resident bytes) and the CPU seconds it used (reaped children included). Linux has no footprint
or compressed count (memory gives two None, rusage only the resident size). Other platforms read nothing
(ENABLED is False).

What it does not read: any process's environment, and another process's command line, except that
owner_kind asks whether an absolute path in it is one of ours and gets back a count, never the arguments.

Three outcomes: alive, gone, unknown. A process the kernel will not let us read (a sandbox, another user's
process) is unknown, never gone.

Tests: inside ``with fake({pid: {...}})`` every function reads only that fake data; ENABLED=False fakes
"the kernel gives nothing".
"""
import contextlib
import ctypes
import errno
import os
import sys
import time

ALIVE = "在"
GONE = "不在"
UNKNOWN = "查不出"

LINUX = sys.platform.startswith("linux")   # 测试设成 True、PROC_ROOT 指假的 /proc，在 macOS 上核 Linux 的读法
ENABLED = sys.platform == "darwin" or LINUX   # 测试关掉它来造「内核也不给读」
PROC_ROOT = "/proc"
PID_MAX = 99999                      # macOS 的进程号上限（kern.maxproc 之外另有 PID_MAX 99999）
SZOMB = 5

_FAKE = None
_LIBS = []

# struct proc_bsdinfo（136 字节）与 struct proc_bsdshortinfo（64 字节）的偏移，在 macOS 26.5 上逐项核过
_TBSD, _TBSD_SZ = 3, 136
_SHORT, _SHORT_SZ = 13, 64
_LISTFDS, _VNODEPATH, _FD_VNODEPATH = 1, 9, 2
_REGIONPATH2, _REGION_SZ = 22, 1272     # PROC_PIDREGIONPATHINFO2：只走有文件的内存区；struct proc_regionwithpathinfo
_FD_TYPE_VNODE = 1
_COALITION, _COALITION_SZ = 20, 40      # PROC_PIDCOALITIONINFO：struct proc_pidcoalitioninfo（两个联盟号加三个保留，各 8 字节），10-16 在 macOS 26.5 上实测


def _libs():
    if not _LIBS:
        _LIBS.append((ctypes.CDLL("/usr/lib/libc.dylib", use_errno=True),
                      ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)))
    return _LIBS[0]


def _i32(b, off):
    return int.from_bytes(b[off:off + 4], "little", signed=True)


def _u32(b, off):
    return int.from_bytes(b[off:off + 4], "little")


def _cstr(b, off, n, errors="replace"):
    return b[off:off + n].split(b"\0", 1)[0].decode("utf-8", errors)


def _valid(pid):
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


# ---------- Linux：读 /proc ----------

_LX = {}   # {PROC_ROOT: (开机时刻 btime, 每秒时钟滴答数)}，各读一次


def _lx_path(pid, *parts):
    return os.path.join(PROC_ROOT, str(pid), *parts)


def _lx_read(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


def _lx_clock():
    """(开机时刻 btime, 每秒时钟滴答数)：/proc/stat 的 btime 与 sysconf(SC_CLK_TCK)。读不到 btime 抛 OSError。"""
    if PROC_ROOT not in _LX:
        btime = None
        for line in _lx_read(os.path.join(PROC_ROOT, "stat")).splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        if btime is None:
            raise OSError(errno.EIO, "%s/stat 里没有 btime" % PROC_ROOT)
        try:
            hz = os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError, AttributeError):
            hz = 100
        _LX[PROC_ROOT] = (btime, hz or 100)
    return _LX[PROC_ROOT]


def _lx_stat(pid):
    """/proc/<pid>/stat：{state, ppid, pgid, ticks（第 22 项，开机后第几个滴答起的，原文）, comm}。
    名字在第一个 ( 与最后一个 ) 之间（名字里可以有空格与右括号）。读不到抛 OSError，看不懂抛 ValueError。"""
    s = _lx_read(_lx_path(pid, "stat"))
    lp, rp = s.find("("), s.rfind(")")
    if lp < 0 or rp < lp:
        raise ValueError("看不懂 %s：%r" % (_lx_path(pid, "stat"), s[:80]))
    rest = s[rp + 2:].split()
    if len(rest) < 20:
        raise ValueError("%s 不到 22 项" % _lx_path(pid, "stat"))
    return {"state": rest[0], "ppid": int(rest[1]), "pgid": int(rest[2]), "ticks": rest[19],
            "comm": s[lp + 1:rp]}


def _lx_status(pid, key):
    """/proc/<pid>/status 里 key 那一行冒号后的各项；读不到或没有给 None。"""
    try:
        for line in _lx_read(_lx_path(pid, "status")).splitlines():
            if line.startswith(key + ":"):
                return line.split(":", 1)[1].split()
    except (OSError, ValueError):
        pass
    return None


def _lx_info(pid):
    """(信息, errno)，同 _raw_info。/proc/<pid> 不在而进程号还在（hidepid 挂的 /proc 藏起别的用户的进程）算读不到，不算不在。"""
    try:
        st = _lx_stat(pid)
        btime, hz = _lx_clock()
    except FileNotFoundError:
        return None, (errno.EPERM if exists(pid) else errno.ESRCH)
    except PermissionError:
        return None, errno.EPERM
    except (OSError, ValueError, IndexError):
        return None, errno.EIO
    uid = _lx_status(pid, "Uid")
    start = btime + int(st["ticks"]) / float(hz)
    return {"pid": pid, "ppid": st["ppid"], "pgid": st["pgid"],
            "uid": int(uid[1]) if uid and len(uid) > 1 and uid[1].isdigit() else None,
            "epoch": float(int(start)), "t": start, "ticks": st["ticks"], "zombie": st["state"] in ("Z", "X"),
            "comm": st["comm"][:16], "name": st["comm"]}, 0


def _lx_link(pid, what):
    """/proc/<pid>/<what> 这个链接指的路径（去掉删掉的文件后面的「 (deleted)」）；读不到给空串。"""
    try:
        p = os.readlink(_lx_path(pid, what))
    except OSError:
        return ""
    return p[:-10] if p.endswith(" (deleted)") else p


# Linux 的状态字母换成 state_letter 的那几个：D（不可中断地等）是 U，t（被跟踪停住）是 T，X 是 Z，别的睡着的是 S
_LX_STATE = {"R": "R", "S": "S", "D": "U", "T": "T", "t": "T", "Z": "Z", "X": "Z", "I": "I"}


def _lx_open_files(pid, mapped):
    """当前目录、文件描述符里的文件、可执行文件、映射进内存的文件（/proc/<pid>/maps）；读不到当前目录或 fd 目录给 None。"""
    here = _lx_link(pid, "cwd")
    try:
        fds = os.listdir(_lx_path(pid, "fd"))
    except OSError:
        return None
    if not here:
        return None
    paths = [here]
    for n in sorted(fds, key=lambda x: int(x) if x.isdigit() else -1):
        p = _lx_link(pid, os.path.join("fd", n))
        if p.startswith("/"):
            paths.append(p)
    e = _lx_link(pid, "exe")
    if e:
        paths.append(e)
    if mapped:
        have = set(paths)
        try:
            maps = _lx_read(_lx_path(pid, "maps"))
        except OSError:
            maps = ""
        for line in maps.splitlines():
            f = line.split(None, 5)
            if len(f) == 6 and f[5].startswith("/"):
                p = f[5][:-10] if f[5].endswith(" (deleted)") else f[5]
                if p not in have:
                    have.add(p)
                    paths.append(p)
    return paths


@contextlib.contextmanager
def fake(procs, listable=True):
    """测试用：期间只认这份假进程表。procs = {pid: {"ppid", "pgid", "uid", "epoch", "zombie", "comm", "name",
    "exe", "files": [路径], "footprint_mb", "compressed_mb", "resident_mb", "hidden": 读不到这个进程,
    "fd_hidden": 读得到信息、读不到打开的文件, "info_hidden": 读不到详细信息、读得到简短信息（沙盒外别的用户的进程就是这样）,
    "args": [参数], "args_hidden": 读不到参数, "cwd": 当前目录, "state": 进程状态字母, "cpu_s": 耗的 CPU 秒数}}，缺的字段有缺省。
    listable=False 造「列不出全机进程号」（沙盒里 proc_listallpids 被拒）。"""
    global _FAKE
    old = _FAKE
    _FAKE = {"procs": {int(k): dict(v) for k, v in procs.items()}, "listable": listable}
    try:
        yield _FAKE
    finally:
        _FAKE = old


def _fake_row(pid):
    p = _FAKE["procs"][pid]
    return {"pid": pid, "ppid": p.get("ppid", 1), "pgid": p.get("pgid", pid), "uid": p.get("uid", os.getuid()),
            "epoch": p.get("epoch"), "t": p.get("t", p.get("epoch")), "zombie": bool(p.get("zombie")),
            "comm": p.get("comm", ""),
            "name": p.get("name") or p.get("comm", "")}


# ---------- 有没有这个进程号 ----------

def exists(pid):
    """只用 os.kill(pid, 0) 判有没有这个进程号：True 有（PermissionError 也算有，僵尸也算有），False 没有。"""
    if not _valid(pid):
        return False
    if _FAKE is not None:
        return pid in _FAKE["procs"]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


# ---------- 一个进程的信息 ----------

def _raw_info(pid):
    """(信息, errno)。信息是 dict 或 None；errno 是读不到的原因（0 表示读到了）。"""
    if _FAKE is not None:
        if pid not in _FAKE["procs"]:
            return None, errno.ESRCH
        p = _FAKE["procs"][pid]
        if p.get("zombie"):
            return None, errno.ESRCH
        if p.get("hidden") or p.get("info_hidden"):
            return None, errno.EPERM
        return _fake_row(pid), 0
    if not ENABLED:
        return None, errno.EPERM
    if LINUX:
        return _lx_info(pid)
    try:
        _, lp = _libs()
        buf = ctypes.create_string_buffer(_TBSD_SZ)
        ctypes.set_errno(0)
        n = lp.proc_pidinfo(pid, _TBSD, ctypes.c_uint64(0), buf, _TBSD_SZ)
        err = ctypes.get_errno()
    except Exception:
        return None, errno.EPERM
    if n != _TBSD_SZ:
        return None, err or errno.EPERM
    raw = buf.raw
    if _u32(raw, 12) != pid:
        return None, errno.ESRCH
    start = _u32(raw, 120) + int.from_bytes(raw[128:136], "little") / 1e6
    return {"pid": pid, "ppid": _u32(raw, 16), "pgid": _u32(raw, 100), "uid": _u32(raw, 20),
            "epoch": float(int(start)), "t": start, "zombie": _u32(raw, 4) == SZOMB,
            "comm": _cstr(raw, 48, 16), "name": _cstr(raw, 64, 32) or _cstr(raw, 48, 16)}, 0


def short_info(pid):
    """PROC_PIDT_SHORTBSDINFO：不带开始时刻的简短信息 {pid, ppid, pgid, uid, comm, zombie}；读不到返回 None。
    沙盒外它对别的用户的进程也读得到（内核不查同一用户），用来认「这是别的用户的进程」；沙盒里与 TBSDINFO 一样被拒。"""
    if _FAKE is not None:
        p = _FAKE["procs"].get(pid)
        if p is None or p.get("hidden") or p.get("zombie"):
            return None
        r = _fake_row(pid)
        return {k: r[k] for k in ("pid", "ppid", "pgid", "uid", "comm", "zombie")}
    if not ENABLED or not _valid(pid):
        return None
    if LINUX:
        r = _lx_info(pid)[0]
        return None if r is None else {k: r[k] for k in ("pid", "ppid", "pgid", "uid", "comm", "zombie")}
    try:
        _, lp = _libs()
        buf = ctypes.create_string_buffer(_SHORT_SZ)
        if lp.proc_pidinfo(pid, _SHORT, ctypes.c_uint64(0), buf, _SHORT_SZ) != _SHORT_SZ:
            return None
    except Exception:
        return None
    raw = buf.raw
    if _u32(raw, 0) != pid:
        return None
    return {"pid": pid, "ppid": _u32(raw, 4), "pgid": _u32(raw, 8), "uid": _u32(raw, 36),
            "comm": _cstr(raw, 16, 16), "zombie": _u32(raw, 12) == SZOMB}


def look(pid):
    """(状态, 信息, 原因)。状态 ALIVE / GONE / UNKNOWN；信息只在 ALIVE 时给：
    {pid, ppid, pgid, uid, epoch（开始时刻，取整到秒）, t（开始时刻，带微秒）, start（同 ps lstart 的写法）,
    zombie, comm, name}。
    僵尸（已退出、还没被收走）算不在。"""
    if not _valid(pid):
        return GONE, None, "进程号不对：%r" % (pid,)
    if not exists(pid):
        return GONE, None, "进程 %d 不在" % pid
    info, err = _raw_info(pid)
    if info is None:
        if err == errno.ESRCH:
            # 僵尸：PROC_PIDTBSDINFO 报 ESRCH 而 os.kill(pid, 0) 仍成功（实测）
            if exists(pid):
                return GONE, None, "进程 %d 已经退出（僵尸，还没被收走）" % pid
            return GONE, None, "进程 %d 不在" % pid
        return UNKNOWN, None, "进程号 %d 还在，但内核不给读它的信息（errno %d），认不出是不是它" % (pid, err)
    if info["zombie"]:
        return GONE, None, "进程 %d 已经退出（僵尸，还没被收走）" % pid
    info["start"] = start_text(info["epoch"])
    return ALIVE, info, None


def info(pid):
    """活着且读得到时给信息 dict（同 look），否则 None。"""
    st, inf, _ = look(pid)
    return inf if st == ALIVE else None


def state(pid):
    return look(pid)[0]


def exe(pid):
    """可执行文件路径（proc_pidpath）；读不到给空串。"""
    if _FAKE is not None:
        p = _FAKE["procs"].get(pid) or {}
        return "" if p.get("hidden") else p.get("exe", "")
    if not ENABLED or not _valid(pid):
        return ""
    if LINUX:
        return _lx_link(pid, "exe")
    try:
        _, lp = _libs()
        buf = ctypes.create_string_buffer(4096)
        n = lp.proc_pidpath(pid, buf, 4096)
    except Exception:
        return ""
    return buf.raw[:n].decode("utf-8", "replace") if n > 0 else ""


def coalition(pid):
    """进程所在的 launchd 资源联盟号（proc_pidinfo 的 PROC_PIDCOALITIONINFO，资源那一类）：launchd 给每个单元一个联盟，
    单元起的进程连同脱了亲、挂到 launchd 下的都还在里面（10-16 真机实测：CDP 单元的 Chrome 主进程、它的子进程、脱了亲的
    崩溃上报进程同一个号；别的单元、Apple 的进程、ssh 会话各是各的）。读不到回 None。"""
    if _FAKE is not None:
        return (_FAKE["procs"].get(pid) or {}).get("coalition")
    if not ENABLED or LINUX or not _valid(pid):
        return None
    try:
        _, lp = _libs()
        buf = ctypes.create_string_buffer(_COALITION_SZ)
        n = lp.proc_pidinfo(pid, _COALITION, ctypes.c_uint64(0), buf, _COALITION_SZ)
    except Exception:
        return None
    co = int.from_bytes(buf.raw[0:8], "little") if n == _COALITION_SZ else 0
    return co or None


APPLE_PREFIXES = ("/System/", "/usr/libexec/", "/usr/sbin/")   # SIP 护着的；/bin、/usr/bin 不算（shell 与 /usr/bin/python3 是人和我们的服务在用的）
NOT_APPLE_PREFIXES = ("/System/Volumes/Data/",)                  # 数据卷经 /System 也走得到，那下面是人和我们装的


def real_exe(pid):
    """可执行文件解开链接以后的路径；读不到给空串。"""
    p = exe(pid)
    return os.path.realpath(p) if p else ""


def apple_path(p):
    """解开链接以后的可执行文件路径在 SIP 护着的 /System、/usr/libexec、/usr/sbin 下（数据卷除外）。"""
    return bool(p) and p.startswith(APPLE_PREFIXES) and not p.startswith(NOT_APPLE_PREFIXES)


def is_apple(pid):
    """Apple 自己的进程（只是在看、在扫，不写我们的数据）：可执行文件解开链接以后见 apple_path。读不到可执行文件的不算。"""
    return apple_path(real_exe(pid))


def argv_hits(pid, mine):
    """参数里有几处路径 mine(绝对路径) 认是我们的：每个参数本身与「-键=值」的值，是绝对路径的才问。只回个数，参数本身
    不回、不记（文件头「不查的」那一段的例外，只给 owner_kind 用）。读不到参数回 None。"""
    args, _why = _argv(pid)
    if args is None:
        return None
    n = 0
    for a in args[1:]:
        v = a.split("=", 1)[1] if a.startswith("-") and "=" in a else a
        n += bool(v.startswith("/") and mine(os.path.normpath(v)))
    return n


def owner_kind(pid, mine, unit_pids=(), unit_coalitions=()):
    """这个进程是谁的，分三类（认进程是谁的只经这一处）：
      ours     能证明是我们的：在我们 launchd 单元里（unit_pids 是记下的进程号；unit_coalitions 是我们单元的资源联盟号，
               脱了亲的也认得出，见 coalition），或可执行文件解开链接以后、或参数里的路径，mine(绝对路径) 认是我们装的
               （例如 CDP 起的 Chrome，--user-data-dir 在我们的目录下）；
      apple    不是上面那样的，可执行文件解开链接以后在 SIP 护着的 /System、/usr/libexec、/usr/sbin 下（apple_path）；
      unknown  其余，连可执行文件都读不到的也算：拦下交人，绝不 TERM、KILL。
    先认我们的（进程树、可执行文件、参数），再认 Apple 的：我们拿 Apple 的程序起的进程也是我们的（例如
    我们用 /usr/sbin/sshd 起的测试服务）。回 (类, 依据一句话)。"""
    if pid in set(unit_pids):
        return "ours", "在我们的 launchd 单元的进程树里"
    co = coalition(pid) if unit_coalitions else None
    if co is not None and co in set(unit_coalitions):
        return "ours", "在我们 launchd 单元的资源联盟里（%d）" % co
    p = real_exe(pid)
    if p and mine(p):
        return "ours", "可执行文件 %s 在我们装的目录里" % p
    n = argv_hits(pid, mine)
    if n:
        return "ours", "参数里有 %d 处路径在我们装的目录里" % n
    if apple_path(p):
        return "apple", "Apple 的（%s）" % p
    return "unknown", "认不出（可执行文件 %s%s）" % (p or "读不到", "，参数读不到" if n is None else "")


def name_of(pid):
    """展示用的名字：可执行文件的 basename，读不到时用 PROC_PIDTBSDINFO 的名字；都读不到给空串。"""
    e = exe(pid)
    if e:
        return os.path.basename(e)
    inf = info(pid)
    return inf["name"] if inf else ""


def _vnode_cwd(lp, pid):
    """(读到没有, 当前目录)：PROC_PIDVNODEPATHINFO，struct proc_vnodepathinfo 里当前目录的路径在 152。"""
    vb = ctypes.create_string_buffer(2352)
    if lp.proc_pidinfo(pid, _VNODEPATH, ctypes.c_uint64(0), vb, 2352) != 2352:
        return False, ""
    return True, _cstr(vb.raw, 152, 1024, "surrogateescape")


def cwd(pid):
    """进程的当前目录；读不到（或内核给的是空的）给 None。假数据用 "cwd" 字段。"""
    if _FAKE is not None:
        p = _FAKE["procs"].get(pid) or {}
        return None if p.get("hidden") or p.get("fd_hidden") else (p.get("cwd") or None)
    if not ENABLED or not _valid(pid):
        return None
    if LINUX:
        return _lx_link(pid, "cwd") or None
    try:
        ok, path = _vnode_cwd(_libs()[1], pid)
    except Exception:
        return None
    return (path or None) if ok else None


# ---------- 进程状态（同 ps STAT 的头一个字） ----------

_LISTTHREADS, _THREADINFO, _THREADINFO_SZ = 6, 5, 112   # PROC_PIDLISTTHREADS；PROC_PIDTHREADINFO，pth_run_state 在 24


def state_letter(status, run_states):
    """进程状态的一个字母，同 ps 的 STAT 头一个字：status 是 proc_bsdinfo 的 pbi_status（1 SIDL、2 SRUN、3 SSLEEP、
    4 SSTOP、5 SZOMB）；macOS 上睡着的进程 pbi_status 也是 2，要看各线程的 pth_run_state（1 在跑、2 停、3 等、
    4 不可中断地等、5 停）：有一个 4 就是 U，有一个 1 就是 R，否则 S。"""
    if status == SZOMB:
        return "Z"
    if status == 4:
        return "T"
    if status == 1:
        return "I"
    if 4 in run_states:
        return "U"
    if 1 in run_states:
        return "R"
    return "S"


def _thread_run_states(lp, pid):
    ids = ctypes.create_string_buffer(8 * 1024)
    n = lp.proc_pidinfo(pid, _LISTTHREADS, ctypes.c_uint64(0), ids, len(ids))
    out = []
    for i in range(max(n, 0) // 8):
        tid = int.from_bytes(ids.raw[i * 8:i * 8 + 8], "little")
        tb = ctypes.create_string_buffer(_THREADINFO_SZ)
        if lp.proc_pidinfo(pid, _THREADINFO, ctypes.c_uint64(tid), tb, _THREADINFO_SZ) == _THREADINFO_SZ:
            out.append(_i32(tb.raw, 24))
    return out


def run_state(pid):
    """进程状态的一个字母（I、R、S、T、U、Z，见 state_letter）；读不到给 None。假数据用 "state" 字段，缺省 S。"""
    if _FAKE is not None:
        p = _FAKE["procs"].get(pid)
        if p is None or p.get("hidden") or p.get("info_hidden"):
            return None
        return p.get("state") or ("Z" if p.get("zombie") else "S")
    if not ENABLED or not _valid(pid):
        return None
    if LINUX:
        try:
            return _LX_STATE.get(_lx_stat(pid)["state"], "S")
        except (OSError, ValueError, IndexError):
            return None
    try:
        _, lp = _libs()
        b = ctypes.create_string_buffer(_TBSD_SZ)
        if lp.proc_pidinfo(pid, _TBSD, ctypes.c_uint64(0), b, _TBSD_SZ) != _TBSD_SZ or _u32(b.raw, 12) != pid:
            return None
        return state_letter(_u32(b.raw, 4), _thread_run_states(lp, pid))
    except Exception:
        return None


# ---------- 内存占用 ----------

_RUSAGE_V4, _RU_FOOTPRINT, _RU_SZ = 4, 72, 296          # proc_pid_rusage(RUSAGE_INFO_V4)：ri_phys_footprint 在 72
_TASK_VM_INFO, _VM_COMPRESSED, _VM_COUNT = 22, 120, 128  # task_info(TASK_VM_INFO)：compressed 在 120；count 以 4 字节计


def memory(pid):
    """(物理占用 MB, 压缩量 MB)，各保留一位小数，读不到的给 None。物理占用是活动监视器与 top 的「内存」列
    （proc_pid_rusage 的 ri_phys_footprint），压缩量是 top 的 CMPRS 列（task_name_for_pid 加 TASK_VM_INFO 的
    compressed）。只读这两个计数，不读进程的内存内容。别的用户的进程（root 的系统服务）与沙盒外的进程读不到。
    Linux 上没有这两个计数，给 (None, None)；要常驻量用 rusage。"""
    if _FAKE is not None:
        p = _FAKE["procs"].get(pid) or {}
        if p.get("hidden") or pid not in _FAKE["procs"]:
            return None, None
        return p.get("footprint_mb"), p.get("compressed_mb")
    if not ENABLED or not _valid(pid) or LINUX:
        return None, None
    try:
        libc, lp = _libs()
    except Exception:
        return None, None
    fp = comp = None
    try:
        buf = ctypes.create_string_buffer(512)
        if lp.proc_pid_rusage(pid, _RUSAGE_V4, buf) == 0:
            fp = round(int.from_bytes(buf.raw[_RU_FOOTPRINT:_RU_FOOTPRINT + 8], "little") / 1048576.0, 1)
    except Exception:
        fp = None
    try:
        me = ctypes.c_uint32.in_dll(libc, "mach_task_self_").value
        port = ctypes.c_uint32(0)
        if libc.task_name_for_pid(me, pid, ctypes.byref(port)) == 0:
            try:
                buf = ctypes.create_string_buffer(_VM_COUNT * 4)
                cnt = ctypes.c_uint32(_VM_COUNT)
                if libc.task_info(port, _TASK_VM_INFO, buf, ctypes.byref(cnt)) == 0 \
                        and cnt.value * 4 >= _VM_COMPRESSED + 8:
                    comp = round(int.from_bytes(buf.raw[_VM_COMPRESSED:_VM_COMPRESSED + 8], "little")
                                 / 1048576.0, 1)
            finally:
                libc.mach_port_deallocate(me, port)
    except Exception:
        comp = None
    return fp, comp


_RU_RESIDENT = 64   # struct rusage_info_v2 以上：ri_resident_size 在 64，ri_phys_footprint 在 72（_RU_FOOTPRINT）


def rusage(pid=None):
    """{"footprint": 物理占用字节数或 None, "resident": 常驻量字节数或 None}，读不到给 None（memguard 用它）。
    pid 缺省本进程。macOS：proc_pid_rusage 的 ri_phys_footprint 与 ri_resident_size（不取整，memguard 要逐 MB 比门限）；
    Linux：/proc/<pid>/status 的 VmRSS，物理占用给 None。假数据用 "footprint_mb"、"resident_mb"。"""
    pid = os.getpid() if pid is None else pid
    if _FAKE is not None:
        p = _FAKE["procs"].get(pid)
        if p is None or p.get("hidden"):
            return None
        return {k: (None if p.get(k + "_mb") is None else int(p[k + "_mb"] * 1048576))
                for k in ("footprint", "resident")}
    if not ENABLED or not _valid(pid):
        return None
    if LINUX:
        v = _lx_status(pid, "VmRSS")
        return {"footprint": None, "resident": int(v[0]) * 1024} if v and v[0].isdigit() else None
    try:
        _, lp = _libs()
        buf = ctypes.create_string_buffer(512)
        if lp.proc_pid_rusage(pid, _RUSAGE_V4, buf) != 0:
            return None
    except Exception:
        return None
    raw = buf.raw
    return {"footprint": int.from_bytes(raw[_RU_FOOTPRINT:_RU_FOOTPRINT + 8], "little"),
            "resident": int.from_bytes(raw[_RU_RESIDENT:_RU_RESIDENT + 8], "little")}


PRESSURE_LEVELS = (1, 2, 4)   # kern.memorystatus_vm_pressure_level 的三档：1 正常、2 警告、4 危急
PRESSURE_CRITICAL = 4


def pressure():
    """整机内存这一刻怎么样：{"level": 内核判的压力档或 None, "free_pct": 空闲百分比或 None}（sysctl，不起子进程）。
    判「够不够、能不能再起活」只看 level（kern.memorystatus_vm_pressure_level，1 正常、2 警告、4 危急；到
    PRESSURE_CRITICAL 才挡）。free_pct 是 kern.memorystatus_level（memory_pressure -Q 的空闲百分比），数的是没锁定、
    没压缩的那部分，冷数据一压缩它就掉、机器并不卡，只记下给人看，不拿来判。转写的内存准入读这里，
    读法只此一份。Linux 与读不到时两项都是 None（不判）。"""
    out = {"level": None, "free_pct": None}
    if not ENABLED or LINUX:
        return out
    try:
        libc, _ = _libs()
    except Exception:
        return out
    for key, name in (("level", b"kern.memorystatus_vm_pressure_level"), ("free_pct", b"kern.memorystatus_level")):
        v = ctypes.c_int(0)
        sz = ctypes.c_size_t(ctypes.sizeof(v))
        try:
            if libc.sysctlbyname(name, ctypes.byref(v), ctypes.byref(sz), None, 0) == 0:
                out[key] = v.value
        except Exception:
            pass
    return out


_RU_USER, _RU_SYSTEM = 16, 24   # struct rusage_info_v0 起：ri_user_time 在 16，ri_system_time 在 24（单位是 mach 时钟滴答）
_RU_CHILD_USER, _RU_CHILD_SYSTEM = 96, 104   # rusage_info_v1 起：ri_child_user_time、ri_child_system_time（已收走的子进程）
_TIMEBASE = []


def _ns_per_tick():
    """mach_timebase_info 的 numer/denom：Apple 芯片上 ri_user_time 是 mach 时钟滴答（约 41.67 纳秒一个），不是纳秒。"""
    if not _TIMEBASE:
        libc, _ = _libs()
        tb = (ctypes.c_uint32 * 2)()
        libc.mach_timebase_info(tb)
        _TIMEBASE.append(float(tb[0]) / float(tb[1] or 1))
    return _TIMEBASE[0]


def cpu_s(pid):
    """进程耗的 CPU 秒数：自己的加上它已收走（wait 过）的子进程的，用户态加内核态；读不到给 None
    （判卡死看进程树耗没耗 CPU；算上收走的子进程，一闪而过、还没被认出来的子进程耗的也算得到）。
    macOS：proc_pid_rusage 的 ri_user_time、ri_system_time、ri_child_user_time、ri_child_system_time，按 mach_timebase_info
    换成秒；沙盒外的进程读不到。Linux：/proc/<pid>/stat 第 14 到 17 项（utime、stime、cutime、cstime）除以每秒时钟滴答数。
    假数据用 "cpu_s"。"""
    if _FAKE is not None:
        p = _FAKE["procs"].get(pid)
        if p is None or p.get("hidden") or p.get("zombie"):
            return None
        v = p.get("cpu_s")
        return None if v is None else float(v)
    if not ENABLED or not _valid(pid):
        return None
    if LINUX:
        try:
            s = _lx_read(_lx_path(pid, "stat"))
            rest = s[s.rfind(")") + 2:].split()
            return sum(int(x) for x in rest[11:15]) / float(_lx_clock()[1])
        except (OSError, ValueError, IndexError):
            return None
    try:
        _, lp = _libs()
        buf = ctypes.create_string_buffer(512)
        if lp.proc_pid_rusage(pid, _RUSAGE_V4, buf) != 0:
            return None
        raw = buf.raw
        ticks = sum(int.from_bytes(raw[o:o + 8], "little") for o in (_RU_USER, _RU_SYSTEM, _RU_CHILD_USER, _RU_CHILD_SYSTEM))
        return ticks * _ns_per_tick() / 1e9
    except Exception:
        return None


# ---------- 开始时刻 ----------

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def start_text(epoch):
    """开始时刻写成 ps lstart 那样（本地时间，到秒）：'Fri Oct 10 12:34:56 2026'。"""
    if epoch is None:
        return ""
    lt = time.localtime(epoch)
    return "%s %s %d %02d:%02d:%02d %d" % (_DAYS[lt.tm_wday], _MONS[lt.tm_mon - 1], lt.tm_mday,
                                           lt.tm_hour, lt.tm_min, lt.tm_sec, lt.tm_year)


def parse_start(v):
    """开始时刻（秒）：数字原样，ps lstart 写法的文本（日期前补不补空格都认）换算；认不出给 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = " ".join(str(v).split())
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    parts = s.split()
    if len(parts) != 5 or parts[0] not in _DAYS or parts[1] not in _MONS:
        return None
    try:
        h, m, sec = (int(x) for x in parts[3].split(":"))
        t = (int(parts[4]), _MONS.index(parts[1]) + 1, int(parts[2]), h, m, sec, 0, 0, -1)
        return float(time.mktime(t))
    except (ValueError, OverflowError):
        return None


def same_start(recorded, actual, slack=1):
    """记下的开始时刻与现在读到的是不是同一个（差不过 slack 秒）；两边都可以是秒或 lstart 文本。认不出算不同。"""
    a, b = parse_start(recorded), parse_start(actual)
    return a is not None and b is not None and abs(a - b) <= slack


def check(pid, start=None, name=None, slack=5):
    """进程 pid 还是不是记下的那个进程：(状态, 信息或 None, 原因)。
    只给进程号：在不在（僵尸算不在；进程号还在就算在，不必读得到信息）。
    给了 start（记下的开始时刻，秒或 lstart 文本）或 name（记下的名字）：还要读得到信息才算在，
    开始时刻差过 slack 秒或名字对不上算不在（进程号被别的进程重用），读不到算查不出。"""
    if not _valid(pid) or pid == 1:
        return GONE, None, "进程号不对：%r" % (pid,)
    st, inf, why = look(pid)
    if st == GONE:
        return st, None, why
    if start is None and not name:
        return ALIVE, inf, None
    if st == UNKNOWN:
        return st, None, why
    if start is not None:
        want = parse_start(start)
        if want is None:
            return UNKNOWN, inf, "记下的开始时刻认不出：%r" % (start,)
        if abs(inf["epoch"] - want) > slack:
            return GONE, inf, "进程号 %d 被别的进程重用（开始时刻对不上）" % pid
    if name and name not in (inf["comm"], inf["name"], os.path.basename(exe(pid) or "")):
        return GONE, inf, "进程号 %d 被别的进程重用（名字 %s 对不上 %s）" % (pid, inf["name"], name)
    return ALIVE, inf, None


# ---------- 列进程 ----------

def list_pids():
    """(进程号列表, 列全了没有)。先用 proc_listallpids；被拒（沙盒里）时逐个号 os.kill(pid, 0) 扫一遍
    （0.05 到 0.1 秒），这时列出的号齐，但别的进程大多读不到信息，所以算没列全。Linux 上列 /proc 下的数字目录。"""
    if _FAKE is not None:
        return sorted(_FAKE["procs"]), _FAKE["listable"]
    if LINUX:
        if not ENABLED:
            return [], False
        try:
            return sorted(int(n) for n in os.listdir(PROC_ROOT) if n.isdigit()), True
        except OSError:
            return [], False
    if ENABLED:
        try:
            _, lp = _libs()
            n = lp.proc_listallpids(None, 0)
            if n > 0:
                cap = n + 64
                arr = (ctypes.c_int * cap)()
                got = lp.proc_listallpids(arr, ctypes.sizeof(arr))
                if got > 0:
                    return sorted({arr[i] for i in range(min(got, cap)) if arr[i] > 0}), True
        except Exception:
            pass
    out = []
    for pid in range(1, PID_MAX + 1):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except OSError:
            pass
        out.append(pid)
    return out, False


class Table(dict):
    """{pid: 信息}，读得到的进程（含僵尸，僵尸的 ppid、pgid 可能是 None）；
    hidden 是进程号在、信息读不到的个数，complete 是进程号列全了没有。"""
    hidden = 0
    complete = True


def table():
    """全机进程表 {pid: {pid, ppid, pgid, uid, epoch, start, zombie, comm, name}}（Table）。
    读不到信息的进程不在表里，个数记在 .hidden。"""
    pids, complete = list_pids()
    t = Table()
    t.complete = complete
    for pid in pids:
        inf, err = _raw_info(pid)
        if inf is None:
            if err == errno.ESRCH and exists(pid):
                sh = short_info(pid) if _FAKE is None else None
                k = _kinfo(pid) if sh is None else None
                if _FAKE is not None:
                    sh = {"ppid": _FAKE["procs"][pid].get("ppid"), "pgid": _FAKE["procs"][pid].get("pgid")}
                t[pid] = {"pid": pid, "ppid": (sh or k or {}).get("ppid"), "pgid": (sh or k or {}).get("pgid"),
                          "uid": (sh or {}).get("uid"), "epoch": None, "start": "", "zombie": True,
                          "comm": "", "name": ""}
            elif err != errno.ESRCH:
                t.hidden += 1
            continue
        inf["start"] = start_text(inf["epoch"])
        t[pid] = inf
    return t


def _kinfo(pid):
    """sysctl KERN_PROC_PID 读 struct kinfo_proc（648 字节）：{ppid, pgid, zombie}；读不到 None。
    只在僵尸上用（PROC_PIDTBSDINFO 对僵尸报 ESRCH）；沙盒里它只读得到本进程，沙盒外读得到僵尸。"""
    if not ENABLED or _FAKE is not None or LINUX:
        return None
    try:
        libc, _ = _libs()
        buf = ctypes.create_string_buffer(648)
        size = ctypes.c_size_t(648)
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)   # CTL_KERN, KERN_PROC, KERN_PROC_PID
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value != 648:
            return None
    except Exception:
        return None
    raw = buf.raw
    if _i32(raw, 40) != pid:
        return None
    return {"ppid": _i32(raw, 560), "pgid": _i32(raw, 564), "zombie": raw[36] == SZOMB}


def descendants(tab, roots):
    """进程表里 roots 的全部后代（不含 roots 本身）。"""
    kids = {}
    for pid, r in tab.items():
        if r.get("ppid") is not None:
            kids.setdefault(r["ppid"], []).append(pid)
    seen, stack = set(), list(roots)
    while stack:
        for c in kids.get(stack.pop(), []):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen - set(roots)


def children(tab, pid):
    return sorted(p for p, r in tab.items() if r.get("ppid") == pid)


def group(tab, pgid):
    """进程组 pgid 里读得到的进程号（含僵尸）。"""
    return sorted(p for p, r in tab.items() if r.get("pgid") == pgid)


def group_state(pgid):
    """进程组还有没有活进程：ALIVE / GONE / UNKNOWN。
    killpg(pgid, 0) 报 ESRCH 是没有，成功是有；EPERM（macOS 上只剩僵尸的组也报 EPERM，REL3 实测）时看进程表：
    组里有读得到的活进程算在，读得到的全是僵尸算不在，一个也读不到算查不出。"""
    if not _valid(pgid):
        return GONE
    if _FAKE is None:
        try:
            os.killpg(pgid, 0)
            return ALIVE
        except ProcessLookupError:
            return GONE
        except OSError:
            pass
    tab = table()
    mem = [tab[p] for p in group(tab, pgid)]
    if any(not r["zombie"] for r in mem):
        return ALIVE
    if mem or _FAKE is not None:
        return GONE
    # 沙盒里僵尸的进程组号读不到；组长（进程号就是组号）是僵尸而 killpg 报 EPERM，组里没有能收信号的活进程，算不在
    if pgid in tab and tab[pgid]["zombie"]:
        return GONE
    return UNKNOWN


# ---------- 祖先链 ----------

def ancestors(pid=None, limit=64):
    """从 pid（缺省本进程）的父进程往上的祖先链：(链, 断在哪)。链是 [信息 + {"exe"}]，近的在前；
    读到 1 号（launchd）或进程号 0 停下，断在哪是 None；某一级读不到（沙盒外的进程）就停在那里，
    断在哪是那个读不到的进程号。"""
    pid = os.getpid() if pid is None else pid
    me = info(pid)
    if me is None:
        return [], pid
    chain, seen = [], {pid}
    nxt = me["ppid"]
    while len(chain) < limit:
        if nxt in (0, 1) or nxt in seen:
            return chain, None
        seen.add(nxt)
        inf = info(nxt)
        if inf is None:
            return chain, nxt
        inf["exe"] = exe(nxt)
        chain.append(inf)
        nxt = inf["ppid"]
    return chain, None


_KERN_PROCARGS2 = (1, 49)   # CTL_KERN, KERN_PROCARGS2


def _self_line():
    """本进程与它的祖先：{进程号: 开始时刻（带微秒）}，读到 1 号或读不到为止。"""
    out, pid = {}, os.getpid()
    while pid > 1 and pid not in out and len(out) < 128:
        inf = info(pid)
        if inf is None:
            break
        out[pid] = inf["t"]
        pid = inf["ppid"]
    return out


def _argv(pid):
    """(参数列表, 原因)：sysctl KERN_PROCARGS2 只解出 argc 与 argv。内核把参数与环境变量放在同一块里一起给，
    这里解完 argc 个参数就停，后面的环境变量一个字节都不解码，读完把整块清零。读不到时列表是 None。
    只给 ancestor_chain 用，它先确认 pid 在本进程的祖先链上。"""
    if _FAKE is not None:
        p = _FAKE["procs"].get(pid) or {}
        if p.get("hidden") or p.get("args_hidden"):
            return None, "内核不给读进程 %d 的参数" % pid
        return list(p.get("args") or []), None
    if not ENABLED:
        return None, "内核不给读"
    if LINUX:
        try:
            raw = _lx_read(_lx_path(pid, "cmdline"), "rb")
        except OSError as e:
            return None, "读不到进程 %d 的参数（%s）" % (pid, e.strerror or e)
        parts = raw.split(b"\0")
        if parts and parts[-1] == b"":
            parts.pop()
        return [a.decode("utf-8", "replace") for a in parts], None
    try:
        libc, _ = _libs()
        mib = (ctypes.c_int * 3)(_KERN_PROCARGS2[0], _KERN_PROCARGS2[1], pid)
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0 or not size.value:
            return None, "读不到进程 %d 的参数（errno %d）" % (pid, ctypes.get_errno())
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
            return None, "读不到进程 %d 的参数（errno %d）" % (pid, ctypes.get_errno())
    except Exception as e:
        return None, "读进程 %d 的参数出错：%r" % (pid, e)
    try:
        n = min(size.value, len(buf))
        argc = _i32(buf.raw[:4], 0)
        args, off = [], 4
        end = buf.raw.find(b"\0", off, n)            # 可执行文件路径
        off = n if end < 0 else end
        while off < n and buf.raw[off:off + 1] == b"\0":
            off += 1
        while len(args) < max(argc, 0) and off < n:
            end = buf.raw.find(b"\0", off, n)
            if end < 0:
                end = n
            args.append(buf.raw[off:end].decode("utf-8", "replace"))
            off = end + 1
        return args, None
    finally:
        ctypes.memset(buf, 0, len(buf))


def ancestor_chain(pid=None, args=True, limit=64):
    """从 pid（缺省本进程的父进程）起往上逐级：(链, 断在哪)。链里每级
    {pid, ppid, t（开始时刻，带微秒，防进程号复用）, epoch, start, name（可执行文件名，读不到用内核记的名字）,
    exe（可执行文件路径）, args（参数列表或 None）, args_why（args 为 None 时的原因）}，起点在前。
    读到 1 号或进程号 0 停下，断在哪是 None；某一级读不到就停在那里，断在哪是那个进程号。
    参数只读本进程自己与它祖先链上的进程（进程号与开始时刻都对得上），别的一律 None、不去读；不读任何环境变量。
    args=False 时一个参数都不读。"""
    if pid is None:
        me = info(os.getpid())
        pid = me["ppid"] if me else os.getppid()
    mine = _self_line() if args else {}
    chain, seen, nxt = [], set(), pid
    while len(chain) < limit:
        if nxt in (0,) or nxt in seen or (chain and nxt == 1):
            return chain, None
        seen.add(nxt)
        inf = info(nxt)
        if inf is None:
            return chain, nxt
        path = exe(nxt)
        row = {k: inf[k] for k in ("pid", "ppid", "t", "epoch", "start")}
        row.update(name=os.path.basename(path) or inf["name"], exe=path, args=None, args_why=None)
        if not args:
            row["args_why"] = "没要参数"
        elif nxt not in mine or abs(mine[nxt] - inf["t"]) > 1e-3:
            row["args_why"] = "进程 %d 不在本进程的祖先链上，不读它的参数" % nxt
        else:
            row["args"], row["args_why"] = _argv(nxt)
            again = info(nxt)
            if row["args"] is not None and (again is None or again["t"] != inf["t"]):
                row["args"], row["args_why"] = None, "读参数时进程 %d 换了人" % nxt
        chain.append(row)
        if nxt == 1:
            return chain, None
        nxt = inf["ppid"]
    return chain, None


# ---------- 打开的文件 ----------

def _fd_paths(pid):
    """(路径列表, 读得到没有)：当前目录、各文件描述符里的文件与目录。"""
    _, lp = _libs()
    paths = []
    ok_cwd, here = _vnode_cwd(lp, pid)
    if here:
        paths.append(here)
    n = lp.proc_pidinfo(pid, _LISTFDS, ctypes.c_uint64(0), None, 0)
    if n <= 0:
        return paths, ok_cwd
    buf = ctypes.create_string_buffer(n + 8 * 64)
    n = lp.proc_pidinfo(pid, _LISTFDS, ctypes.c_uint64(0), buf, len(buf))
    if n <= 0:
        return paths, ok_cwd
    raw = buf.raw
    fb = ctypes.create_string_buffer(1200)
    for i in range(0, n - n % 8, 8):
        if _u32(raw, i + 4) != _FD_TYPE_VNODE:
            continue
        if lp.proc_pidfdinfo(pid, _i32(raw, i), _FD_VNODEPATH, fb, 1200) == 1200:
            p = _cstr(fb.raw, 176, 1024, "surrogateescape")
            if p:
                paths.append(p)
    return paths, True


def _mapped_paths(pid, limit=20000):
    """映射进内存的文件（载入的库、mmap 的文件）。lsof 的 txt 一类；文件描述符关了它们照样占着文件。
    struct proc_regionwithpathinfo：pri_address 在 80、pri_size 在 88，路径在 248（vnode_info 之后）。"""
    _, lp = _libs()
    buf = ctypes.create_string_buffer(_REGION_SZ)
    out, seen, addr = [], set(), 0
    for _ in range(limit):
        if lp.proc_pidinfo(pid, _REGIONPATH2, ctypes.c_uint64(addr), buf, _REGION_SZ) <= 0:
            break
        raw = buf.raw
        a = int.from_bytes(raw[80:88], "little")
        sz = int.from_bytes(raw[88:96], "little")
        p = _cstr(raw, 248, 1024, "surrogateescape")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
        if a + sz <= addr:
            break
        addr = a + sz
    return out


def open_files(pid, mapped=True):
    """进程 pid 开着的文件与目录（当前目录、文件描述符、可执行文件、映射进内存的文件）的路径列表；读不到返回 None。
    mapped=False 不列映射进内存的文件（入口认祖先只看它开着什么，省下逐段问内存区的那几毫秒）。"""
    if _FAKE is not None:
        p = _FAKE["procs"].get(pid)
        if p is None or p.get("hidden") or p.get("fd_hidden") or p.get("zombie"):
            return None
        return list(p.get("files") or []) + ([p["exe"]] if p.get("exe") else [])
    if not ENABLED or not _valid(pid):
        return None
    if LINUX:
        return _lx_open_files(pid, mapped)
    try:
        paths, ok = _fd_paths(pid)
    except Exception:
        return None
    if not ok:
        return None
    e = exe(pid)
    if e:
        paths.append(e)
    if mapped:
        try:
            have = set(paths)
            paths.extend(p for p in _mapped_paths(pid) if p not in have)
        except Exception:
            pass
    return paths


class OpenScan(object):
    """全机查一次谁开着什么：files {pid: [路径]}，names {pid: 名字}；
    blind_same_user：本用户的进程里开着的文件读不到的进程号；other_users：别的用户的进程个数（看不到，盲区）；
    hidden：进程号在、连是谁都读不到的个数；complete：进程号列全了没有；self_seen：本进程在不在结果里。
    ok 为真才算查全了（本用户的进程都看得到）。"""

    def __init__(self):
        self.files, self.names = {}, {}
        self.blind_same_user, self.other_users, self.hidden = [], 0, 0
        self.complete, self.self_seen = True, False

    @property
    def ok(self):
        return self.complete and self.self_seen and self.hidden == 0 and not self.blind_same_user

    @property
    def why(self):
        if self.ok:
            return None
        bits = []
        if not self.complete:
            bits.append("列不出全机进程号（proc_listallpids 被拒，沙盒里常见）")
        if self.hidden:
            bits.append("%d 个进程连是谁都读不到" % self.hidden)
        if self.blind_same_user:
            bits.append("本用户的进程 %s 开着的文件读不到" % ",".join(map(str, self.blind_same_user[:10])))
        if not self.self_seen:
            bits.append("结果里没有本进程自己")
        return "列得不全：" + "；".join(bits)


def scan_open_files(uid=None):
    """全机查一次各进程开着的文件（只看 uid 的进程，缺省本用户；别的用户的只计个数）。返回 OpenScan。"""
    uid = os.getuid() if uid is None else uid
    sc = OpenScan()
    pids, sc.complete = list_pids()
    me = os.getpid()
    for pid in pids:
        inf, err = _raw_info(pid)
        if inf is None:
            if err == errno.ESRCH:
                continue
            sh = short_info(pid)
            if sh is None:
                if exists(pid):
                    sc.hidden += 1
                continue
            if sh["uid"] != uid:
                sc.other_users += 1
            elif not sh["zombie"]:
                sc.blind_same_user.append(pid)
            continue
        if inf["zombie"]:
            continue
        if inf["uid"] != uid:
            sc.other_users += 1
            continue
        paths = open_files(pid)
        if paths is None:
            if exists(pid) and state(pid) == ALIVE:
                sc.blind_same_user.append(pid)
            continue
        sc.files[pid] = paths
        sc.names[pid] = inf["name"] or inf["comm"]
        if pid == me:
            sc.self_seen = True
    return sc


def holders(path, scan=None):
    """开着 path（实际路径相同）的进程号，(列表, 查全了没有)。scan 不给就全机查一次。"""
    sc = scan or scan_open_files()
    want = os.path.realpath(path)
    names = {os.path.basename(want), os.path.basename(path)}
    hits = sorted(pid for pid, ps in sc.files.items()
                  if any(os.path.basename(p) in names and _real(p) == want for p in ps))
    return hits, sc.ok


def _real(p):
    try:
        return os.path.realpath(p)
    except (OSError, ValueError):
        return p


# ---------- 命令行（给 shell 脚本用） ----------

def etime_text(start, now=None):
    """已跑时长，写法同 ps 的 etime：[[天-]时:]分:秒。"""
    s = max(0, int((time.time() if now is None else now) - start))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return "%d-%02d:%02d:%02d" % (d, h, m, s)
    if h:
        return "%02d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def group_lines(pgid, tab=None):
    """(行列表, 列全了没有)：进程组 pgid 里读得到的进程，一行一个「进程号 组号 已跑时长 名字」，列宽同 ps -o pid=,pgid=,etime=；
    最后一列是名字（不读别的进程的命令行），僵尸的名字写「(僵尸)」。"""
    tab = table() if tab is None else tab
    out = []
    for pid in group(tab, pgid):
        r = tab[pid]
        et = etime_text(r["t"] if r.get("t") is not None else r["epoch"]) if r.get("epoch") is not None else "?"
        name = "(僵尸)" if r.get("zombie") else (r.get("name") or r.get("comm") or "?")
        out.append("%7d %7d %11s %s" % (pid, pgid, et, name))
    return out, bool(tab.complete and not tab.hidden)


def main(argv=None):
    """omo_proc.py group <进程组号>：打出 group_lines 的行。退出码：0 列全了；1 列得不全（有进程读不到，或列不出全机进程号），
    读得到的照样打；2 用法不对。shell 脚本用它列一个进程组的组员（原来是 ps -eo）。"""
    a = sys.argv[1:] if argv is None else argv
    if len(a) != 2 or a[0] != "group" or not a[1].isdigit() or int(a[1]) < 1:
        sys.stderr.write("用法：omo_proc.py group <进程组号>\n")
        return 2
    lines, full = group_lines(int(a[1]))
    for line in lines:
        print(line)
    return 0 if full else 1


if __name__ == "__main__":
    sys.exit(main())
