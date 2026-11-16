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
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

VERSION = "1.0.0"
APP_NAME = "LANPush 被控端"
DISCOVERY_PORT = 8099
CHUNK = 1024 * 1024
IDLE_INTERVAL = 2.0
MAX_RETRY = 5


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


def load_config() -> dict:
    cfg = {"server": "", "saveDir": "", "id": "", "name": ""}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    if not cfg.get("id"):
        cfg["id"] = default_id()
    if not cfg.get("name"):
        cfg["name"] = socket.gethostname()
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


def single_instance() -> bool:
    """命名互斥体，防止重复运行"""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Global\\LANPushAgentMutex")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _ = handle
        return True
    except Exception:
        return True


# ---------------------------------------------------------------- HTTP
def post_json(url: str, obj: dict, timeout: float = 15.0) -> dict:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def discover(timeout: float = 8.0):
    """监听主控端广播，返回服务地址"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", DISCOVERY_PORT))
    except Exception as e:
        log("监听发现端口失败：%s" % e)
        return None
    s.settimeout(1.0)
    log("正在局域网内寻找主控端…")
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, _addr = s.recvfrom(4096)
        except socket.timeout:
            continue
        except Exception:
            break
        try:
            info = json.loads(data.decode("utf-8"))
        except Exception:
            continue
        if info.get("service") == "lanpush" and info.get("url"):
            s.close()
            log("找到主控端：%s（%s）" % (info.get("url"), info.get("name", "")))
            return info["url"]
    s.close()
    log("没有找到主控端广播")
    return None


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
    if not single_instance():
        log("已经有一个被控端在运行，本次退出")
        return

    cfg = load_config()
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--server" and i + 1 < len(args):
            cfg["server"] = args[i + 1]
        elif a == "--dir" and i + 1 < len(args):
            cfg["saveDir"] = args[i + 1]
    save_config(cfg)

    aid = cfg["id"]
    log("%s %s 启动，机器名 %s" % (APP_NAME, VERSION, cfg["name"]))
    log("接收目录：%s" % cfg["saveDir"])

    base = cfg.get("server") or ""
    fail_streak = 0

    while True:
        if not base or fail_streak >= 3:
            found = discover(8)
            if found:
                base = found
                cfg["server"] = base
                save_config(cfg)
                fail_streak = 0
            elif not base:
                log("仍未找到主控端，10 秒后重试。也可以确认主控端是否已启动。")
                time.sleep(10)
                continue

        try:
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
            fail_streak = 0
            tasks = resp.get("tasks") or []
        except Exception as e:
            fail_streak += 1
            log("连接主控端失败（第 %d 次）：%s" % (fail_streak, e))
            if fail_streak >= 3:
                log("尝试重新发现主控端…")
            time.sleep(min(2 * fail_streak, 8))
            continue

        if not tasks:
            time.sleep(IDLE_INTERVAL)
            continue

        base = server_url or base
        for t in tasks:
            try:
                run_task(base, aid, t, cfg)
            except Exception as e:
                log("任务异常：%s" % e)
        time.sleep(0.5)


if __name__ == "__main__":
    while True:
        try:
            main()
            break
        except KeyboardInterrupt:
            log("已手动停止")
            break
        except Exception as e:
            log("运行异常，5 秒后自动重启：%s" % e)
            time.sleep(5)
