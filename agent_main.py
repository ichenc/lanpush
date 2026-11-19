# -*- coding: utf-8 -*-
"""
LANPush 被控端
- 启动后自动发现主控端（同一局域网 UDP 广播），无需手工填 IP
- 轮询任务，按 Range 断点续传下载到指定目录
- 传输进度实时回报主控端，失败自动重试
"""
import hashlib
import json
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from tray import tray_available, run_tray, IDI_APPLICATION, IDI_WARNING

VERSION = "1.0.2"
APP_NAME = "LANPush 被控端"
DISCOVERY_PORT = 8099        # 主控 -> 被控，广播
DISCOVERY_REPLY_PORT = 8098  # 被控 -> 主控，主动询问
CHUNK = 1024 * 1024
IDLE_INTERVAL = 2.0
MAX_RETRY = 5

# 托盘相关状态，供菜单展示
STATUS = {"base": "", "last_ok": 0.0, "device": "", "running": True}


# ---------------------------------------------------------------- 基础
def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = base_dir()
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
CONFIG_PATH = DATA / "agent_config.json"
LOG_PATH = DATA / "agent.log"

LOG_LOCK = threading.Lock()


def log(msg: str):
    line = time.strftime("%m-%d %H:%M:%S") + "  " + msg
    with LOG_LOCK:
        try:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2 * 1024 * 1024:
                try:
                    LOG_PATH.replace(LOG_PATH.with_suffix(".old.log"))
                except Exception:
                    pass
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def default_save_dir() -> str:
    """挑一个真正可写的目录"""
    for d in ("D:\\", "E:\\"):
        if not os.path.isdir(d):
            continue
        cand = os.path.join(d, "LAN接收")
        try:
            os.makedirs(cand, exist_ok=True)
            t = os.path.join(cand, ".write_test")
            with open(t, "wb") as f:
                f.write(b"1")
            os.remove(t)
            return cand
        except Exception:
            continue
    fallback = BASE / "接收"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return str(fallback)


def default_id() -> str:
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "PC"
    raw = hostname + "-" + format(uuid.getnode(), "x")[-6:]
    return raw


def current_hostname() -> str:
    """读取系统当前计算机名（改用机器名后以此为准）"""
    try:
        return (socket.gethostname() or "").strip()
    except Exception:
        return ""


def load_config() -> dict:
    cfg = {"server": "", "saveDir": "", "id": "", "name": ""}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    if not cfg.get("id"):
        cfg["id"] = default_id()
    # 关键修复：机器名每次启动都以系统当前值为准，
    # 否则改完电脑名后配置文件里仍是旧的 DESKTOP-xxx。
    osname = current_hostname()
    if osname and not cfg.get("nameLocked"):
        cfg["name"] = osname
    if not cfg.get("name"):
        cfg["name"] = osname or "PC"
    if not cfg.get("saveDir"):
        cfg["saveDir"] = default_save_dir()
    save_config(cfg)
    return cfg


def save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


_MUTEX_HANDLE = None  # 持有句柄，避免被 GC 释放导致互斥失效


def single_instance() -> bool:
    """命名互斥体，防止重复运行。必须在创建托盘之前调用。"""
    global _MUTEX_HANDLE
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        # Global 需特权，普通用户改到 Local；两种都试，命中已存在即判定重复
        for name in ("Global\\LANPushAgentMutex", "Local\\LANPushAgentMutex"):
            ctypes.set_last_error(0)
            h = kernel32.CreateMutexW(None, False, name)
            err = ctypes.get_last_error()
            if not h:
                continue  # 创建失败（如无 Global 权限），试下一个
            if err == 183:  # ERROR_ALREADY_EXISTS
                return False
            _MUTEX_HANDLE = h
            return True
        return True  # 都建不了就放行，避免误杀
    except Exception:
        return True


# ---------------------------------------------------------------- HTTP
def post_json(url: str, obj: dict, timeout: float = 15.0) -> dict:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _local_broadcasts():
    """本机各网卡的广播地址（多网卡时每个网段都试着喊一声）"""
    ips = {"255.255.255.255"}
    try:
        info = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for item in info:
            ip = item[4][0]
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    out = set()
    for ip in ips:
        if ip == "255.255.255.255":
            out.add(ip)
            continue
        parts = ip.split(".")
        if len(parts) == 4:
            out.add(".".join(parts[:3]) + ".255")
    return sorted(out | {"255.255.255.255"})


def _parse_server(info, addr):
    """用数据来源 IP 构造地址 —— 不信任包里自报的 url（多网卡/虚拟网卡会错）"""
    port = int(info.get("port") or 0)
    if not port:
        m = re.search(r":(\d+)(?:/|$)", str(info.get("url") or ""))
        port = int(m.group(1)) if m else 8080
    ip = addr[0] if addr and addr[0] else ""
    if not ip or ip.startswith("0."):
        return None
    return "http://%s:%d" % (ip, port)


