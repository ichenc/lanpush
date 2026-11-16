# -*- coding: utf-8 -*-
"""
LANPush 主控端
- 提供网页控制台（默认 http://本机IP:8080）
- 管理被控端在线状态、文件仓库、分发任务
- 通过 HTTP Range 支持断点续传，UDP 广播供被控端自动发现
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.0.0"
APP_NAME = "LANPush"
DISCOVERY_PORT = 8099
CHUNK = 1024 * 1024
AGENT_ONLINE_SECONDS = 15


# ---------------------------------------------------------------- 基础工具
def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = base_dir()
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
CONFIG_PATH = DATA / "host_config.json"
TASKS_PATH = DATA / "tasks.json"

DEFAULT_CONFIG = {
    "port": 8080,
    "repo": str(BASE / "待分发"),
    "autoOpenBrowser": True,
}


def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def msgbox(text: str, title: str = APP_NAME):
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        print(text)


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass


def safe_name(name: str) -> str:
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    if name in ("", ".", ".."):
        name = "unnamed"
    return name[:180]


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


# ---------------------------------------------------------------- 状态
CFG = dict(DEFAULT_CONFIG)
CFG.update(load_json(CONFIG_PATH, {}))
REPO = Path(CFG["repo"])
REPO.mkdir(parents=True, exist_ok=True)
save_json(CONFIG_PATH, CFG)

LOCK = threading.RLock()
AGENTS = {}   # id -> {name, ip, last, saveDir, free}
TASKS = {}    # tid -> task
TASK_ORDER = []


def load_tasks():
    data = load_json(TASKS_PATH, [])
    if not isinstance(data, list):
        return
    with LOCK:
        for t in data:
            if isinstance(t, dict) and t.get("id"):
                TASKS[t["id"]] = t
                TASK_ORDER.append(t["id"])


def persist_tasks():
    with LOCK:
        items = [TASKS[tid] for tid in TASK_ORDER if tid in TASKS]
    save_json(TASKS_PATH, items[-60:])


_DIR_CACHE = {}   # name -> (computed_at, size, count)


def walk_files(root: Path):
    """递归列出目录下的文件（跳过传输临时目录），返回 [(相对路径, 完整路径, 大小, mtime)]"""
    out = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".lanpush_tmp"]
        for fn in files:
            full = Path(cur) / fn
            if fn.endswith(".part") or fn.endswith(".meta"):
                continue
            try:
                st = full.stat()
            except Exception:
                continue
            try:
                rel = full.relative_to(REPO).as_posix()
            except Exception:
                continue
            out.append((rel, full, st.st_size, int(st.st_mtime)))
    return out


def dir_stat(p: Path, ttl=8.0):
    """目录大小做缓存，避免每秒轮询都去遍历大盘"""
    now = time.time()
    hit = _DIR_CACHE.get(p.name)
    if hit and now - hit[0] < ttl:
        return hit[1], hit[2]
    size = 0
    cnt = 0
    for _rel, _full, s, _mt in walk_files(p):
        size += s
        cnt += 1
    _DIR_CACHE[p.name] = (now, size, cnt)
    return size, cnt


def repo_list():
    out = []
    try:
        items = sorted(REPO.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        for p in items:
            if p.name.startswith(".lanpush"):
                continue
            if p.is_dir():
                size, cnt = dir_stat(p)
                out.append({"name": p.name, "size": size, "isDir": True, "count": cnt})
            elif p.is_file() and not p.name.endswith(".uploading"):
                try:
                    out.append({"name": p.name, "size": p.stat().st_size, "isDir": False, "count": 1})
                except Exception:
                    pass
    except Exception:
        pass
    return out


def agent_online(a) -> bool:
    return (time.time() - a.get("last", 0)) < AGENT_ONLINE_SECONDS


def next_tid() -> str:
    return "T" + time.strftime("%Y%m%d%H%M%S") + str(int(time.time() * 1000) % 1000).zfill(3)


def task_recalc(t: dict):
    """根据各被控端回报刷新整体进度与状态"""
    files_total = sum(f.get("size", 0) for f in t.get("files", []))
    done_cnt = 0
    err_cnt = 0
    for aid, st in t.get("agents", {}).items():
        if st.get("status") == "done":
            done_cnt += 1
            st["done"] = files_total
        elif st.get("status") == "error":
            err_cnt += 1
        st["total"] = files_total
    n = len(t.get("agents", {}))
    if n and done_cnt == n:
        t["status"] = "done"
    elif n and (done_cnt + err_cnt) == n:
        t["status"] = "partial" if done_cnt else "failed"
    elif t.get("cancel"):
        t["status"] = "cancelled"
    else:
        t["status"] = "running"


# ---------------------------------------------------------------- 广播发现
def discovery_loop(port: int):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except Exception:
        pass
    targets = {("255.255.255.255", DISCOVERY_PORT)}
    ip = lan_ip()
    if ip.count(".") == 3:
        targets.add((".".join(ip.split(".")[:3]) + ".255", DISCOVERY_PORT))
    while True:
        payload = json.dumps(
            {
                "service": "lanpush",
                "name": socket.gethostname(),
                "url": "http://%s:%d" % (ip, port),
                "ver": VERSION,
            }
        ).encode("utf-8")
        for tgt in targets:
            try:
                sock.sendto(payload, tgt)
            except Exception:
                pass
        time.sleep(3)


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "LANPush/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---------- 低层输出
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except Exception:
                pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self, limit=8 * 1024 * 1024) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        n = min(n, limit)
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _query(self):
        q = urllib.parse.urlsplit(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    # ---------- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._send(200, html_page(), "text/html; charset=utf-8")
            if path == "/favicon.ico":
                return self._send(200, b"", "image/x-icon")
            if path == "/api/state":
                return self._json(state_payload())
            if path == "/api/download":
                return self.download()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        self._send(404, b"not found", "text/plain")

    # ---------- POST
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/upload":
                return self.upload()
            if path == "/api/distribute":
                return self.distribute()
            if path == "/api/refresh":
                _DIR_CACHE.clear()
                return self._json({"repo": repo_list()})
            if path == "/api/open_repo":
                try:
                    subprocess.Popen(["explorer", str(REPO)])
                except Exception:
                    pass
                return self._json({"ok": True, "repo": str(REPO)})
            if path == "/api/sync":
                return self.sync()
            if path == "/api/progress":
                return self.progress()
            if path == "/api/cancel":
                return self.cancel()
            if path == "/api/shutdown":
                self._json({"ok": True})
                threading.Timer(0.4, lambda: os._exit(0)).start()
                return
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        self._send(404, b"not found", "text/plain")

    # ---------- 接口实现
    def sync(self):
        """被控端心跳 + 领取任务"""
        d = self._body()
        aid = str(d.get("id") or "")[:120]
        if not aid:
            return self._json({"error": "missing id"}, 400)
        now = time.time()
        with LOCK:
            a = AGENTS.get(aid) or {}
            a.update(
                {
                    "id": aid,
                    "name": str(d.get("name") or aid)[:64],
                    "ip": self.client_address[0],
                    "last": now,
                    "saveDir": str(d.get("saveDir") or "")[:260],
                    "free": d.get("free") or 0,
                    "ver": str(d.get("ver") or "")[:16],
                }
            )
            AGENTS[aid] = a
            pending = []
            for tid in TASK_ORDER:
                t = TASKS.get(tid)
                if not t or t.get("status") in ("done", "failed", "cancelled"):
                    continue
                st = t.get("agents", {}).get(aid)
                if not st or st.get("status") in ("done", "error"):
                    continue
                pending.append(
                    {
                        "id": tid,
                        "dir": t.get("dir", ""),
                        "overwrite": bool(t.get("overwrite")),
                        "skip_existing": bool(t.get("skipExisting")),
                        "files": [
                            {"i": i, "rel": f.get("rel") or f.get("name"), "size": f["size"], "mt": f.get("mt")}
                            for i, f in enumerate(t.get("files", []))
                        ],
                    }
                )
        self._json({"ok": True, "tasks": pending, "server": "http://%s:%d" % (lan_ip(), CFG["port"])})

    def progress(self):
        d = self._body()
        tid, aid = str(d.get("tid") or ""), str(d.get("aid") or "")
        with LOCK:
            t = TASKS.get(tid)
            if not t:
                return self._json({"ok": False})
            st = t.setdefault("agents", {}).setdefault(aid, {"files": {}})
            if d.get("status"):
                st["status"] = d["status"]
            if d.get("msg") is not None:
                st["msg"] = str(d["msg"])[:300]
            st["updated"] = time.time()
            acc = 0
            for f in d.get("files") or []:
                nm = str(f.get("name"))
                fs = st["files"].setdefault(nm, {})
                fs["name"] = nm
                fs["done"] = int(f.get("done") or 0)
                fs["size"] = int(f.get("size") or 0)
                fs["st"] = str(f.get("st") or "pending")
                acc += fs["done"]
            st["done"] = acc
            task_recalc(t)
            if st.get("status") in ("done", "error"):
                persist_tasks()
        self._json({"ok": True, "cancel": bool(TASKS.get(tid, {}).get("cancel"))})

    def distribute(self):
        d = self._body()
        agents = [str(x) for x in (d.get("agents") or [])]
        names = [safe_name(str(x)) for x in (d.get("files") or [])]
        target_dir = str(d.get("dir") or "").strip()
        if not agents:
            return self._json({"error": "请先勾选接收设备"}, 400)
        if not names:
            return self._json({"error": "请先选择要分发的文件或文件夹"}, 400)
        files = []
        for nm in names:
            p = REPO / nm
            if p.is_dir():
                # 文件夹：展开成里面的所有文件，保留相对路径
                walked = walk_files(p)
                if not walked:
                    return self._json({"error": "文件夹是空的：%s" % nm}, 400)
                for rel, _full, size, mt in walked:
                    files.append({"rel": rel, "size": size, "mt": mt})
            elif p.is_file():
                info = p.stat()
                files.append({"rel": nm, "size": info.st_size, "mt": int(info.st_mtime)})
            else:
                return self._json({"error": "找不到：%s" % nm}, 400)
        tid = next_tid()
        now = time.time()
        with LOCK:
            online = {aid: a.get("name", aid) for aid, a in AGENTS.items()}
            task = {
                "id": tid,
                "created": now,
                "createdText": time.strftime("%m-%d %H:%M:%S"),
                "dir": target_dir,
                "overwrite": bool(d.get("overwrite")),
                "skipExisting": bool(d.get("skipExisting")),
                "files": files,
                "labels": names,
                "status": "running",
                "cancel": False,
                "total": sum(f["size"] for f in files),
                "agents": {
                    aid: {"status": "pending", "msg": "", "files": {}, "done": 0, "name": online.get(aid, aid)}
                    for aid in agents
                },
            }
            TASKS[tid] = task
            TASK_ORDER.append(tid)
            persist_tasks()
        self._json({"ok": True, "tid": tid})

    def cancel(self):
        d = self._body()
        tid = str(d.get("tid") or "")
        with LOCK:
            t = TASKS.get(tid)
            if t and t.get("status") not in ("done", "failed"):
                t["cancel"] = True
                t["status"] = "cancelled"
                for st in t.get("agents", {}).values():
                    if st.get("status") in ("pending", "running"):
                        st["status"] = "error"
                        st["msg"] = "已被主控端取消"
                persist_tasks()
        self._json({"ok": True})

    def upload(self):
        name = safe_name(self._query().get("name", ""))
        if not name:
            return self._json({"error": "bad name"}, 400)
        try:
            total = int(self.headers.get("Content-Length") or 0)
        except Exception:
            total = 0
        target = REPO / name
        stem, suf = target.stem, target.suffix
        k = 1
        while target.exists():
            k += 1
            target = REPO / ("%s (%d)%s" % (stem, k, suf))
        tmp = REPO / (target.name + ".uploading")
        got = 0
        with open(tmp, "wb") as f:
            while got < total:
                chunk = self.rfile.read(min(CHUNK, total - got))
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
        if got != total:
            try:
                tmp.unlink()
            except Exception:
                pass
            return self._json({"error": "上传中断"}, 400)
        os.replace(tmp, target)
        self._json({"ok": True, "name": target.name, "size": got, "repo": repo_list()})

    def download(self):
        q = self._query()
        tid, idx = q.get("task", ""), q.get("i", "")
        with LOCK:
            t = TASKS.get(tid)
            if not t:
                return self._json({"error": "no task"}, 404)
            try:
                f = t["files"][int(idx)]
            except Exception:
                return self._json({"error": "no file"}, 404)
        rel = str(f.get("rel") or f.get("name") or "")
        if not rel or ".." in rel.replace("\\", "/").split("/"):
            return self._json({"error": "bad path"}, 400)
        path = (REPO / rel.replace("\\", "/")).resolve()
        try:
            path.relative_to(REPO.resolve())
        except Exception:
            return self._json({"error": "bad path"}, 403)
        if not path.is_file():
            return self._json({"error": "file missing"}, 404)
        size = path.stat().st_size
        start = 0
        m = re.match(r"bytes=(\d+)-", self.headers.get("Range") or "")
        if m:
            start = int(m.group(1))
        if start >= size:
            return self._send(416, b"", "text/plain", {"Content-Range": "bytes */%d" % size})
        length = size - start
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if m:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, size - 1, size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True


def state_payload() -> dict:
    now = time.time()
    with LOCK:
        agents = []
        for a in AGENTS.values():
            agents.append(
                {
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "ip": a.get("ip"),
                    "online": agent_online(a),
                    "saveDir": a.get("saveDir"),
                    "free": a.get("free"),
                    "freeText": human(a.get("free") or 0),
                    "ver": a.get("ver"),
                    "ago": int(now - a.get("last", now)),
                }
            )
        agents.sort(key=lambda x: (not x["online"], (x["name"] or "").lower()))
        tasks = []
        for tid in reversed(TASK_ORDER):
            t = TASKS.get(tid)
            if not t:
                continue
            tasks.append(t)
            if len(tasks) >= 15:
                break
    ip = lan_ip()
    return {
        "app": APP_NAME,
        "version": VERSION,
        "host": {
            "ip": ip,
            "port": CFG["port"],
            "url": "http://%s:%d" % (ip, CFG["port"]),
            "repo": str(REPO),
            "hostname": socket.gethostname(),
        },
        "agents": agents,
        "repo": repo_list(),
        "tasks": tasks,
        "defaultDir": str(CFG.get("defaultDir") or ""),
    }


def html_page() -> str:
    path = BASE / "web" / "index.html"
    if getattr(sys, "frozen", False):
        cand = Path(getattr(sys, "_MEIPASS", BASE)) / "web" / "index.html"
        if cand.is_file():
            path = cand
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        return "<h1>界面文件缺失</h1><pre>%s\n%s</pre>" % (path, e)


def main():
    load_tasks()
    port = int(CFG.get("port") or 8080)
    for attempt in range(12):
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            break
        except OSError:
            port += 1
    else:
        msgbox("没有可用端口（8080-8091 都被占用），程序退出。")
        return
    httpd.daemon_threads = True
    threading.Thread(target=discovery_loop, args=(port,), daemon=True).start()
    url = "http://127.0.0.1:%d" % port
    if CFG.get("autoOpenBrowser", True):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print("%s %s 已启动" % (APP_NAME, VERSION))
    print("本机访问：%s" % url)
    print("局域网访问：http://%s:%d" % (lan_ip(), port))
    print("文件仓库：%s" % REPO)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