def discover(timeout: float = 8.0):
    """
    双通道发现：
    1) 监听主控端在 8099 的广播
    2) 主动向各网段 8098 发 hello，主控端单播回复（绕开无法收广播的环境）
    两种方式都只用「数据来源 IP」构造地址，避免主控端多网卡时自报错误 IP。
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("", DISCOVERY_PORT))
        listener.settimeout(0.6)
    except Exception as e:
        log("监听发现端口 %d 失败：%s" % (DISCOVERY_PORT, e))
        # 即便绑定失败，仍可用主动询问通道
        listener = None
    log("正在局域网内寻找主控端…")

    hello = json.dumps({"service": "lanpush", "hello": 1, "name": socket.gethostname()}).encode("utf-8")
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except Exception:
        pass

    end = time.time() + timeout
    last_hello = 0.0
    found = None
    while time.time() < end and not found:
        # 每 2 秒主动喊一次
        if time.time() - last_hello >= 2.0:
            for b in _local_broadcasts():
                try:
                    sender.sendto(hello, (b, DISCOVERY_REPLY_PORT))
                except Exception:
                    pass
            last_hello = time.time()

        if listener is None:
            time.sleep(0.4)
            continue
        try:
            data, addr = listener.recvfrom(4096)
        except socket.timeout:
            continue
        except Exception:
            break
        try:
            info = json.loads(data.decode("utf-8"))
        except Exception:
            continue
        if info.get("service") != "lanpush":
            continue
        server = _parse_server(info, addr)
        if server:
            found = server
            log("找到主控端：%s（%s）" % (found, info.get("name", "")))
            break

    try:
        sender.close()
    except Exception:
        pass
    if listener is not None:
        listener.close()
    if not found:
        log("没有找到主控端广播（已尝试广播监听 + 主动询问）")
    return found


# ---------------------------------------------------------------- 进度回报
class Reporter:
    def __init__(self, base, aid, tid, files):
        self.base = base.rstrip("/")
        self.aid = aid
        self.tid = tid
        self.files = files  # name -> {done,size,st}
        self.last = 0.0
        self.status = "running"
        self.msg = ""

    def touch(self, name, done=None, size=None, st=None):
        f = self.files.setdefault(name, {"done": 0, "size": 0, "st": "pending"})
        if done is not None:
            f["done"] = int(done)
        if size is not None:
            f["size"] = int(size)
        if st:
            f["st"] = st

    def flush(self, force=False, status=None, msg=None):
        now = time.time()
        if status:
            self.status = status
            force = True
        if msg is not None:
            self.msg = msg
        if not force and now - self.last < 1.0:
            return
        self.last = now
        payload = {
            "tid": self.tid,
            "aid": self.aid,
            "status": self.status,
            "msg": self.msg,
            "files": [dict(name=k, **v) for k, v in self.files.items()],
        }
        try:
            r = post_json(self.base + "/api/progress", payload, timeout=10)
            return r
        except Exception:
            return None


# ---------------------------------------------------------------- 下载
def unique_path(p: Path) -> Path:
    stem, suf, i = p.stem, p.suffix, 1
    while p.exists():
        i += 1
        p = p.with_name("%s (%d)%s" % (stem, i, suf))
    return p


def tmp_name(rel: str) -> str:
    """把 相对路径 压成一个安全的临时文件名（避免深目录/超长路径）"""
    flat = rel.replace("\\", "/").replace("/", "@@")
    if len(flat) > 90:
        h = hashlib.md5(rel.encode("utf-8")).hexdigest()[:16]
        flat = h + os.path.splitext(rel)[1][:12]
    return flat


def fetch_file(base, task, f, dest: Path, rep: Reporter, cancel_flag):
    rel = str(f.get("rel") or f.get("name") or "")
    size, mt = int(f["size"]), f.get("mt")
    final = dest / rel.replace("\\", "/")
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise IOError("无法创建子目录：%s" % e)
    tmpdir = dest / ".lanpush_tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    base_name = tmp_name(rel)
    part = tmpdir / (base_name + ".part")
    meta = tmpdir / (base_name + ".meta")

    # 换过文件就重头来
    if part.exists():
        try:
            old = json.loads(meta.read_text(encoding="utf-8"))
            if str(old.get("mt")) != str(mt) or int(old.get("size", -1)) != size:
                part.unlink()
                meta.unlink(missing_ok=True)
        except Exception:
            pass
    if not part.exists():
        try:
            meta.write_text(json.dumps({"rel": rel, "mt": mt, "size": size}), encoding="utf-8")
        except Exception:
            pass

    start = part.stat().st_size if part.exists() else 0
    if start > size:
        start = 0
        try:
            part.unlink()
        except Exception:
            pass
    rep.touch(rel, done=start, size=size, st="running")
    rep.flush(force=True)

    url = "%s/api/download?task=%s&i=%d" % (base.rstrip("/"), urllib.parse.quote(task["id"]), f["i"])
    attempt = 0
    while True:
        if cancel_flag():
            raise InterruptedError("任务已取消")
        try:
            headers = {"Range": "bytes=%d-" % start} if start > 0 else {}
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                mode = "ab"
                if start > 0 and resp.status == 200:
                    start, mode = 0, "wb"   # 服务端不支持续传
                with open(part, mode) as fh:
                    while True:
                        if cancel_flag():
                            raise InterruptedError("任务已取消")
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        start += len(chunk)
                        rep.touch(rel, done=start, size=size)
                        rep.flush()
            break
        except InterruptedError:
            raise
        except Exception as e:
            attempt += 1
            if attempt >= MAX_RETRY:
                raise IOError("下载失败（重试 %d 次）：%s" % (attempt, e))
            log("  %s 中断，%d 秒后从 %s 处续传…" % (rel, min(2 * attempt, 10), human(start)))
            time.sleep(min(2 * attempt, 10))
            start = part.stat().st_size if part.exists() else 0
            rep.touch(rel, done=start, size=size)
            rep.flush(force=True)

    got = part.stat().st_size if part.exists() else 0
    if got != size:
        raise IOError("文件大小不一致（%s / %s）" % (human(got), human(size)))
    return part


def human(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "-"


def place_file(part: Path, final: Path, overwrite: bool, skip_existing: bool) -> str:
    if final.exists() and skip_existing:
        try:
            part.unlink()
        except Exception:
            pass
        return "skipped"
    if final.exists() and not overwrite:
        final = unique_path(final)
    try:
        if final.exists():
            final.unlink()
    except Exception:
        pass
    os.replace(part, final)
    return "saved"


def run_task(base, aid, task, cfg) -> None:
    tid = task["id"]
    dest = Path(task.get("dir") or cfg["saveDir"])
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log("无法创建目录 %s：%s" % (dest, e))
        r = Reporter(base, aid, tid, {})
        r.flush(force=True, status="error", msg="无法创建目录：%s" % str(e)[:80])
        return

    files = task.get("files", [])
    rep = Reporter(
        base, aid, tid,
        {str(f.get("rel") or f.get("name")): {"done": 0, "size": f["size"], "st": "pending"} for f in files},
    )
    log("收到任务 %s：%d 个文件 → %s" % (tid, len(files), dest))
    rep.flush(force=True, status="running")
    # 错开 0~1.5 秒，避免 20 台同时猛冲主控端磁盘
    time.sleep(random.uniform(0, 1.5))

    cancelled = {"v": False}
    for f in files:
        if cancelled["v"]:
            break
        rel = str(f.get("rel") or f.get("name"))
        try:
            part = fetch_file(base, task, f, dest, rep, lambda: cancelled["v"])
            final = dest / rel.replace("\\", "/")
            res = place_file(part, final, bool(task.get("overwrite")), bool(task.get("skip_existing")))
            try:
                (dest / ".lanpush_tmp" / (tmp_name(rel) + ".meta")).unlink(missing_ok=True)
            except Exception:
                pass
            rep.touch(rel, done=f["size"], size=f["size"], st="done")
            rep.flush(force=True)
            log("  %s %s（%s）" % ("跳过" if res == "skipped" else "完成", rel, human(f["size"])))
        except InterruptedError:
            cancelled["v"] = True
            rep.touch(rel, st="error")
            rep.flush(force=True, status="error", msg="已被主控端取消")
            log("任务 %s 已被取消" % tid)
            return
        except Exception as e:
            rep.touch(rel, st="error")
            rep.flush(force=True, status="error", msg=str(e)[:200])
            log("  %s 失败：%s" % (rel, e))
            return

    # 清掉空临时目录与残留元数据
    try:
        tmp = dest / ".lanpush_tmp"
        if tmp.is_dir():
            for m in tmp.glob("*.meta"):
                if not m.with_name(m.name[:-5] + ".part").exists():
                    try:
                        m.unlink()
                    except Exception:
                        pass
            if not any(tmp.iterdir()):
                tmp.rmdir()
    except Exception:
        pass
    rep.flush(force=True, status="done")
    log("任务 %s 全部完成" % tid)


# ---------------------------------------------------------------- 主循环
def main():
    cfg = load_config()
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--server" and i + 1 < len(args):
            cfg["server"] = args[i + 1]
        elif a == "--dir" and i + 1 < len(args):
            cfg["saveDir"] = args[i + 1]
    save_config(cfg)

    aid = cfg["id"]
    STATUS["device"] = cfg["name"]
    log("%s %s 启动，机器名 %s" % (APP_NAME, VERSION, cfg["name"]))
    log("接收目录：%s" % cfg["saveDir"])

    base = cfg.get("server") or ""
    fail_streak = 0

    while STATUS["running"]:
        if not base or fail_streak >= 3:
            found = discover(8)
            if found:
                base = found
                cfg["server"] = base
                save_config(cfg)
                fail_streak = 0
            elif not base:
                log("仍未找到主控端，10 秒后重试。也可以确认主控端是否已启动。")
                for _ in range(10):
                    if not STATUS["running"]:
                        return
                    time.sleep(1)
                continue

        try:
            # 每次心跳都校准一次机器名：运行中改名也能立刻反映到主控端
            osname = current_hostname()
            if osname and not cfg.get("nameLocked") and cfg.get("name") != osname:
                cfg["name"] = osname
                STATUS["device"] = osname
                save_config(cfg)
            free = 0
            try:
                import shutil

                free = shutil.disk_usage(cfg["saveDir"]).free
            except Exception:
                pass
            resp = post_json(
                base.rstrip("/") + "/api/sync",
                {"id": aid, "name": cfg["name"], "saveDir": cfg["saveDir"], "free": free, "ver": VERSION},
                timeout=15,
            )
            server_url = resp.get("server") or base
            if server_url and server_url != base:
                base = server_url
                cfg["server"] = base
                save_config(cfg)
            fail_streak = 0
            STATUS["base"] = base
            STATUS["last_ok"] = time.time()
            tasks = resp.get("tasks") or []
        except Exception as e:
            fail_streak += 1
            log("连接主控端失败（第 %d 次）：%s" % (fail_streak, e))
            if fail_streak >= 3:
                log("尝试重新发现主控端…")
            for _ in range(min(2 * fail_streak, 8)):
                if not STATUS["running"]:
                    return
                time.sleep(1)
            continue

        if not tasks:
            for _ in range(int(IDLE_INTERVAL)):
                if not STATUS["running"]:
                    return
                time.sleep(1)
            continue

        base = server_url or base
        for t in tasks:
            if not STATUS["running"]:
                return
            try:
                run_task(base, aid, t, cfg)
            except Exception as e:
                log("任务异常：%s" % e)
        time.sleep(0.5)


def _status_line():
    if STATUS["last_ok"] and time.time() - STATUS["last_ok"] < 20:
        return "已连接主控：%s" % STATUS["base"]
    return "未连接主控，正在寻找…"


def _tray_main():
    def show_log():
        try:
            os.startfile(str(LOG_PATH))
        except Exception:
            try:
                subprocess.Popen(["notepad", str(LOG_PATH)])
            except Exception:
                pass

    def rediscover():
        # 清掉旧地址，下轮主循环会重新发现
        try:
            cfg = load_config()
            cfg["server"] = ""
            save_config(cfg)
        except Exception:
            pass
        log("手动触发重新发现主控端")

    def do_exit():
        STATUS["running"] = False
        log("用户从托盘退出")
        # 不能等主循环优雅退出（可能在长 sleep），直接结束
        os._exit(0)

    items = [
        (1001, "查看状态", lambda: msgbox(_status_line(), APP_NAME)),
        (1002, "重新寻找主控端", rediscover),
        (1003, "查看运行日志", show_log),
        (1004, None, None),
        (1005, "退出被控端", do_exit),
    ]
    cand = Path(getattr(sys, "_MEIPASS", BASE)) / "icons" / "agent.ico"
    icon_path = str(cand if cand.is_file() else BASE / "icons" / "agent.ico")
    run_tray(
        "LANPush 被控端", items,
        on_double_click=show_log, icon_id=IDI_APPLICATION,
        icon_path=icon_path,
    )


def msgbox(text, title="LANPush"):
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), 0x40)
    except Exception:
        log(str(text))


if __name__ == "__main__":
    # 必须在创建托盘之前检查：否则重复实例的托盘线程已起来，return 无法结束进程
    if not single_instance():
        try:
            log("已经有一个被控端在运行，本次退出（未创建第二个托盘）")
        except Exception:
            pass
        os._exit(0)
    if tray_available():
        threading.Thread(target=_tray_main, daemon=False).start()
    while STATUS["running"]:
        try:
            main()
            break
        except KeyboardInterrupt:
            log("已手动停止")
            break
        except Exception as e:
            log("运行异常，5 秒后自动重启：%s" % e)
            for _ in range(5):
                if not STATUS["running"]:
                    break
                time.sleep(1)
