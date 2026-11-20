#!/usr/bin/env python3
"""mihomo-keeper: mihomo 管理页：内置面板（流量 / 代理 / 连接 / 规则 / 实时日志） + 订阅更新 / override 编辑 / 备份回滚 / 服务控制 / 日志。

零依赖，仅 Python 标准库 + PyYAML（merge-config.py 已依赖）。
认证：HTTP Basic 或专用 Token。Token 支持链接自动登录（HttpOnly 会话 Cookie）和 Bearer API 认证。
密码和 Token 分别取自环境变量或 manager.secret / manager.token 文件。

所有路径 / 端口均可用环境变量配置，详见 README.md「配置」一节。
"""
import base64
import difflib
import hmac
import gzip
import json
import os
import platform
import queue
import re
import secrets
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

APP_DIR = Path(__file__).resolve().parent  # manager.py 所在目录（merge-config.py / update-subscription.sh 默认与它放在一起）
MIHOMO_DIR = Path(os.environ.get("MIHOMO_DIR", "/etc/mihomo"))
MIHOMO_BIN = os.environ.get("MIHOMO_BIN", "/usr/local/bin/mihomo")
CONFIG_FILE = MIHOMO_DIR / "config.yaml"
OVERRIDE_FILE = MIHOMO_DIR / "override.yaml"
MERGE_SCRIPT = Path(os.environ.get("MERGE_SCRIPT", str(APP_DIR / "merge-config.py")))
UPDATE_SCRIPT = Path(os.environ.get("UPDATE_SCRIPT", str(APP_DIR / "update-subscription.sh")))
BACKUP_DIR = MIHOMO_DIR / "backup"
SECRET_FILE = Path(os.environ.get("MANAGER_SECRET_FILE", str(MIHOMO_DIR / "manager.secret")))
TOKEN_FILE = Path(os.environ.get("MANAGER_TOKEN_FILE", str(MIHOMO_DIR / "manager.token")))
SESSION_MAX_AGE = int(os.environ.get("MANAGER_SESSION_MAX_AGE", "604800"))
SESSION_COOKIE = "mihomo_manager_session"
SUB_URL_FILE = Path(os.environ.get("MIHOMO_SUB_URL_FILE", str(MIHOMO_DIR / "subscription.url")))
LISTEN = os.environ.get("MANAGER_LISTEN", "0.0.0.0:9092")
AUTH_USER = os.environ.get("MANAGER_USER", "admin")
SERVICE = os.environ.get("MIHOMO_SERVICE", "mihomo")
# 外部面板（metacubexd / zashboard 等）跳转地址模板，留空则不显示跳转链接。
# 支持占位符：{host} 浏览器访问管理页用的主机名，{port} controller 端口，{secret} controller secret（均已 URL 编码）
DASHBOARD_URL = os.environ.get("MANAGER_DASHBOARD_URL", "").strip()
DASHBOARD_NAME = os.environ.get("MANAGER_DASHBOARD_NAME", "").strip() or "面板"

LOCK = threading.Lock()  # 串行化所有写操作


# ---------- helpers ----------
def run(cmd, timeout=180, env=None):
    e = dict(os.environ, NO_COLOR="1", TERM="dumb")
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=e)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as ex:
        return 124, ex.stdout or "", (ex.stderr or "") + f"\n[timeout after {timeout}s]"
    except OSError as ex:
        return 126, "", f"{type(ex).__name__}: {ex}"


def load_password():
    if os.environ.get("MANAGER_PASSWORD"):
        return os.environ["MANAGER_PASSWORD"].strip()
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    return ""


def load_token():
    if os.environ.get("MANAGER_TOKEN"):
        return os.environ["MANAGER_TOKEN"].strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    return ""


_META_CACHE = {"key": None, "val": (None, "")}
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
# 访问本机 controller 时不走 http_proxy 等环境变量里的代理
_CTRL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def cfg_meta():
    """从 config.yaml 读 external-controller / secret（按 mtime 缓存：内置面板会频繁调用）。"""
    try:
        st = CONFIG_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None, ""
    if _META_CACHE["key"] == key:
        return _META_CACHE["val"]
    try:
        cfg = yaml.load(CONFIG_FILE.read_text(encoding="utf-8"), Loader=_YAML_LOADER) or {}
    except Exception:
        return None, ""
    ec = str(cfg.get("external-controller") or "127.0.0.1:9090")
    host, port = ec.rsplit(":", 1)
    if host in ("0.0.0.0", "*", "::", "[::]", ""):
        host = "127.0.0.1"
    val = (f"http://{host}:{port}", str(cfg.get("secret") or ""))
    _META_CACHE.update(key=key, val=val)
    return val


def ctrl_open(method, path, data=None, timeout=5):
    """向 mihomo controller 发请求，返回响应对象（调用方负责关闭）；非 2xx 抛 urllib.error.HTTPError。"""
    base, secret = cfg_meta()
    if not base:
        raise RuntimeError("cannot read config.yaml")
    req = urllib.request.Request(base + path, data=data, method=method)
    if secret:
        req.add_header("Authorization", f"Bearer {secret}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    return _CTRL_OPENER.open(req, timeout=timeout)


def controller(method, path, body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    with ctrl_open(method, path, data, timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def sub_url():
    """订阅地址：环境变量 MIHOMO_SUB_URL > 订阅地址文件（默认 $MIHOMO_DIR/subscription.url）
    > 兼容旧部署：update-subscription.sh 中写死的 SUB_URL='https://...'。"""
    u = os.environ.get("MIHOMO_SUB_URL", "").strip()
    if u:
        return u
    try:
        for line in SUB_URL_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except Exception:
        pass
    try:
        m = re.search(r"^SUB_URL=['\"]?(https?://[^'\"\n]+)", UPDATE_SCRIPT.read_text(), re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""


def script_env():
    """传给 update-subscription.sh 的环境变量，保证脚本与管理页使用同一套路径配置。"""
    env = {"MIHOMO_DIR": str(MIHOMO_DIR), "MIHOMO_BIN": MIHOMO_BIN, "MIHOMO_SERVICE": SERVICE,
           "MERGE_SCRIPT": str(MERGE_SCRIPT), "MIHOMO_SUB_URL_FILE": str(SUB_URL_FILE)}
    u = sub_url()
    if u:
        env["MIHOMO_SUB_URL"] = u
    return env


def mask_url(u):
    return re.sub(r"(https?://[^/]+/)([^/]{3})[^/]*", r"\1\2***", u)


def file_info(p: Path):
    if not p.exists():
        return None
    st = p.stat()
    return {"size": st.st_size, "mtime": int(st.st_mtime),
            "mtime_str": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}


def safe_backup_name(name):
    if not re.fullmatch(r"config\.yaml\.\d{8}-\d{6}", name or ""):
        raise ValueError("bad backup name")
    return BACKUP_DIR / name


def validate_config(path: Path):
    rc, out, err = run([MIHOMO_BIN, "-t", "-d", str(MIHOMO_DIR), "-f", str(path)], timeout=60)
    return rc == 0, (out + err).strip()


def make_backup(src: Path, keep=20):
    BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
    dst = BACKUP_DIR / f"config.yaml.{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(src, dst)
    files = sorted(BACKUP_DIR.glob("config.yaml.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)
    return dst.name


# ---------- API ----------
def api_status():
    rc, out, _ = run(["systemctl", "show", SERVICE, "--no-pager",
                      "-p", "ActiveState,SubState,MainPID,ActiveEnterTimestamp,NRestarts"], timeout=10)
    svc = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
    version = None
    try:
        version = controller("GET", "/version")
    except Exception as ex:
        version = {"error": str(ex)}
    base, secret = cfg_meta()
    return {
        "service": svc,
        "dashboard": {
            "url": DASHBOARD_URL,
            "name": DASHBOARD_NAME,
            "port": base.rsplit(":", 1)[1] if base else "",
            # 只有模板里用到 {secret} 时才下发
            "secret": secret if "{secret}" in DASHBOARD_URL else "",
        } if DASHBOARD_URL else None,
        "version": version,
        "controller": base,
        "config": file_info(CONFIG_FILE),
        "override": file_info(OVERRIDE_FILE),
        "backups": len(list(BACKUP_DIR.glob("config.yaml.*"))) if BACKUP_DIR.exists() else 0,
        "sub_url": mask_url(sub_url()),
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def api_update():
    with LOCK:
        t0 = time.time()
        rc, out, err = run(["bash", str(UPDATE_SCRIPT)], timeout=300, env=script_env())
    return {"ok": rc == 0, "rc": rc, "output": (out + err).strip(), "seconds": round(time.time() - t0, 1)}


def api_override_validate(text):
    """下载订阅 -> 合并 -> mihomo -t，不落盘。"""
    try:
        yaml.safe_load(text)
    except Exception as ex:
        return {"ok": False, "stage": "yaml", "output": str(ex)}
    url = sub_url()
    if not url:
        return {"ok": False, "stage": "sub", "output": f"未配置订阅地址：请设置环境变量 MIHOMO_SUB_URL 或写入 {SUB_URL_FILE}"}
    with tempfile.TemporaryDirectory(dir=str(MIHOMO_DIR)) as td:
        td = Path(td)
        sub, over, merged = td / "sub.yaml", td / "override.yaml", td / "merged.yaml"
        rc, out, err = run(["curl", "-fsSL", "--max-time", "60", url, "-o", str(sub)], timeout=90)
        if rc != 0:
            return {"ok": False, "stage": "download", "output": (out + err).strip()}
        over.write_text(text, encoding="utf-8")
        rc, out, err = run([sys.executable, str(MERGE_SCRIPT), str(sub), str(over), str(merged)], timeout=60)
        if rc != 0:
            return {"ok": False, "stage": "merge", "output": (out + err).strip()}
        ok, msg = validate_config(merged)
        diff = ""
        if CONFIG_FILE.exists():
            diff = "".join(difflib.unified_diff(
                CONFIG_FILE.read_text(encoding="utf-8").splitlines(True),
                merged.read_text(encoding="utf-8").splitlines(True),
                "config.yaml (当前)", "config.yaml (合并后)"))
        return {"ok": ok, "stage": "mihomo -t", "output": msg or ("合并输出 stderr: " + err.strip() if err.strip() else "OK"),
                "diff": diff}


def api_override_save(text, apply):
    with LOCK:
        if OVERRIDE_FILE.exists():
            bak = OVERRIDE_FILE.with_name(f"override.yaml.bak.{int(time.time())}")
            shutil.copy2(OVERRIDE_FILE, bak)
            baks = sorted(MIHOMO_DIR.glob("override.yaml.bak.*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in baks[10:]:
                old.unlink(missing_ok=True)
        tmp = OVERRIDE_FILE.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(OVERRIDE_FILE)
    res = {"ok": True, "saved": True}
    if apply:
        res["apply"] = api_update()
        res["ok"] = res["apply"]["ok"]
    return res


# ---------- rules (form editing of rules-prepend / rules-append) ----------
LOGIC_RULES = {"AND", "OR", "NOT", "SUB-RULE"}
RULE_EXTRA = {"no-resolve", "src"}


def normalize_rule(r):
    s = str(r).strip()
    if not s or "\n" in s:
        raise ValueError(f"非法规则: {r!r}")
    parts = [p.strip() for p in s.split(",")]
    t = parts[0].upper()
    if t in LOGIC_RULES:
        return s
    if t == "MATCH":
        if len(parts) != 2 or not parts[1]:
            raise ValueError(f"MATCH 规则格式应为 MATCH,策略: {s}")
    else:
        if len(parts) < 3 or not parts[1] or not parts[2]:
            raise ValueError(f"规则格式应为 类型,值,策略: {s}")
        for extra in parts[3:]:
            if extra not in RULE_EXTRA:
                raise ValueError(f"未知附加参数 {extra!r}: {s}")
    parts[0] = t
    return ",".join(parts)


def _find_top_block(lines, key):
    """返回顶层 key 所在行区间 [start, end)，不存在返回 (None, None)。
    块从 key 行开始，到下一个列 0 的非注释、非 '-' 行为止；块尾部的空行/列 0 注释不计入（归下一个 key）。"""
    start = None
    for i, l in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*:", l):
            start = i
            break
    if start is None:
        return None, None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^[^\s#-]", lines[j]):
            end = j
            break
    while end > start + 1 and (lines[end - 1].strip() == "" or lines[end - 1].startswith("#")):
        end -= 1
    return start, end


def _render_rules_block(key, rules):
    if not rules:
        return []
    dumped = yaml.safe_dump(list(rules), allow_unicode=True, default_flow_style=False, width=4096)
    return [key + ":\n"] + ["  " + l + "\n" for l in dumped.splitlines()]


def set_rules_block(text, key, rules):
    """只替换 override.yaml 中 key 对应的块，其余内容（注释等）原样保留。"""
    lines = text.splitlines(True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    block = _render_rules_block(key, rules)
    start, end = _find_top_block(lines, key)
    if start is None:
        if block:
            if lines and lines[-1].strip() != "":
                lines.append("\n")
            lines += block
    else:
        lines[start:end] = block
    return "".join(lines)


def _override_text():
    return OVERRIDE_FILE.read_text(encoding="utf-8") if OVERRIDE_FILE.exists() else ""


def _load_mapping(text):
    try:
        d = yaml.safe_load(text) or {}
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def api_rules():
    over = _load_mapping(_override_text())
    cfg = _load_mapping(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    groups = [g.get("name") for g in cfg.get("proxy-groups") or [] if isinstance(g, dict) and g.get("name")]
    return {
        "prepend": [str(r) for r in over.get("rules-prepend") or []],
        "append": [str(r) for r in over.get("rules-append") or []],
        "insert": over.get("rules-insert") or [],
        "policies": ["DIRECT", "REJECT", "REJECT-DROP"] + [str(g) for g in groups],
        "rule_sets": [str(k) for k in (cfg.get("rule-providers") or {}).keys()],
        "current_rules": [str(r) for r in cfg.get("rules") or []],
        "info": file_info(OVERRIDE_FILE),
    }


def api_rules_save(prepend, append, apply):
    prepend = [normalize_rule(r) for r in prepend or []]
    append = [normalize_rule(r) for r in append or []]
    text = _override_text()
    if text.strip() and not isinstance(yaml.safe_load(text), dict):
        raise ValueError("override.yaml 顶层不是映射，请先在 override.yaml 页修复")
    text = set_rules_block(text, "rules-prepend", prepend)
    text = set_rules_block(text, "rules-append", append)
    chk = _load_mapping(text)
    if [str(r) for r in chk.get("rules-prepend") or []] != prepend or \
       [str(r) for r in chk.get("rules-append") or []] != append:
        raise RuntimeError("写回校验失败：生成的 YAML 与表单不一致，未保存")
    return api_override_save(text, apply)


# ---------- kernel update ----------
KERNEL_REPO = "MetaCubeX/mihomo"
KERNEL_DL_PREFIX = f"https://github.com/{KERNEL_REPO}/releases/download/"
KERNEL_KEEP = 3
_KERNEL_CACHE = {"t": 0.0, "data": None}


def _mixed_port():
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        return int(cfg.get("mixed-port") or cfg.get("port") or 0)
    except Exception:
        return 0


def _curl(url, out=None, timeout=60, extra=None):
    """curl 下载；直连失败时自动改走本机 mixed-port 代理再试一次。"""
    base = ["curl", "-fsSL", "--max-time", str(timeout), "-A", "mihomo-manager", url] + (extra or [])
    if out:
        base += ["-o", str(out)]
    rc, o, e = run(base, timeout=timeout + 10)
    if rc != 0:
        port = _mixed_port()
        if port:
            rc2, o2, e2 = run(base + ["-x", f"http://127.0.0.1:{port}"], timeout=timeout + 10)
            if rc2 == 0:
                return rc2, o2, e2 + "\n[via local proxy]"
    return rc, o, e


def _cpu_level():
    """amd64 微架构等级：v3(avx2) / v2(sse4.2) / v1，用于选 mihomo 的 amd64 变体。"""
    try:
        flags = set(re.search(r"^flags\s*:\s*(.*)$", Path("/proc/cpuinfo").read_text(), re.M).group(1).split())
    except Exception:
        return "v1", []
    if {"avx2", "bmi2", "fma", "movbe"} <= flags:
        return "v3", sorted(flags & {"avx", "avx2", "sse4_2", "popcnt", "bmi2", "fma"})
    if {"sse4_2", "popcnt"} <= flags:
        return "v2", sorted(flags & {"sse4_2", "popcnt", "ssse3"})
    return "v1", sorted(flags & {"sse2", "sse3", "pni"})


def _arch():
    m = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
            "armv7l": "armv7", "armv6l": "armv6", "i386": "386", "i686": "386",
            "riscv64": "riscv64", "mips64": "mips64", "s390x": "s390x"}.get(m, m)


def kernel_version(binary=None):
    rc, out, err = run([binary or MIHOMO_BIN, "-v"], timeout=15)
    text = (out + err).strip()
    m = re.search(r"\bv?(\d+\.\d+\.\d+(?:[-\w.]*)?)\b", text)
    alpha = re.search(r"\balpha[- ]?([0-9a-f]{6,})", text, re.I)
    return {"ok": rc == 0, "raw": text, "version": ("alpha-" + alpha.group(1)) if alpha else (m.group(1) if m else None)}


def _kernel_backups():
    b = Path(MIHOMO_BIN)
    items = []
    for p in sorted(b.parent.glob(b.name + ".bak.*"), key=lambda p: p.stat().st_mtime, reverse=True):
        items.append({"name": p.name, "version": p.name.split(".bak.", 1)[1], **file_info(p)})
    return items


def _parse_asset(name, arch, tag):
    """解析资产名 mihomo-linux-<arch>[-variant]-<tag>.gz，返回 variant（"" 表示默认），不匹配返回 None。"""
    prefix = f"mihomo-linux-{arch}"
    if not name.startswith(prefix) or not name.endswith(".gz"):
        return None
    rest = name[len(prefix):-3]  # e.g. "-compatible-v1.19.33" / "-v1.19.33" / "-alpha-91e85ee"
    if tag == "Prerelease-Alpha":
        m = re.fullmatch(r"(?:-(.*?))?-alpha-[0-9a-f]+", rest)
    else:
        m = re.fullmatch(r"(?:-(.*?))?-" + re.escape(tag), rest)
    if not m:
        return None
    return m.group(1) or ""


def _recommend_variant(variants, level):
    """按 CPU 等级推荐变体；非 amd64 直接用默认。"""
    if _arch() != "amd64":
        return "" if "" in variants else (variants[0] if variants else "")
    order = {"v3": ["", "v3", "v2", "compatible", "v1"],
             "v2": ["v2", "compatible", "v1"],
             "v1": ["compatible", "v1"]}[level]
    for v in order:
        if v in variants:
            return v
    return variants[0] if variants else ""


def _fetch_releases():
    rc, out, err = _curl(f"https://api.github.com/repos/{KERNEL_REPO}/releases?per_page=12", timeout=30,
                         extra=["-H", "Accept: application/vnd.github+json"])
    if rc != 0:
        raise RuntimeError("GitHub API 请求失败: " + (err or out).strip()[-300:])
    data = json.loads(out)
    arch = _arch()
    rels = []
    for r in data:
        tag = r.get("tag_name") or ""
        assets = []
        for a in r.get("assets") or []:
            v = _parse_asset(a["name"], arch, tag)
            if v is None or "go1" in v:  # go120/go123 
                continue
            assets.append({"name": a["name"], "variant": v, "size": a["size"], "url": a["browser_download_url"],
                           "updated": a.get("updated_at")})
        if not assets:
            continue
        assets.sort(key=lambda a: (a["variant"] != "", a["variant"]))
        rels.append({"tag": tag, "name": r.get("name") or tag, "prerelease": bool(r.get("prerelease")),
                     "published": (max(a["updated"] or "" for a in assets) if tag == "Prerelease-Alpha" else r.get("published_at")),
                     "url": r.get("html_url"), "body": (r.get("body") or "")[:6000], "assets": assets})
    return rels


def api_kernel(refresh=False):
    now = time.time()
    err = None
    if refresh or _KERNEL_CACHE["data"] is None or now - _KERNEL_CACHE["t"] > 600:
        try:
            _KERNEL_CACHE["data"] = _fetch_releases()
            _KERNEL_CACHE["t"] = now
        except Exception as ex:
            err = str(ex)
    rels = _KERNEL_CACHE["data"] or []
    level, flags = _cpu_level()
    variants = sorted({a["variant"] for r in rels for a in r["assets"]})
    cur = kernel_version()
    stable = next((r for r in rels if not r["prerelease"]), None)
    return {
        "current": {**cur, "binary": MIHOMO_BIN, "info": file_info(Path(MIHOMO_BIN)), "arch": _arch(),
                    "machine": platform.machine(), "cpu_level": level, "cpu_flags": flags},
        "recommended_variant": _recommend_variant(variants, level),
        "latest_stable": stable["tag"] if stable else None,
        "releases": rels, "fetched": _KERNEL_CACHE["t"], "error": err,
        "backups": _kernel_backups(),
    }


def _install_binary(src: Path, label: str):
    """把 src 二进制安装为 MIHOMO_BIN：校验 -v / -t → 备份旧内核 → 替换 → 重启；失败自动回滚。"""
    log = []
    binp = Path(MIHOMO_BIN)
    os.chmod(src, 0o755)
    v = kernel_version(str(src))
    if not v["ok"] or not v["version"]:
        return {"ok": False, "output": "\n".join(log + ["新内核无法运行: " + v["raw"]])}
    log.append(f"新内核: {v['raw'].splitlines()[0]}")
    rc, out, err = run([str(src), "-t", "-d", str(MIHOMO_DIR), "-f", str(CONFIG_FILE)], timeout=60)
    if rc != 0:
        return {"ok": False, "output": "\n".join(log + ["新内核校验当前 config.yaml 失败:", (out + err).strip()])}
    log.append("新内核校验 config.yaml 通过")
    old = kernel_version()
    bak = None
    if binp.exists():
        bak = binp.with_name(f"{binp.name}.bak.{old['version'] or int(time.time())}")
        shutil.copy2(binp, bak)
        log.append(f"旧内核已备份: {bak.name}")
        for p in _kernel_backups()[KERNEL_KEEP:]:
            Path(binp.parent / p["name"]).unlink(missing_ok=True)
            log.append(f"清理旧备份: {p['name']}")
    os.chmod(src, 0o755)
    os.replace(src, binp)
    log.append(f" {binp} \u2190 {label}")
    rc, out, err = run(["systemctl", "restart", SERVICE], timeout=60)
    time.sleep(1.5)
    active = run(["systemctl", "is-active", SERVICE], timeout=10)[1].strip()
    log.append(f"服务重启后状态: {active}")
    if active != "active" and bak:
        shutil.copy2(bak, binp)
        os.chmod(binp, 0o755)
        run(["systemctl", "restart", SERVICE], timeout=60)
        time.sleep(1.0)
        active2 = run(["systemctl", "is-active", SERVICE], timeout=10)[1].strip()
        tail = run(["journalctl", "-u", SERVICE, "-n", "15", "--no-pager", "-o", "cat"], timeout=15)[1]
        log.append(f"\u26a0 \u5df2\u81ea\u52a8\u56de\u6eda\u5230 {bak.name}\uff0c\u670d\u52a1: {active2}\n--- journal ---\n{tail.strip()}")
        return {"ok": False, "rolled_back": True, "output": "\n".join(log)}
    return {"ok": active == "active", "version": kernel_version(), "output": "\n".join(log)}


def api_kernel_update(url, name):
    if not isinstance(url, str) or not url.startswith(KERNEL_DL_PREFIX) or not url.endswith(".gz"):
        raise ValueError("只允许安装 GitHub Release 的 .gz 资产")
    with LOCK:
        t0 = time.time()
        binp = Path(MIHOMO_BIN)
        with tempfile.TemporaryDirectory(dir=str(binp.parent), prefix=".mihomo-dl-") as td:
            gz = Path(td) / "mihomo.gz"
            rc, out, err = _curl(url, out=gz, timeout=300)
            if rc != 0:
                return {"ok": False, "output": "下载失败:\n" + (err or out).strip()}
            size = gz.stat().st_size
            raw = Path(td) / "mihomo"
            try:
                with gzip.open(gz, "rb") as f, open(raw, "wb") as o:
                    shutil.copyfileobj(f, o)
            except Exception as ex:
                return {"ok": False, "output": f"解压失败: {ex}"}
            res = _install_binary(raw, name or url.rsplit("/", 1)[-1])
        res["output"] = f"已下载 {name or url} ({size // 1024} KB, {time.time() - t0:.1f}s)\n" + res["output"]
        res["seconds"] = round(time.time() - t0, 1)
        return res


def api_kernel_rollback(name):
    binp = Path(MIHOMO_BIN)
    if not re.fullmatch(re.escape(binp.name) + r"\.bak\.[\w.\-]+", name or ""):
        raise ValueError("bad backup name")
    src = binp.parent / name
    if not src.exists():
        raise ValueError("备份文件不存在")
    with LOCK:
        with tempfile.TemporaryDirectory(dir=str(binp.parent), prefix=".mihomo-rb-") as td:
            tmp = Path(td) / "mihomo"
            shutil.copy2(src, tmp)
            res = _install_binary(tmp, name)
        return res


def api_kernel_backup_delete(name):
    binp = Path(MIHOMO_BIN)
    if not re.fullmatch(re.escape(binp.name) + r"\.bak\.[\w.\-]+", name or ""):
        raise ValueError("bad backup name")
    (binp.parent / name).unlink(missing_ok=True)
    return {"ok": True}


# ---------- geo databases (geoip.metadb / ASN.mmdb) ----------
GEO_REPO = "MetaCubeX/meta-rules-dat"
GEO_DL = f"https://github.com/{GEO_REPO}/releases/download/latest/"
GEO_FILES = [
    {"file": "geoip.metadb", "key": "mmdb", "asset": "geoip.metadb", "desc": "GEOIP 规则 / fake-ip 过滤 / DNS 地理判断"},
    {"file": "ASN.mmdb", "key": "asn", "asset": "GeoLite2-ASN.mmdb", "desc": "IP-ASN 规则"},
    {"file": "geosite.dat", "key": "geosite", "asset": "geosite.dat", "desc": "GEOSITE 规则（geodata 模式）", "optional": True},
    {"file": "geoip.dat", "key": "geoip", "asset": "geoip.dat", "desc": "GEOIP 规则（geodata-mode: true 时）", "optional": True},
]
_GEO_CACHE = {"t": 0.0, "data": None}
MMDB_MARK = b"\xab\xcd\xefMaxMind.com"


def _mmdb_decode(buf, pos):
    ctrl = buf[pos]; pos += 1
    typ = ctrl >> 5
    if typ == 0:
        typ = buf[pos] + 7; pos += 1
    if typ == 1:
        raise ValueError("pointer in metadata")
    size = ctrl & 0x1F
    if size == 29:
        size = 29 + buf[pos]; pos += 1
    elif size == 30:
        size = 285 + int.from_bytes(buf[pos:pos + 2], "big"); pos += 2
    elif size == 31:
        size = 65821 + int.from_bytes(buf[pos:pos + 3], "big"); pos += 3
    if typ == 2:
        return buf[pos:pos + size].decode("utf-8", "replace"), pos + size
    if typ == 3:
        return struct.unpack(">d", buf[pos:pos + 8])[0], pos + 8
    if typ == 4:
        return bytes(buf[pos:pos + size]), pos + size
    if typ in (5, 6, 9, 10):
        return int.from_bytes(buf[pos:pos + size], "big"), pos + size
    if typ == 8:
        return int.from_bytes(buf[pos:pos + size], "big", signed=True), pos + size
    if typ == 7:
        d = {}
        for _ in range(size):
            k, pos = _mmdb_decode(buf, pos)
            v, pos = _mmdb_decode(buf, pos)
            d[k] = v
        return d, pos
    if typ == 11:
        a = []
        for _ in range(size):
            v, pos = _mmdb_decode(buf, pos)
            a.append(v)
        return a, pos
    if typ == 14:
        return bool(size), pos
    if typ == 15:
        return struct.unpack(">f", buf[pos:pos + 4])[0], pos + 4
    raise ValueError(f"unknown mmdb type {typ}")


def mmdb_meta(path: Path):
    """读取 MMDB 文件尾部 metadata：数据库类型、构建时间、节点数。非 mmdb 返回 None。"""
    with open(path, "rb") as f:
        f.seek(0, 2)
        n = f.tell()
        f.seek(max(0, n - 128 * 1024))
        tail = f.read()
    i = tail.rfind(MMDB_MARK)
    if i < 0:
        return None
    m, _ = _mmdb_decode(tail, i + len(MMDB_MARK))
    be = m.get("build_epoch")
    return {"type": m.get("database_type"), "build": be,
            "build_str": datetime.fromtimestamp(be).strftime("%Y-%m-%d %H:%M") if be else None,
            "nodes": m.get("node_count"), "ip_version": m.get("ip_version"),
            "desc": (m.get("description") or {}).get("en")}


def _geox_urls():
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        g = cfg.get("geox-url") or {}
        return g if isinstance(g, dict) else {}
    except Exception:
        return {}


def _geo_url(spec):
    u = _geox_urls().get(spec["key"])
    return str(u) if u else GEO_DL + spec["asset"]


def _fetch_geo_remote():
    rc, out, err = _curl(f"https://api.github.com/repos/{GEO_REPO}/releases/tags/latest", timeout=30,
                         extra=["-H", "Accept: application/vnd.github+json"])
    if rc != 0:
        raise RuntimeError("GitHub API 请求失败: " + (err or out).strip()[-300:])
    r = json.loads(out)
    return {a["name"]: {"size": a["size"], "updated": a.get("updated_at")} for a in r.get("assets") or []}


def _geo_validate(spec, path: Path):
    """下载后的文件校验：mmdb 能解析出 metadata；.dat 只检查非空且像 protobuf。"""
    if path.stat().st_size < 1024:
        raise ValueError("文件过小，下载可能不完整")
    if spec["file"].endswith((".mmdb", ".metadb")):
        meta = mmdb_meta(path)
        if not meta or not meta.get("type"):
            raise ValueError("不是有效的 MMDB 文件")
        return meta
    with open(path, "rb") as f:
        head = f.read(2)
    if not head or head[0] != 0x0A:
        raise ValueError("不是有效的 geodata .dat 文件")
    return None


def api_geo(refresh=False):
    now = time.time()
    err = None
    if refresh or _GEO_CACHE["data"] is None or now - _GEO_CACHE["t"] > 600:
        try:
            _GEO_CACHE["data"] = _fetch_geo_remote()
            _GEO_CACHE["t"] = now
        except Exception as ex:
            err = str(ex)
    remote = _GEO_CACHE["data"] or {}
    files = []
    for spec in GEO_FILES:
        p = MIHOMO_DIR / spec["file"]
        exists = p.exists()
        if not exists and spec.get("optional"):
            continue
        meta = None
        if exists:
            try:
                meta = mmdb_meta(p) if spec["file"].endswith((".mmdb", ".metadb")) else None
            except Exception as ex:
                meta = {"error": str(ex)}
        url = _geo_url(spec)
        custom = not url.startswith(GEO_DL)
        files.append({**spec, "exists": exists, "info": file_info(p), "meta": meta, "url": url, "custom_url": custom,
                      "remote": None if custom else remote.get(spec["asset"]),
                      "bak": file_info(p.with_name(p.name + ".bak"))})
    return {"files": files, "fetched": _GEO_CACHE["t"], "error": err, "repo": GEO_REPO}


def api_geo_update(names, restart=True):
    specs = [x for x in GEO_FILES if x["file"] in (names or [])]
    if not specs:
        raise ValueError("未选择要更新的数据库")
    log, ok_files = [], []
    t0 = time.time()
    with LOCK:
        for spec in specs:
            dst = MIHOMO_DIR / spec["file"]
            url = _geo_url(spec)
            with tempfile.TemporaryDirectory(dir=str(MIHOMO_DIR), prefix=".geo-dl-") as td:
                tmp = Path(td) / spec["file"]
                rc, out, err = _curl(url, out=tmp, timeout=300)
                if rc != 0:
                    log.append(f"✗ {spec['file']}: 下载失败 {url}\n  {(err or out).strip()[-300:]}")
                    continue
                try:
                    meta = _geo_validate(spec, tmp)
                except Exception as ex:
                    log.append(f"✗ {spec['file']}: 校验失败 — {ex}")
                    continue
                old = mmdb_meta(dst) if dst.exists() and meta else None
                if dst.exists():
                    shutil.copy2(dst, dst.with_name(dst.name + ".bak"))
                os.chmod(tmp, 0o644)
                os.replace(tmp, dst)
                size = dst.stat().st_size
                desc = f"{size // 1024} KB"
                if meta:
                    desc += f" · {meta['type']} · 构建 {meta['build_str']}"
                    if old and old.get("build_str"):
                        desc += f"（原 {old['build_str']}）"
                log.append(f"✓ {spec['file']}: {desc}")
                ok_files.append(spec["file"])
        active = None
        if ok_files and restart:
            rc, out, err = run(["systemctl", "restart", SERVICE], timeout=60)
            time.sleep(1.0)
            active = run(["systemctl", "is-active", SERVICE], timeout=10)[1].strip()
            log.append(f"服务已重启: {active}" if rc == 0 else f"重启失败: {err.strip()}")
        elif ok_files:
            log.append("未重启服务：新数据库将在下次重启 mihomo 后生效")
    ok = len(ok_files) == len(specs) and (active in (None, "active"))
    return {"ok": ok, "updated": ok_files, "seconds": round(time.time() - t0, 1),
            "output": f"更新 {len(ok_files)}/{len(specs)} 个数据库，耗时 {time.time() - t0:.1f}s\n" + "\n".join(log)}


def api_geo_rollback(name):
    spec = next((x for x in GEO_FILES if x["file"] == name), None)
    if not spec:
        raise ValueError("bad name")
    dst = MIHOMO_DIR / name
    bak = dst.with_name(name + ".bak")
    if not bak.exists():
        raise ValueError("没有备份文件")
    with LOCK:
        _geo_validate(spec, bak)
        tmp = dst.with_name(name + ".tmp")
        shutil.copy2(bak, tmp)
        os.replace(tmp, dst)
        rc, out, err = run(["systemctl", "restart", SERVICE], timeout=60)
        time.sleep(1.0)
        active = run(["systemctl", "is-active", SERVICE], timeout=10)[1].strip()
    return {"ok": rc == 0 and active == "active", "output": f"已恢复 {name} ← {bak.name}；服务: {active}"}


def api_backups():
    if not BACKUP_DIR.exists():
        return []
    items = []
    for p in sorted(BACKUP_DIR.glob("config.yaml.*"), key=lambda p: p.name, reverse=True):
        items.append({"name": p.name, **file_info(p)})
    return items


def api_backup_diff(name):
    p = safe_backup_name(name)
    a = p.read_text(encoding="utf-8").splitlines(True)
    b = CONFIG_FILE.read_text(encoding="utf-8").splitlines(True) if CONFIG_FILE.exists() else []
    return {"diff": "".join(difflib.unified_diff(a, b, name, "config.yaml (当前)"))}


def api_backup_restore(name):
    p = safe_backup_name(name)
    with LOCK:
        ok, msg = validate_config(p)
        if not ok:
            return {"ok": False, "output": "备份校验失败:\n" + msg}
        made = make_backup(CONFIG_FILE) if CONFIG_FILE.exists() else None
        shutil.copy2(p, CONFIG_FILE)
        os.chmod(CONFIG_FILE, 0o600)
        rc, out, err = run(["systemctl", "restart", SERVICE], timeout=60)
        active = run(["systemctl", "is-active", SERVICE], timeout=10)[1].strip()
    return {"ok": rc == 0 and active == "active", "backup_of_current": made,
            "output": f"restored {name}; service: {active}\n{out}{err}".strip()}


def api_backup_delete(names):
    """删除一个或多个备份文件（名字经 safe_backup_name 校验，只能删 backup/ 下的 config.yaml.*）。"""
    if isinstance(names, str):
        names = [names]
    deleted, errors = [], []
    with LOCK:
        for n in names or []:
            try:
                p = safe_backup_name(n)
                if p.exists():
                    p.unlink()
                    deleted.append(n)
                else:
                    errors.append(f"{n}: 文件不存在")
            except Exception as ex:
                errors.append(f"{n}: {ex}")
    return {"ok": not errors, "deleted": deleted, "errors": errors,
            "output": "\n".join([f"已删除 {len(deleted)} 个备份"] + errors)}


def api_service(action):
    if action == "reload":
        try:
            controller("PUT", "/configs?force=true", {"path": str(CONFIG_FILE)}, timeout=60)
            return {"ok": True, "output": "controller reload OK"}
        except Exception as ex:
            return {"ok": False, "output": f"reload failed: {ex}"}
    if action in ("restart", "start", "stop"):
        with LOCK:
            rc, out, err = run(["systemctl", action, SERVICE], timeout=60)
        active = run(["systemctl", "is-active", SERVICE], timeout=10)[1].strip()
        return {"ok": rc == 0, "output": f"systemctl {action}: rc={rc} state={active}\n{out}{err}".strip()}
    if action == "validate":
        ok, msg = validate_config(CONFIG_FILE)
        return {"ok": ok, "output": msg or "OK"}
    raise ValueError("unknown action")


def api_logs(n):
    n = max(10, min(int(n or 200), 2000))
    rc, out, err = run(["journalctl", "-u", SERVICE, "-n", str(n), "--no-pager", "-o", "short-iso"], timeout=20)
    return {"output": (out + err).strip()}


def api_proxies():
    data = controller("GET", "/proxies")
    groups = []
    for name, p in data.get("proxies", {}).items():
        if p.get("type") in ("Selector", "URLTest", "Fallback", "LoadBalance") and name != "GLOBAL":
            groups.append({"name": name, "type": p["type"], "now": p.get("now"), "all": p.get("all", [])})
    groups.sort(key=lambda g: g["name"])
    return {"groups": groups}


# ---------- 内置面板：mihomo controller 反向代理 ----------
# 浏览器只和管理页通信（沿用管理页认证），由后端带上 controller secret 转发，
# 所以 secret 不会下发给浏览器，controller 也可以只监听 127.0.0.1。
CTRL_PREFIX = "/api/mihomo"
CTRL_ALLOWED = re.compile(
    r"/(?:version|configs|proxies|group|providers|rules|connections|traffic|memory|logs|cache|dns)(?:/[^?#]*)?")
CTRL_STREAMS = {"/traffic", "/memory", "/logs"}  # controller 以 NDJSON 持续推送的接口
CTRL_MAX_BODY = 1 << 20
CTRL_STREAM_IDLE = 60  # 流式接口上游空闲超时（秒）；超时后结束本次响应，前端自动重连


def ctrl_path_ok(path):
    return bool(CTRL_ALLOWED.fullmatch(path)) and \
        not re.search(r"(^|/)\.{1,2}(/|$)", urllib.parse.unquote(path))


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "mihomo-manager/1.0"
    PASSWORD = load_password()
    TOKEN = load_token()

    def log_message(self, fmt, *args):
        # Hide whole query strings, including percent-encoded token parameter names.
        message = re.sub(r"\?[^\s\"]*", "?[REDACTED]", fmt % args)
        sys.stderr.write("%s - %s\n" % (self.address_string(), message))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _valid_token(self, token):
        return bool(self.TOKEN) and isinstance(token, str) and \
            hmac.compare_digest(token.encode(), self.TOKEN.encode())

    def _valid_session(self):
        if not self.TOKEN:
            return False
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            value = cookies[SESSION_COOKIE].value
            expires, nonce, signature = value.split(".")
            expected = hmac.new(self.TOKEN.encode(), f"{expires}.{nonce}".encode(), "sha256").hexdigest()
            return int(expires) > time.time() and hmac.compare_digest(signature.encode(), expected.encode())
        except (CookieError, KeyError, ValueError):
            return False

    def _session_cookie(self, clear=False):
        cookies = SimpleCookie()
        if clear:
            value = ""
        else:
            payload = f"{int(time.time()) + SESSION_MAX_AGE}.{secrets.token_urlsafe(18)}"
            signature = hmac.new(self.TOKEN.encode(), payload.encode(), "sha256").hexdigest()
            value = f"{payload}.{signature}"
        cookies[SESSION_COOKIE] = value
        cookie = cookies[SESSION_COOKIE]
        cookie["path"] = "/"
        cookie["httponly"] = True
        cookie["samesite"] = "Strict"
        cookie["max-age"] = 0 if clear else SESSION_MAX_AGE
        if isinstance(self.connection, ssl.SSLSocket):
            cookie["secure"] = True
        return cookie.OutputString()

    def _login(self, token, redirect=False):
        if not self._valid_token(token):
            self._json({"ok": False, "error": "invalid token"}, 401)
            return
        cookie = self._session_cookie()
        if redirect:
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", cookie)
            self.end_headers()
        else:
            self._json({"ok": True}, cookie=cookie)

    def _auth(self):
        if not self.PASSWORD and not self.TOKEN:
            return True
        h = self.headers.get("Authorization", "")
        scheme, _, credentials = h.partition(" ")
        if scheme.lower() == "bearer" and self._valid_token(credentials):
            return True
        if self._valid_session():
            return True
        if self.PASSWORD and scheme.lower() == "basic":
            try:
                user, _, pw = base64.b64decode(credentials).decode().partition(":")
            except Exception:
                user, pw = "", ""
            if hmac.compare_digest(user.encode(), AUTH_USER.encode()) and \
                    hmac.compare_digest(pw.encode(), self.PASSWORD.encode()):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="mihomo-manager"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"auth required")
        return False

    def _json(self, obj, code=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, code, body, ctype="application/json"):
        self.send_response(code)
        if code in (204, 304):
            self.end_headers()
            return
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("X-Accel-Buffering", "no")  # 让 Nginx 等反向代理不要缓冲
        self.end_headers()
        self.close_connection = True

    def _ctrl_proxy(self, method):
        """/api/mihomo/<controller 路径> → mihomo external-controller。"""
        u = urlparse(self.path)
        path, query = u.path[len(CTRL_PREFIX):], u.query
        if not ctrl_path_ok(path):
            self._json({"message": "path not allowed"}, 403)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > CTRL_MAX_BODY:
            self._json({"message": "invalid request body size"}, 400)
            return
        data = self.rfile.read(n) if n else None
        stream = method == "GET" and path in CTRL_STREAMS
        target = path + ("?" + query if query else "")
        try:
            resp = ctrl_open(method, target, data, timeout=CTRL_STREAM_IDLE if stream else 60)
        except urllib.error.HTTPError as ex:
            body = ex.read()
            self._raw(ex.code, body, ex.headers.get("Content-Type") or "application/json")
            return
        except Exception as ex:
            self._json({"message": f"controller unreachable: {ex}"}, 502)
            return
        with resp:
            if not stream:
                self._raw(resp.status, resp.read(), resp.headers.get("Content-Type") or "application/json")
                return
            self._stream_headers()
            try:
                for line in resp:
                    self.wfile.write(line)
            except (OSError, ValueError):  # 浏览器断开 / 上游空闲超时
                pass

    def _overview_stream(self):
        """合并 traffic / memory / 连接数为一条 NDJSON 流，节省浏览器的并发连接数。
        每行形如 {"traffic":{...}} / {"memory":{...}} / {"conns":{...}} / {"error":"..."}。"""
        q = queue.Queue(maxsize=256)
        stop = threading.Event()

        def put(item):
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

        def pump(topic):
            try:
                with ctrl_open("GET", "/" + topic, timeout=30) as r:
                    for line in r:
                        if stop.is_set():
                            return
                        line = line.strip()
                        if line:
                            put((topic, line))
            except Exception as ex:
                put(("error", json.dumps(f"{topic}: {ex}", ensure_ascii=False).encode()))
            finally:
                put(None)  # 任一上游结束则结束整个流，由前端重连

        def conns():
            while not stop.is_set():
                try:
                    d = controller("GET", "/connections", timeout=10)
                    put(("conns", json.dumps({"count": len(d.get("connections") or []),
                                              "uploadTotal": d.get("uploadTotal") or 0,
                                              "downloadTotal": d.get("downloadTotal") or 0}).encode()))
                except Exception:
                    pass
                stop.wait(2)

        for target, args in ((pump, ("traffic",)), (pump, ("memory",)), (conns, ())):
            threading.Thread(target=target, args=args, daemon=True).start()
        self._stream_headers()
        try:
            while True:
                try:
                    item = q.get(timeout=30)
                except queue.Empty:
                    break
                if item is None:
                    break
                topic, payload = item
                self.wfile.write(b'{"' + topic.encode() + b'":' + payload + b"}\n")
        except OSError:
            pass
        finally:
            stop.set()

    def _other(self, method):
        if not self._auth():
            return
        if urlparse(self.path).path.startswith(CTRL_PREFIX + "/"):
            self._ctrl_proxy(method)
        else:
            self._json({"error": "not found"}, 404)

    def do_PUT(self):
        self._other("PUT")

    def do_PATCH(self):
        self._other("PATCH")

    def do_DELETE(self):
        self._other("DELETE")

    def _body(self, max_bytes=None):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or (max_bytes is not None and n > max_bytes):
            raise ValueError("invalid request body size")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query, keep_blank_values=True)
        if u.path == "/login":
            body = LOGIN_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/" and "token" in q:
            tokens = q.get("token", [])
            self._login(tokens[0] if len(tokens) == 1 else None, redirect=True)
            return
        if not self._auth():
            return
        if u.path.startswith(CTRL_PREFIX + "/"):
            self._ctrl_proxy("GET")
            return
        if u.path == "/api/stream/overview":
            self._overview_stream()
            return
        try:
            if u.path == "/":
                body = HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/status":
                self._json(api_status())
            elif u.path == "/api/override":
                self._json({"content": OVERRIDE_FILE.read_text(encoding="utf-8") if OVERRIDE_FILE.exists() else "",
                            "info": file_info(OVERRIDE_FILE)})
            elif u.path == "/api/config":
                self._json({"content": CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else "",
                            "info": file_info(CONFIG_FILE)})
            elif u.path == "/api/rules":
                self._json(api_rules())
            elif u.path == "/api/kernel":
                self._json(api_kernel(refresh=q.get("refresh", ["0"])[0] == "1"))
            elif u.path == "/api/geo":
                self._json(api_geo(refresh=q.get("refresh", ["0"])[0] == "1"))
            elif u.path == "/api/backups":
                self._json(api_backups())
            elif u.path.startswith("/api/backups/") and u.path.endswith("/diff"):
                self._json(api_backup_diff(u.path.split("/")[3]))
            elif u.path == "/api/logs":
                self._json(api_logs(q.get("n", ["200"])[0]))
            elif u.path == "/api/proxies":
                self._json(api_proxies())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as ex:
            self._json({"ok": False, "error": f"{type(ex).__name__}: {ex}"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/auth/login":
            try:
                data = self._body(max_bytes=4096)
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
            except (ValueError, UnicodeError):
                self._json({"ok": False, "error": "invalid login request"}, 400)
                return
            self._login(data.get("token"))
            return
        if not self._auth():
            return
        if u.path.startswith(CTRL_PREFIX + "/"):
            self._ctrl_proxy("POST")
            return
        try:
            data = self._body()
            if u.path == "/api/auth/logout":
                self._json({"ok": True}, cookie=self._session_cookie(clear=True))
            elif u.path == "/api/update":
                self._json(api_update())
            elif u.path == "/api/override/validate":
                self._json(api_override_validate(data.get("content", "")))
            elif u.path == "/api/override/save":
                self._json(api_override_save(data.get("content", ""), bool(data.get("apply"))))
            elif u.path == "/api/rules/save":
                self._json(api_rules_save(data.get("prepend"), data.get("append"), bool(data.get("apply"))))
            elif u.path == "/api/kernel/update":
                self._json(api_kernel_update(data.get("url"), data.get("name")))
            elif u.path == "/api/kernel/rollback":
                self._json(api_kernel_rollback(data.get("name")))
            elif u.path == "/api/geo/update":
                self._json(api_geo_update(data.get("names"), bool(data.get("restart", True))))
            elif u.path == "/api/geo/rollback":
                self._json(api_geo_rollback(data.get("name")))
            elif u.path == "/api/kernel/backup/delete":
                self._json(api_kernel_backup_delete(data.get("name")))
            elif u.path == "/api/backups/delete":
                self._json(api_backup_delete(data.get("names")))
            elif u.path.startswith("/api/backups/") and u.path.endswith("/restore"):
                self._json(api_backup_restore(u.path.split("/")[3]))
            elif u.path.startswith("/api/service/"):
                self._json(api_service(u.path.split("/")[3]))
            elif u.path == "/api/proxies/select":
                controller("PUT", "/proxies/" + urllib.parse.quote(data["group"]), {"name": data["name"]})
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as ex:
            self._json({"ok": False, "error": f"{type(ex).__name__}: {ex}"}, 500)


LOGIN_HTML = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>mihomo 管理 · 登录</title></head>
<body><p id="message">正在登录…</p><noscript>自动登录需要启用 JavaScript。</noscript>
<script>
(async()=>{
 const token=new URLSearchParams(location.hash.slice(1)).get('token');
 history.replaceState(null,'',location.pathname);
 try{
  if(!token)throw new Error('登录链接缺少 Token。');
  const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});
  if(!r.ok)throw new Error('登录链接无效，请检查 Token。');
  location.replace('/');
 }catch(e){document.getElementById('message').textContent=e.message}
})();
</script></body></html>
"""


HTML = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#11141b"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>mihomo 管理</title><link rel="icon" href="data:,">
<style>
:root{--bg:#0d1017;--card:#161a22;--card2:#1b2029;--ink:#0b0d11;--bd:#262c37;--bd2:#333b48;--fg:#e8eaf0;--mut:#8a91a0;--ok:#3fb950;--bad:#f85149;--acc:#58a6ff;--warn:#d29922;--pur:#a371f7;--cyan:#39c5cf;--pink:#f778ba;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--r:10px}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
code{font-family:var(--mono);font-size:12px;background:#21262d;padding:1px 5px;border-radius:4px}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.sp{flex:1}.ok{color:var(--ok)}.bad{color:var(--bad)}.mut{color:var(--mut)}.warn{color:var(--warn)}.acc{color:var(--acc)}
.mono{font-family:var(--mono)}
/* top */
.top{position:sticky;top:0;z-index:5;background:rgba(17,20,27,.92);backdrop-filter:saturate(1.4) blur(10px);-webkit-backdrop-filter:saturate(1.4) blur(10px);border-bottom:1px solid var(--bd)}
header{display:flex;align-items:center;gap:14px;padding:12px 22px 8px;max-width:1200px;margin:0 auto}
.logo{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,var(--acc),var(--pur));display:grid;place-items:center;font-weight:800;font-size:14px;color:#fff;flex:none;box-shadow:0 2px 10px rgba(88,166,255,.3)}
header h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}
.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--bd);border-radius:20px;padding:3px 10px 3px 8px;font-size:12px;color:var(--mut);background:var(--card)}
.pill .dot{margin:0}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--mut);flex:none}
.dot.ok{background:var(--ok);box-shadow:0 0 0 3px rgba(63,185,80,.18)}.dot.bad{background:var(--bad);box-shadow:0 0 0 3px rgba(248,81,73,.18)}.dot.acc{background:var(--acc)}.dot.warn{background:var(--warn)}
.dot.live{animation:pulse 1.6s ease-in-out infinite}@keyframes pulse{50%{box-shadow:0 0 0 6px rgba(63,185,80,0)}}
header a.ext{font-size:12px;border:1px solid var(--bd);border-radius:6px;padding:4px 10px;color:var(--fg);background:var(--card)}header a.ext:hover{border-color:var(--bd2);text-decoration:none}
nav{display:flex;gap:2px;padding:0 22px;max-width:1200px;margin:0 auto;overflow-x:auto;scrollbar-width:none}nav::-webkit-scrollbar{display:none}
nav button{background:none;border:none;color:var(--mut);padding:8px 12px 10px;cursor:pointer;border-bottom:2px solid transparent;font-size:13.5px;white-space:nowrap;font-family:inherit;border-radius:6px 6px 0 0;transition:color .12s}
nav button:hover{color:var(--fg);background:rgba(255,255,255,.03)}nav button.on{color:var(--fg);border-color:var(--acc);font-weight:600}
nav button small{color:var(--mut);font-weight:400;font-size:11px;margin-left:4px}nav button small.dotn{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--warn);vertical-align:3px;margin-left:5px}
.ng{display:flex;align-items:center}.ng+.ng{border-left:1px solid var(--bd);margin-left:8px;padding-left:8px}
.gl{font-size:10.5px;color:var(--mut);letter-spacing:.8px;margin:0 4px 0 2px;opacity:.75;white-space:nowrap}
main{padding:16px 22px 40px;max-width:1200px;margin:0 auto}.tab{display:none}.tab.on{display:block;animation:fade .15s ease}@keyframes fade{from{opacity:0;transform:translateY(3px)}}
/* cards */
.card{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:16px 18px;margin-bottom:14px}
.card-h{display:flex;align-items:center;gap:10px;margin:-2px 0 12px}.card-h h2{font-size:14px;margin:0;font-weight:650}.card-h .d{font-size:12px;color:var(--mut)}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;position:sticky;z-index:1;background:var(--card);padding:6px 0 10px;margin-bottom:6px;border-bottom:1px solid var(--bd)}
.toolbar .info{font-size:12px;color:var(--mut)}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid var(--bd);color:var(--mut);white-space:nowrap;line-height:16px}
.badge.dirty{color:var(--warn);border-color:var(--warn);background:rgba(210,153,34,.1)}.badge.ok{color:var(--ok);border-color:rgba(63,185,80,.5)}.badge.bad{color:var(--bad);border-color:rgba(248,81,73,.5)}
.tag{font-size:11px;padding:1px 7px;border-radius:5px;background:#21262d;color:var(--mut);border:1px solid var(--bd);white-space:nowrap}
.tag.acc{color:var(--acc);border-color:rgba(88,166,255,.35);background:rgba(88,166,255,.08)}.tag.cyan{color:var(--cyan);border-color:rgba(57,197,207,.35);background:rgba(57,197,207,.08)}
.tag.warn{color:var(--warn);border-color:rgba(210,153,34,.35);background:rgba(210,153,34,.08)}.tag.pur{color:var(--pur);border-color:rgba(163,113,247,.35);background:rgba(163,113,247,.08)}
.tag.ok{color:var(--ok);border-color:rgba(63,185,80,.35);background:rgba(63,185,80,.08)}.tag.bad{color:var(--bad);border-color:rgba(248,81,73,.35);background:rgba(248,81,73,.08)}
/* buttons */
button.btn{background:#21262d;border:1px solid #30363d;color:var(--fg);padding:6px 14px;border-radius:7px;cursor:pointer;font-size:13px;font-family:inherit;line-height:1.4;transition:background .12s,border-color .12s;white-space:nowrap}
button.btn:hover{background:#2b323b;border-color:var(--bd2)}button.btn:active{transform:translateY(1px)}
button.btn.pri{background:#238636;border-color:#2ea043;font-weight:600}button.btn.pri:hover{background:#2ea043}
button.btn.dan{background:rgba(248,81,73,.1);border-color:rgba(248,81,73,.4);color:#ff7b72}button.btn.dan:hover{background:rgba(248,81,73,.2)}
button.btn.ghost{background:none;border-color:transparent;color:var(--mut)}button.btn.ghost:hover{background:#21262d;color:var(--fg)}
button.btn.sm{padding:3px 9px;font-size:12px}button.btn:disabled{opacity:.45;cursor:default;transform:none}
.btn-group{display:inline-flex;border:1px solid #30363d;border-radius:7px;overflow:hidden}.btn-group .btn{border:0;border-radius:0;border-right:1px solid #30363d}.btn-group .btn:last-child{border-right:0}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
/* inputs */
select,input[type=text],input[type=search],input[type=number]{background:var(--ink);color:var(--fg);border:1px solid var(--bd);border-radius:7px;padding:6px 9px;font:inherit;font-size:13px;max-width:100%}
input[type=text],input[type=search],input[type=number]{min-width:0;width:100%}
select:focus,input:focus,textarea:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 2px rgba(88,166,255,.18)}
textarea{width:100%;min-height:520px;background:var(--ink);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:12px;font:13px/1.5 var(--mono);tab-size:2;resize:vertical}
.switch{display:inline-flex;align-items:center;gap:8px;cursor:pointer;font-size:12.5px;color:var(--mut);user-select:none}
.switch input{display:none}.switch i{width:32px;height:18px;border-radius:9px;background:#30363d;position:relative;transition:background .15s;flex:none}
.switch i:after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:#fff;transition:left .15s}
.switch input:checked+i{background:var(--ok)}.switch input:checked+i:after{left:16px}
.search{position:relative;flex:1;min-width:160px;max-width:360px}.search input{padding-left:28px}.search:before{content:"⌕";position:absolute;left:9px;top:50%;transform:translateY(-50%);color:var(--mut);font-size:15px}
/* panels */
pre{background:var(--ink);border:1px solid var(--bd);border-radius:8px;padding:10px 12px;overflow:auto;max-height:520px;font:12.5px/1.5 var(--mono);white-space:pre-wrap;word-break:break-all;margin:8px 0}
.panel{border:1px solid var(--bd);border-radius:8px;overflow:hidden;margin:8px 0;background:var(--ink)}
.panel-h{display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--card2);border-bottom:1px solid var(--bd);font-size:12px;color:var(--mut)}
.panel-h b{color:var(--fg);font-weight:600}.panel pre{border:0;border-radius:0;margin:0;max-height:520px}
.panel.ok .panel-h{border-left:3px solid var(--ok)}.panel.bad .panel-h{border-left:3px solid var(--bad)}.panel.busy .panel-h{border-left:3px solid var(--warn)}
.diff .a{color:var(--bad);background:rgba(248,81,73,.07);display:inline-block;width:100%}.diff .b{color:var(--ok);background:rgba(63,185,80,.07);display:inline-block;width:100%}.diff .h{color:var(--acc)}
details{margin-top:8px}summary{cursor:pointer;color:var(--mut);font-size:12.5px;user-select:none}summary:hover{color:var(--fg)}
.empty{color:var(--mut);padding:22px;border:1px dashed var(--bd);border-radius:8px;text-align:center;font-size:13px}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--bd2);border-top-color:var(--acc);border-radius:50%;animation:rot .7s linear infinite;vertical-align:-2px;margin-right:6px}@keyframes rot{to{transform:rotate(360deg)}}
/* status */
.hero{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.hero .big{width:14px;height:14px;border-radius:50%;background:var(--mut);flex:none}.hero .big.ok{background:var(--ok);box-shadow:0 0 0 5px rgba(63,185,80,.15);animation:pulse 2s infinite}.hero .big.bad{background:var(--bad);box-shadow:0 0 0 5px rgba(248,81,73,.15)}
.hero .t{font-size:20px;font-weight:700;line-height:1.2}.hero .s{font-size:12.5px;color:var(--mut);margin-top:3px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;margin-top:14px}
.tile{background:var(--ink);border:1px solid var(--bd);border-radius:8px;padding:10px 12px;border-left:3px solid var(--bd2);min-width:0}
.tile b{display:block;font-size:11px;color:var(--mut);font-weight:500;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px}
.tile span{font-size:14px;word-break:break-all;display:block;line-height:1.35}.tile small{color:var(--mut);font-size:11.5px}
.tile.acc{border-left-color:var(--acc)}.tile.ok{border-left-color:var(--ok)}.tile.bad{border-left-color:var(--bad)}.tile.pur{border-left-color:var(--pur)}.tile.cyan{border-left-color:var(--cyan)}.tile.warn{border-left-color:var(--warn)}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.actions .sep{width:1px;height:22px;background:var(--bd)}
/* editor */
.editor{position:relative}.editor .meta{position:absolute;right:10px;bottom:10px;font:11px var(--mono);color:var(--mut);background:rgba(22,26,34,.85);padding:2px 7px;border-radius:5px;pointer-events:none}
/* backups */
.bk{display:flex;flex-direction:column;gap:6px}
.bk-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;font-size:12.5px;color:var(--mut)}.bk-bar .btn{font-size:12px}
.bki{display:grid;grid-template-columns:auto auto 1fr auto auto;gap:4px 12px;align-items:center;background:var(--ink);border:1px solid var(--bd);border-radius:8px;padding:9px 12px;border-left:3px solid var(--bd2)}
.bki.sel{border-color:var(--acc)}.bki.chk{background:rgba(88,166,255,.05);border-left-color:var(--acc)}.bki .ck{width:16px;height:16px;margin:0;cursor:pointer;accent-color:var(--acc)}.bki .ic{width:30px;height:30px;border-radius:7px;background:#21262d;display:grid;place-items:center;color:var(--mut);font-size:14px}
.bki .nm{font-family:var(--mono);font-size:12.5px}.bki .nm small{display:block;color:var(--mut);font-family:inherit;font-size:11.5px;margin-top:1px}
.bki .sz{font-size:12px;color:var(--mut);font-family:var(--mono)}.bki .act{display:flex;gap:4px}
/* proxies */
.pgs{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}
.pg{background:var(--ink);border:1px solid var(--bd);border-radius:8px;padding:11px 12px;border-top:3px solid var(--bd2);min-width:0}
.pg.Selector{border-top-color:var(--acc)}.pg.URLTest{border-top-color:var(--cyan)}.pg.Fallback{border-top-color:var(--warn)}.pg.LoadBalance{border-top-color:var(--pur)}
.pg-h{display:flex;align-items:center;gap:8px;margin-bottom:8px}.pg-h b{font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pg select{width:100%}.pg .now{font-size:13px;padding:6px 9px;border:1px dashed var(--bd);border-radius:7px;color:var(--fg);display:flex;align-items:center;gap:6px}.pg .now small{color:var(--mut)}
.pg .cnt{font-size:11px;color:var(--mut);margin-top:6px}
/* kernel */
.kver{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.kver .v{font:600 22px/1.2 var(--mono);letter-spacing:-.3px}.kver .arrow{color:var(--mut);font-size:18px}
.kver .v.new{color:var(--ok)}.kver .s{font-size:12px;color:var(--mut);margin-top:3px}
.krow{display:grid;grid-template-columns:1fr 1fr auto;gap:10px;align-items:end;margin:4px 0 8px}
.krow .fld{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--mut)}.krow select{width:100%}
.kwarn{font-size:12px;color:var(--warn);background:rgba(210,153,34,.08);border:1px solid rgba(210,153,34,.3);border-radius:7px;padding:7px 10px;margin:6px 0}
.steps{display:flex;gap:4px;flex-wrap:wrap;font-size:11.5px;color:var(--mut);margin:6px 0 2px}.steps span{background:#21262d;border:1px solid var(--bd);border-radius:5px;padding:1px 7px}.steps i{font-style:normal;color:var(--bd2)}
.md{font:12.5px/1.6 inherit;white-space:pre-wrap;max-height:360px}
/* geo */
.geos{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
.geo{background:var(--ink);border:1px solid var(--bd);border-left:3px solid var(--bd2);border-radius:8px;padding:11px 12px;display:grid;grid-template-columns:auto 1fr auto;gap:4px 10px;align-items:start}
.geo.new{border-left-color:var(--warn)}.geo.ok{border-left-color:var(--ok)}.geo.miss{border-left-color:var(--bad)}
.geo .ck{width:16px;height:16px;margin:3px 0 0;accent-color:var(--acc)}
.geo .nm{font:600 13.5px var(--mono)}.geo .d{font-size:11.5px;color:var(--mut);grid-column:2}.geo .badge{justify-self:end}
.geo .kv{grid-column:2/-1;display:grid;grid-template-columns:auto 1fr;gap:2px 10px;font-size:12px;margin-top:6px}.geo .kv b{color:var(--mut);font-weight:500}.geo .kv span{font-family:var(--mono);font-size:11.5px}
.geo .act{grid-column:2/-1;display:flex;gap:4px;margin-top:8px}
/* logs */
.log{font:12px/1.55 var(--mono);max-height:70vh}.log .l{display:block;padding:0 4px;border-left:2px solid transparent}
.log .l.e{color:#ff7b72;border-left-color:var(--bad);background:rgba(248,81,73,.05)}.log .l.w{color:#e3b341;border-left-color:var(--warn)}.log .l.d{color:var(--mut)}.log .l.s{color:var(--mut)}
.log .l mark{background:rgba(210,153,34,.35);color:inherit;border-radius:2px}
/* misc */
#toast{position:fixed;right:16px;bottom:16px;background:#21262d;border:1px solid #30363d;border-left:3px solid var(--ok);padding:10px 14px;border-radius:8px;display:none;max-width:420px;z-index:9;box-shadow:0 8px 24px rgba(0,0,0,.4);font-size:13px}
#toast.bad{border-left-color:var(--bad)}
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch}
/* rules form */
.badge{font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid var(--bd);color:var(--mut);white-space:nowrap}
.badge.dirty{color:var(--warn);border-color:var(--warn);background:rgba(210,153,34,.1)}
.badge.ok{color:var(--ok);border-color:var(--ok)}
.rhelp{color:var(--mut);font-size:12px;margin:6px 0 2px}
.rsec{margin-top:18px}.rsec-h{display:flex;align-items:baseline;gap:10px;margin-bottom:8px;flex-wrap:wrap}
.rsec-h h3{margin:0;font-size:14px;font-weight:600;color:var(--fg)}
.rsec-h .cnt{background:#21262d;border:1px solid #30363d;border-radius:10px;padding:0 8px;font-size:12px;color:var(--mut);line-height:18px}
.rsec-h .d{color:var(--mut);font-size:12px}
.rlist{display:flex;flex-direction:column;gap:6px}
.rule{display:grid;grid-template-columns:26px 165px minmax(150px,1fr) 18px 180px auto auto;grid-template-areas:"n t v a p nr o" "n pv pv pv pv pv pv";gap:6px 8px;align-items:center;background:#0b0d11;border:1px solid var(--bd);border-left:3px solid var(--mut);border-radius:8px;padding:8px 10px 6px;transition:border-color .15s}
.rule:hover{border-color:#3a404b;border-left-color:inherit}
.rule.c-dom{border-left-color:#a371f7}.rule.c-ip{border-left-color:#d29922}.rule.c-rs{border-left-color:#39c5cf}.rule.c-proc{border-left-color:#f778ba}.rule.c-match{border-left-color:var(--bad)}.rule.c-raw{border-left-color:var(--mut)}
.rule.err{border-color:rgba(248,81,73,.55)}
.rule .n{grid-area:n;color:var(--mut);font:12px ui-monospace,SFMono-Regular,Menlo,monospace;text-align:right;align-self:center}
.rule .rt{grid-area:t}.rule .rvw{grid-area:v}.rule .arr{grid-area:a;color:var(--mut);text-align:center;font-size:14px}.rule .pol{grid-area:p}.rule .nrw{grid-area:nr}.rule .ops{grid-area:o}.rule .pv{grid-area:pv}
.rule select,.rule input[type=text]{width:100%;font-size:13px}
.rule .rt{font-weight:600;letter-spacing:.2px}
.rule .pol{display:flex;align-items:center;gap:6px}.rule .pol .dot{flex:none;margin:0}.rule .pol .dot.acc{background:var(--acc)}.rule .pol .dot.warn{background:var(--warn)}
.rule .nrw{white-space:nowrap;font-size:12px;color:var(--mut);display:inline-flex;align-items:center;gap:4px;cursor:pointer;user-select:none;padding:0 2px}
.rule .nrw input{margin:0}
.rule .ops{display:flex;gap:1px}.rule .ops .btn{padding:3px 8px;background:none;border-color:transparent;color:var(--mut);font-size:13px;line-height:1.2}
.rule .ops .btn:hover{background:#21262d;color:var(--fg)}.rule .ops .btn.dan:hover{background:rgba(248,81,73,.15);color:var(--bad)}.rule .ops .btn:disabled{opacity:.25}
.rule .pv{font:11.5px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:14px}
.rule .pv.bad{color:var(--bad)}.rule .pv.dup{color:var(--warn)}
.rule.m-raw .arr,.rule.m-raw .pol,.rule.m-raw .nrw,.rule.m-match .rvw,.rule.m-match .arr,.rule.m-match .nrw,.rule.no-nr .nrw{display:none}
.rempty{color:var(--mut);padding:16px;border:1px dashed var(--bd);border-radius:8px;text-align:center;font-size:13px}
.radd{margin-top:8px}.radd .btn{border-style:dashed;color:var(--mut)}.radd .btn:hover{color:var(--fg)}
.rlegend{display:flex;gap:12px;flex-wrap:wrap;font-size:11px;color:var(--mut);margin:10px 0 0}
.rlegend i{display:inline-block;width:10px;height:10px;border-radius:2px;vertical-align:-1px;margin-right:4px}
/* segmented control / mode switch */
.seg{display:inline-flex;background:var(--ink);border:1px solid var(--bd);border-radius:8px;padding:2px;gap:2px;flex:none}
.seg button{background:none;border:0;color:var(--mut);padding:4px 12px;border-radius:6px;cursor:pointer;font:inherit;font-size:12.5px;white-space:nowrap;line-height:1.5}
.seg button:hover{color:var(--fg)}.seg button.on{background:#262c37;color:var(--fg);font-weight:600}
.tbg{display:contents}
/* live overview */
.spd{font-family:var(--mono);gap:10px}.spd .u{color:var(--cyan)}.spd .d{color:var(--acc)}
.charts{display:grid;grid-template-columns:3fr 2fr;gap:10px;margin-top:12px}
.chart{background:var(--ink);border:1px solid var(--bd);border-radius:8px;padding:8px 10px;min-width:0}
.chart-h{display:flex;gap:12px;align-items:center;font-size:12px;color:var(--mut);margin-bottom:4px}.chart-h b{color:var(--fg);font-weight:600}
.chart-h i{display:inline-block;width:10px;height:3px;border-radius:2px;vertical-align:middle;margin-right:4px}
.chart canvas{width:100%;height:150px;display:block}
/* proxies */
.pxg{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;align-items:start}
.pgc{background:var(--ink);border:1px solid var(--bd);border-radius:10px;min-width:0;transition:border-color .15s}.pgc:hover{border-color:var(--bd2)}
.pgc-h{display:flex;align-items:center;gap:10px;padding:12px 12px 0 14px;cursor:pointer;user-select:none;min-width:0}
.gic{position:relative;width:24px;height:24px;flex:none;display:grid;place-items:center}.gic img{width:100%;height:100%;object-fit:contain;display:block}
.gic .av{width:100%;height:100%;border-radius:7px;display:grid;place-items:center;font-size:12px;font-weight:700;color:#fff;background:linear-gradient(135deg,var(--acc),var(--pur))}
.gic.sm{width:16px;height:16px}.gic.sm .av{border-radius:4px;font-size:9px}
.pgc-h b{font-size:15px;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.pgc-h .cnt{flex:none;font:600 10.5px var(--mono);color:var(--mut);border:1px solid var(--bd2);border-radius:5px;padding:0 5px;line-height:16px}
.pgc-t{flex:none;width:32px;height:32px;border-radius:8px;border:1px solid var(--bd);background:var(--card);color:var(--mut);display:grid;place-items:center;cursor:pointer;padding:0}
.pgc-t:hover{color:var(--fg);border-color:var(--bd2)}.pgc-t .spin{margin:0}
.pgc-b{display:flex;align-items:center;gap:10px;padding:9px 12px 0 14px;cursor:pointer;min-width:0}
.pgc-tags{display:flex;flex-direction:column;align-items:flex-start;gap:6px;min-width:0;flex:1}
.gpill{display:inline-flex;align-items:center;gap:6px;max-width:100%;font-size:11.5px;font-weight:600;line-height:20px;padding:0 8px;border-radius:6px;white-space:nowrap;overflow:hidden;background:#262c37;color:var(--fg)}
.gpill .nm{overflow:hidden;text-overflow:ellipsis;min-width:0}.gpill i{font-style:normal;opacity:.6;font-weight:400}
.gpill.Selector{background:rgba(88,166,255,.16);color:#79b8ff}.gpill.URLTest{background:rgba(57,197,207,.15);color:var(--cyan)}.gpill.Fallback{background:rgba(210,153,34,.16);color:#e3b341}.gpill.LoadBalance{background:rgba(163,113,247,.16);color:#bc8cff}
.gpill.spd{background:rgba(247,120,186,.13);color:var(--pink);font:500 11px/20px var(--mono)}.gpill.spd.idle{background:#1b2029;color:var(--mut)}
.pgc-b .chev{color:var(--mut);flex:none;transition:transform .15s;width:16px;height:16px}.pgc.open .chev{transform:rotate(180deg)}
.pgc-f{display:flex;align-items:center;gap:12px;padding:10px 14px 13px;cursor:pointer;min-width:0}
.pgc-f .latv{font-size:11.5px;min-width:30px;text-align:right}
.dots{display:flex;flex-wrap:wrap;gap:5px;flex:1;min-width:0}.dots i{width:12px;height:12px;border-radius:50%;background:var(--bd2);--c:var(--bd2)}
.dots i.g{background:var(--ok);--c:var(--ok)}.dots i.y{background:var(--warn);--c:var(--warn)}.dots i.r{background:var(--bad);--c:var(--bad)}.dots i.on{background:#fff;box-shadow:inset 0 0 0 3px var(--c)}
.lbar{flex:1;display:flex;height:6px;border-radius:3px;overflow:hidden;background:#262c37;min-width:0}.lbar i{height:100%}.lbar .g{background:var(--ok)}.lbar .y{background:var(--warn)}.lbar .r{background:var(--bad)}
.pgc .nds{border-top:1px solid var(--bd);padding:10px 12px 12px}
.nds{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:6px}
.nd{background:var(--card);border:1px solid var(--bd);border-radius:7px;padding:7px 9px;min-width:0;transition:border-color .12s,background .12s}
.nd.sel{cursor:pointer}.nd.sel:hover{border-color:var(--bd2);background:var(--card2)}.nd.on{border-color:var(--acc);background:rgba(88,166,255,.09)}
.nd-n{font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:flex;align-items:center;gap:5px}.nd-n span{overflow:hidden;text-overflow:ellipsis}
.nd-m{display:flex;align-items:center;gap:5px;margin-top:3px;font-size:10.5px;color:var(--mut);min-width:0}
.nd-m .u{border:1px solid var(--bd);border-radius:4px;padding:0 4px;line-height:14px}.nd-m .sub{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.lat,.latv{background:none;border:0;padding:0 2px;font:600 11.5px var(--mono);color:var(--mut);white-space:nowrap;flex:none}.lat{cursor:pointer}.lat:hover{text-decoration:underline}
.lat.g,.latv.g{color:var(--ok)}.lat.y,.latv.y{color:var(--warn)}.lat.r,.latv.r{color:var(--bad)}
.pvs{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
.prv{background:var(--ink);border:1px solid var(--bd);border-left:3px solid var(--pur);border-radius:8px;padding:10px 12px;min-width:0}
.prv-h{display:flex;align-items:center;gap:8px;margin-bottom:3px}.prv-h b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.prv .act{display:flex;gap:4px;margin-top:8px}
.bar{height:5px;background:#21262d;border-radius:3px;overflow:hidden;margin-top:5px}.bar i{display:block;height:100%;background:var(--acc)}.bar i.warn{background:var(--warn)}.bar i.bad{background:var(--bad)}
/* connections */
/* 连接列表：随页面滚动、不横向滚动，列随宽度逐级收起 */
.cn-tb{margin-bottom:0}.cn-opts{display:contents}.cn-tb .search{flex:2 1 220px;max-width:420px}.cn-opts select{width:auto;max-width:170px}
.cl{border:1px solid var(--bd);border-radius:8px;background:var(--ink)}
.cr,.cr-h{display:grid;grid-template-columns:minmax(0,1fr) 150px 58px 96px 104px 28px;gap:0 12px;align-items:center;padding:7px 8px 7px 11px}
.cr-h{position:sticky;z-index:2;background:var(--card2);border-bottom:1px solid var(--bd);border-radius:8px 8px 0 0;font-size:11.5px;color:var(--mut);padding-top:6px;padding-bottom:6px}
.cr-h span{cursor:pointer;user-select:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.cr-h span:hover,.cr-h .s{color:var(--fg)}.cr-h .r{text-align:right}
.cr-h .s:after{content:" ▾"}.cr-h .s.asc:after{content:" ▴"}
.cr{border-top:1px solid var(--bd);border-left:3px solid transparent;cursor:pointer;transition:background .1s}#cn-rows .cr:first-child{border-top:0}
.cr:hover{background:rgba(255,255,255,.03)}.cr.hot{border-left-color:var(--acc)}.cr.closed .c-main{opacity:.65}
.c-main{min-width:0}
.c-l1,.c-l2,.c-l3{display:flex;align-items:center;gap:6px;min-width:0;white-space:nowrap}
.c-host{font-size:13px;font-weight:550;overflow:hidden;text-overflow:ellipsis;min-width:0}
.c-l1 .tag{flex:none;font-size:10px;padding:0 5px;line-height:15px}.c-in{flex:none;font-size:11px;color:var(--mut)}
.c-l2{font-size:11.5px;color:var(--mut);margin-top:2px}
.c-rule{flex:0 1 auto;max-width:42%;overflow:hidden;text-overflow:ellipsis}.c-l2 i{font-style:normal;flex:none;opacity:.6}
.c-chain{flex:1 1 0;overflow:hidden;text-overflow:ellipsis;min-width:0}.c-chain b{color:var(--fg);font-weight:500}.c-chain b.ok{color:var(--ok)}.c-chain b.bad{color:var(--bad)}
.c-l3{display:none;font:11px var(--mono);color:var(--mut);margin-top:2px}.c-l3 span{overflow:hidden;text-overflow:ellipsis;min-width:0}.c-l3 b{font-weight:400;flex:none}
.c-src,.c-tot,.c-spd{display:flex;flex-direction:column;font:11.5px/1.45 var(--mono);min-width:0;white-space:nowrap}
.c-src span,.c-src small{overflow:hidden;text-overflow:ellipsis}.c-src small{color:var(--mut);font:11px -apple-system,BlinkMacSystemFont,sans-serif}
.c-time{font:11.5px var(--mono);color:var(--mut);text-align:right;white-space:nowrap}
.c-tot{color:var(--mut);text-align:right}.c-spd{text-align:right}.c-spd .d{color:var(--acc)}.c-spd .u{color:var(--cyan)}.c-spd .z{color:var(--bd2)}
.c-x{display:grid;place-items:center;width:26px;height:26px;border-radius:6px;border:0;background:none;color:var(--mut);cursor:pointer;font-size:13px}.c-x:hover{background:rgba(248,81,73,.15);color:var(--bad)}
.cn-empty{border:0;border-radius:0}
@media (max-width:980px){
 .cr,.cr-h{grid-template-columns:minmax(0,1fr) 104px 28px}.c-src,.c-tot,.c-time,.cr-h [data-k=src],.cr-h [data-k=dl],.cr-h [data-k=start]{display:none}.c-l3{display:flex}
}
/* runtime rules / lists */
.lst{display:flex;flex-direction:column;gap:6px}
.li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 12px;align-items:center;background:var(--ink);border:1px solid var(--bd);border-left:3px solid var(--cyan);border-radius:8px;padding:8px 12px}
.li .nm{min-width:0}.li .nm small{display:block;color:var(--mut);font-size:11.5px;margin-top:2px}.li .act{display:flex;gap:4px}
.rrl{border:1px solid var(--bd);border-radius:8px;background:var(--ink);overflow:hidden}
.rr{display:grid;grid-template-columns:48px 120px minmax(0,1fr) minmax(0,220px);gap:10px;padding:5px 10px;border-bottom:1px solid var(--bd);border-left:3px solid var(--mut);font-size:12.5px;align-items:center}
.rr:last-child{border-bottom:0}.rr .i{color:var(--mut);font:11.5px var(--mono);text-align:right}.rr .ty{font:600 11.5px var(--mono)}
.rr .pl{font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.rr .px{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:flex;align-items:center}
.rr.c-dom{border-left-color:#a371f7}.rr.c-ip{border-left-color:#d29922}.rr.c-rs{border-left-color:#39c5cf}.rr.c-proc{border-left-color:#f778ba}.rr.c-match{border-left-color:var(--bad)}
/* live logs */
.llog{font:12px/1.55 var(--mono);max-height:70vh;min-height:200px;overflow:auto;background:var(--ink);border:1px solid var(--bd);border-radius:8px;padding:6px 0}
.ll{display:grid;grid-template-columns:62px 58px minmax(0,1fr);gap:8px;padding:1px 10px;border-left:2px solid transparent}
.ll .tm{color:var(--mut)}.ll .lv{font-weight:600;font-size:10.5px;text-transform:uppercase;padding-top:1px}.ll .msg{word-break:break-all;white-space:pre-wrap}
.ll.error{border-left-color:var(--bad)}.ll.error .lv,.ll.error .msg{color:#ff7b72}.ll.warning{border-left-color:var(--warn)}.ll.warning .lv{color:var(--warn)}.ll.info .lv{color:var(--acc)}.ll.debug .lv,.ll.debug .msg{color:var(--mut)}
.ll mark{background:rgba(210,153,34,.35);color:inherit;border-radius:2px}
/* settings */
.sgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}
.sf{background:var(--ink);border:1px solid var(--bd);border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:6px;min-width:0}
.sf>b{font-size:12px;color:var(--mut);font-weight:500}.sf small{color:var(--mut);font-size:11.5px;min-height:1em}
/* modal */
.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;align-items:center;justify-content:center;z-index:20;padding:16px}.modal.on{display:flex}
.modal-c{background:var(--card);border:1px solid var(--bd2);border-radius:12px;width:min(760px,100%);max-height:86vh;display:flex;flex-direction:column;box-shadow:0 20px 50px rgba(0,0,0,.5)}
.modal-h{display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:1px solid var(--bd)}.modal-h b{font-size:14px}
.modal-b{padding:12px 16px;overflow:auto}.modal-f{display:flex;gap:8px;justify-content:flex-end;padding:10px 16px;border-top:1px solid var(--bd)}.modal-f:empty{display:none}
.kvs{display:grid;grid-template-columns:auto minmax(0,1fr);gap:5px 14px;font-size:12.5px}.kvs b{color:var(--mut);font-weight:500;white-space:nowrap}.kvs span{font-family:var(--mono);word-break:break-all}
@media (max-width:640px){
 body{font-size:15px}
 header{padding:10px 12px 6px;padding-top:calc(10px + env(safe-area-inset-top));gap:8px}header h1{font-size:15px}header .pill{font-size:11px}header a.ext{display:inline-flex;align-items:center;min-height:32px;padding:4px 10px;white-space:nowrap}header a.ext .lg{display:none}
 nav{padding:0 6px}nav button{padding:8px 9px 9px;font-size:13.5px}nav button small{display:none}nav button small.dotn{display:inline-block}.gl{display:none}.ng+.ng{margin-left:4px;padding-left:4px}
 main{padding:10px;padding-bottom:calc(16px + env(safe-area-inset-bottom))}
 .card{padding:12px;margin-bottom:10px}
 .toolbar{padding:8px 0}.toolbar .btn{flex:1 1 30%;min-height:40px}.toolbar .badge,.toolbar .info{flex:1 1 100%}
 .hero .t{font-size:18px}.tiles{grid-template-columns:1fr 1fr;gap:8px}.tile span{font-size:13px}
 .actions .btn{flex:1 1 44%;min-height:42px;font-size:14px}.actions .sep{display:none}.actions .btn-group{flex:1 1 100%}.actions .btn-group .btn{flex:1}
 select{font-size:16px;min-height:40px}textarea{min-height:55vh;font-size:14px}pre{max-height:60vh;font-size:12px}
 .bki{grid-template-columns:auto auto 1fr;grid-template-areas:"ck ic nm" "sz sz sz" "act act act"}.bki .ck{grid-area:ck;width:20px;height:20px}.bki .ic{grid-area:ic}.bki .nm{grid-area:nm}.bki .sz{grid-area:sz}.bki .act{grid-area:act;padding-top:4px}.bki .act .btn{flex:1;min-height:40px}
 .pgs{grid-template-columns:1fr}
 .krow{grid-template-columns:1fr}.krow .btn{min-height:42px}.kver .v{font-size:19px}
 .search{max-width:none;flex:1 1 100%}
 #toast{left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom));max-width:none;text-align:center}
 .rule{grid-template-columns:26px 1fr 1fr auto;grid-template-areas:"n t t o" "v v v v" "a p p nr" "pv pv pv pv";padding:10px 10px 8px}
 .rule select,.rule input[type=text]{font-size:16px;min-height:40px}.rule .ops .btn{min-height:36px;padding:4px 10px}
 .rule .pv{white-space:normal;word-break:break-all}
 .radd .btn{width:100%;min-height:42px}
 input[type=text],input[type=search]{font-size:16px;min-height:40px}
 label{min-height:40px;display:inline-flex;align-items:center;gap:4px}input[type=checkbox]{width:18px;height:18px}
 header .spd{display:none}
 .charts{grid-template-columns:1fr}.chart canvas{height:130px}
 .seg button{padding:6px 12px;font-size:13.5px}.toolbar .seg{flex:1 1 100%}.toolbar .seg button{flex:1}
 .pxg{grid-template-columns:1fr}.pgc .nds{grid-template-columns:1fr 1fr;padding:8px}.dots i{width:13px;height:13px}
 #t-proxies .card{padding:10px 0 12px;background:none;border:0}
 #t-proxies .toolbar{position:static;padding:0 0 8px;background:none;gap:8px}
 #t-proxies .toolbar .seg{flex:1 1 calc(50% - 4px)}#t-proxies .toolbar .seg button{padding:6px 8px}
 #t-proxies .toolbar select{flex:1 1 120px;min-width:0;min-height:36px;font-size:14px}#t-proxies .toolbar .switch{min-height:36px;flex:none}#t-proxies .toolbar .sp{display:none}
 #t-proxies .toolbar .btn{flex:1 1 28%;min-height:36px;font-size:13px;padding:5px 8px}
 input[type=number]{font-size:16px;min-height:40px}
 #t-conns .card{padding:10px 0 12px;background:none;border:0}
 #t-conns .toolbar{position:static;padding:0 0 10px;border:0;background:none;gap:8px}
 #t-conns .toolbar .seg{flex:1 1 100%}#t-conns .search{flex:1 1 100%}
 .cn-opts{display:flex;gap:8px;flex:1 1 100%;align-items:center}.cn-opts select{flex:1 1 0;min-width:0;font-size:14px;min-height:36px}.cn-opts .switch{flex:none;min-height:36px}
 #t-conns .toolbar .info{flex:1 1 auto;font-size:11.5px}#t-conns .toolbar .btn{flex:none;min-height:34px}
 .cr-h{display:none}.cl{border-radius:10px}
 .cr{grid-template-columns:minmax(0,1fr) auto 26px;gap:0 8px;padding:9px 6px 9px 10px}.c-time{display:none}
 .c-host{font-size:13.5px}.c-spd{font-size:11px}.c-spd .z{display:none}#t-conns .toolbar .info{text-align:left}
 .cn-opts select{max-width:none}#t-conns .toolbar .search{max-width:none}
 .rr{grid-template-columns:36px minmax(0,1fr);grid-template-areas:"i ty" "i pl" "i px";gap:1px 8px}.rr .i{grid-area:i;align-self:start}.rr .ty{grid-area:ty}.rr .pl{grid-area:pl;white-space:normal;word-break:break-all}.rr .px{grid-area:px}
 .ll{grid-template-columns:58px minmax(0,1fr);grid-template-areas:"tm lv" "msg msg"}.ll .tm{grid-area:tm}.ll .lv{grid-area:lv}.ll .msg{grid-area:msg}
 .sgrid{grid-template-columns:1fr}
 .modal{padding:0;align-items:flex-end}.modal-c{border-radius:14px 14px 0 0;max-height:90vh;padding-bottom:env(safe-area-inset-bottom)}
}
</style></head><body>
<div class="top">
<header><div class="logo">M</div><h1>mihomo 管理</h1><span class="pill" id="hstate"><span class="dot"></span>…</span><span class="pill spd" id="hspd" style="display:none" title="实时上传 / 下载速度"><span class="u" id="hspd-u"></span><span class="d" id="hspd-d"></span></span><span class="sp"></span>
<a id="dash" class="ext" href="#" target="_blank" rel="noopener" style="display:none"></a></header>
<nav>
<div class="ng"><button data-t="status" class="on">概览</button><button data-t="proxies">代理</button><button data-t="conns">连接<small id="nav-conns"></small></button><button data-t="rtrules">规则</button><button data-t="logs">日志</button></div>
<div class="ng"><span class="gl">配置</span><button data-t="rules">自定义规则</button><button data-t="override">override<small>yaml</small></button><button data-t="config">config<small>只读</small></button><button data-t="settings">内核设置</button></div>
<div class="ng"><span class="gl">维护</span><button data-t="update">更新<small id="nav-upd"></small></button><button data-t="backups">备份</button></div>
</nav>
</div>
<main>
<section class="tab on" id="t-status">
 <div class="card">
  <div class="hero"><div class="big" id="h-big"></div><div><div class="t" id="h-t">加载中…</div><div class="s" id="h-s"></div></div><span class="sp"></span>
   <button class="btn pri" onclick="doUpdate()">↻ 更新订阅并应用</button></div>
  <div class="tiles" id="stat"></div>
 </div>
 <div class="card">
  <div class="card-h"><h2>实时</h2><span class="d" id="live-st">连接 controller…</span><span class="sp"></span><span class="seg modesw" title="代理模式"></span></div>
  <div class="tiles" id="live-tiles">
   <div class="tile cyan"><b>上传速度</b><span class="mono" id="lv-up">-</span><small id="lv-upt"></small></div>
   <div class="tile acc"><b>下载速度</b><span class="mono" id="lv-down">-</span><small id="lv-downt"></small></div>
   <div class="tile ok"><b>活动连接</b><span class="mono" id="lv-conns">-</span><small><a href="#conns">查看连接 →</a></small></div>
   <div class="tile pur"><b>内存占用</b><span class="mono" id="lv-mem">-</span><small>mihomo 进程</small></div>
  </div>
  <div class="charts">
   <div class="chart"><div class="chart-h"><b>流量</b><span><i style="background:var(--cyan)"></i>上传</span><span><i style="background:var(--acc)"></i>下载</span><span class="sp"></span><span>最近 60 秒</span></div><canvas id="ch-tr"></canvas></div>
   <div class="chart"><div class="chart-h"><b>内存</b><span><i style="background:var(--pur)"></i>占用</span></div><canvas id="ch-mem"></canvas></div>
  </div>
 </div>
 <div class="card">
  <div class="card-h"><h2>服务操作</h2><span class="d">热重载不中断连接；重启会断开所有连接</span></div>
  <div class="actions">
   <button class="btn" onclick="svc('validate')">✓ 校验当前 config</button>
   <button class="btn" onclick="svc('reload')">⟳ 热重载</button>
   <button class="btn" onclick="svc('restart')">↺ 重启服务</button>
   <span class="sep"></span>
   <button class="btn" id="b-start" onclick="svc('start')">▶ 启动</button>
   <button class="btn dan" id="b-stop" onclick="if(confirm('确定停止 mihomo？所有代理连接会中断。'))svc('stop')">■ 停止</button>
  </div>
  <div class="panel" id="p-status"><div class="panel-h"><b>输出</b><span id="p-status-t"></span><span class="sp"></span><button class="btn ghost sm" onclick="clearOut('status')">清空</button></div><pre id="out-status" class="mut">操作输出会显示在这里</pre></div>
 </div>
</section>
<section class="tab" id="t-rules">
 <div class="card">
  <div class="toolbar" id="rtool">
   <span id="ru-dirty" class="badge">已同步</span><span id="ru-info" class="mut" style="font-size:12px"></span><span class="sp"></span>
   <button class="btn" onclick="loadRules(true)">放弃修改</button>
   <button class="btn" onclick="saveRules(false)">仅保存</button>
   <button class="btn pri" onclick="saveRules(true)">保存并应用</button>
  </div>
  <div class="rhelp">规则按从上到下的顺序匹配，命中即停止。此处的规则写入 override.yaml 的 <code>rules-prepend</code> / <code>rules-append</code>，订阅更新后会自动重新合并；override 里的其它内容不受影响。</div>
  <div class="rlegend"><span><i style="background:#a371f7"></i>域名</span><span><i style="background:#d29922"></i>IP</span><span><i style="background:#39c5cf"></i>规则集</span><span><i style="background:#f778ba"></i>端口/进程</span><span><i style="background:var(--bad)"></i>MATCH</span><span><i style="background:var(--mut)"></i>原文</span></div>

  <div class="rsec"><div class="rsec-h"><h3>前置规则</h3><span class="cnt" id="cnt-prepend">0</span><span class="d">插在订阅规则最前面 · 优先级最高 · 适合强制某域名走代理/直连</span></div>
   <div class="rlist" id="rl-prepend"></div>
   <div class="radd"><button class="btn" onclick="addRule('prepend')">＋ 添加前置规则</button></div></div>

  <div class="rsec"><div class="rsec-h"><h3>后置规则</h3><span class="cnt" id="cnt-append">0</span><span class="d">插在最后的 MATCH 之前 · 兜底 · 只对订阅没覆盖的流量生效</span></div>
   <div class="rlist" id="rl-append"></div>
   <div class="radd"><button class="btn" onclick="addRule('append')">＋ 添加后置规则</button></div></div>

  <div id="ru-insert" class="warn" style="display:none;margin-top:12px;font-size:12px"></div>
  <div class="panel" style="margin-top:14px"><div class="panel-h"><span>保存结果</span></div><pre id="out-rules" class="mut">尚未保存</pre></div>
  <details><summary class="mut">生成的 YAML 预览</summary><pre id="ru-yaml"></pre></details>
  <details><summary class="mut">当前生效的完整规则（config.yaml）</summary><pre id="ru-cur"></pre></details>
 </div>
</section>
<section class="tab" id="t-override">
 <div class="card">
  <div class="toolbar">
   <span id="ov-dirty" class="badge">已同步</span><span id="ov-info" class="info"></span><span class="sp"></span>
   <button class="btn" onclick="loadOverride(true)">放弃修改</button>
   <button class="btn" onclick="validateOverride()">✓ 校验</button>
   <button class="btn" onclick="saveOverride(false)">仅保存</button>
   <button class="btn pri" onclick="saveOverride(true)">保存并应用</button></div>
  <div class="mut" style="font-size:12px;margin-bottom:8px">校验会：下载订阅 → 用 merge-config.py 合并 → <code>mihomo -t</code>，不落盘。字典递归合并、<code>proxy-groups</code> 按 name 合并、规则用 <code>rules-prepend / rules-append / rules-insert</code>，值为 <code>null</code> 表示删除。</div>
  <div class="editor"><textarea id="ov" spellcheck="false"></textarea><span class="meta" id="ov-meta"></span></div>
  <div class="panel" id="p-ov"><div class="panel-h"><b>校验 / 保存结果</b><span id="p-ov-t"></span></div><pre id="out-ov" class="mut">尚未校验</pre></div>
  <details><summary>合并后 config.yaml 差异 <span id="ov-diff-stat" class="mono"></span></summary><pre id="ov-diff" class="diff"></pre></details>
 </div>
</section>
<section class="tab" id="t-backups">
 <div class="card">
  <div class="card-h"><h2>config.yaml 备份</h2><span class="tag" id="bk-cnt">0</span><span class="d">每次更新订阅前自动备份，保留最近 20 份</span><span class="sp"></span><button class="btn sm" onclick="loadBackups()">↻ 刷新</button></div>
  <div class="mut" style="font-size:12px;margin-bottom:10px">恢复流程：<code>mihomo -t</code> 校验备份 → 把当前 config 再备份一份 → 替换 → 重启服务。</div>
  <div class="bk-bar"><label class="switch" style="gap:6px"><input type="checkbox" id="bk-all" onchange="bkToggleAll(this.checked)"><i></i>全选</label><span id="bk-selinfo">未选择</span><span class="sp"></span><button class="btn sm" onclick="bkCleanup()" title="只保留最新 5 份，删除其余">清理旧备份（保留 5）</button><button class="btn sm dan" id="bk-delsel" onclick="bkDeleteSelected()" disabled>删除选中</button></div>
  <div class="bk" id="bk"></div>
  <div class="panel" id="p-bk" style="margin-top:12px"><div class="panel-h"><b id="bk-diff-t">差异</b><span id="bk-diff-stat" class="mono"></span><span class="sp"></span><button class="btn ghost sm" onclick="$('#bk-diff').innerHTML='';$('#bk-diff-t').textContent='差异';$('#bk-diff-stat').textContent='';document.querySelectorAll('.bki.sel').forEach(x=>x.classList.remove('sel'))">清空</button></div><pre id="bk-diff" class="diff mut">点「对比」查看备份与当前 config.yaml 的差异</pre></div>
 </div>
</section>
<section class="tab" id="t-proxies">
 <div class="card">
  <div class="toolbar">
   <span class="seg" id="px-view"><button data-v="groups" class="on">代理 <span id="px-ng">0</span></button><button data-v="providers">代理集合 <span id="px-np">0</span></button></span>
   <span class="seg modesw" title="代理模式"></span>
   <span class="search"><input type="search" id="pxq" placeholder="搜索节点 / 策略组" autocomplete="off"></span>
   <span class="tbg px-g"><select id="pxsort"><option value="">默认顺序</option><option value="delay">按延迟</option><option value="name">按名称</option></select>
   <label class="switch"><input type="checkbox" id="pxhide"><i></i>隐藏超时</label></span>
   <span class="sp"></span>
   <span class="tbg px-g"><button class="btn" id="px-expand" onclick="pxExpandAll()">全部展开</button><button class="btn pri" id="px-testall" onclick="pxTestAll()">⚡ 全部测速</button></span>
   <span class="tbg px-p" style="display:none"><button class="btn pri" onclick="pvUpdateAll(this)">↻ 全部更新</button></span>
   <button class="btn" onclick="loadProxies()">↻ 刷新</button>
  </div>
  <details class="px-g" style="margin:0 0 10px"><summary>测速与切换设置</summary>
   <div class="row"><span class="mut" style="font-size:12px">测速地址</span><input type="text" id="px-url" style="max-width:340px" spellcheck="false"><span class="mut" style="font-size:12px">超时 ms</span><input type="number" id="px-to" min="500" max="30000" step="500" style="width:100px">
    <label class="switch"><input type="checkbox" id="px-autoclose"><i></i>切换节点后断开该组的旧连接</label><button class="btn sm" onclick="pxSaveSet()">保存</button></div></details>
  <div class="pxg px-g" id="px"><div class="empty">加载中…</div></div>
  <div class="pvs px-p" id="pvs" style="display:none"></div>
 </div>
</section>
<section class="tab" id="t-conns">
 <div class="card">
  <div class="toolbar cn-tb">
   <span class="seg" id="cn-view"><button data-v="active" class="on">活动 <span id="cn-na">0</span></button><button data-v="closed">已关闭 <span id="cn-nc">0</span></button></span>
   <span class="search"><input type="search" id="cnq" placeholder="过滤主机 / IP / 规则 / 链路 / 进程" autocomplete="off"></span>
   <span class="cn-opts">
    <select id="cn-dev" title="按来源设备筛选"><option value="">全部来源</option></select>
    <select id="cn-sort" title="排序"></select>
    <label class="switch"><input type="checkbox" id="cn-pause"><i></i>暂停</label>
   </span>
   <span class="sp"></span><span class="info mono" id="cn-info"></span>
   <button class="btn dan sm" id="cn-closeall" onclick="cnCloseAll()">断开全部</button>
  </div>
  <div class="cl" id="cn-list"><div class="cr-h" id="cn-h"><span data-k="host">连接 / 规则 → 链路</span><span data-k="src">来源 / 进程</span><span data-k="start" class="r">时长</span><span data-k="dl" class="r">流量</span><span data-k="dls" class="r">速度</span><span></span></div><div id="cn-rows"></div><div class="empty cn-empty" id="cn-empty" style="display:none"></div></div>
  <div class="mut" id="cn-more" style="font-size:12px;margin-top:8px"></div>
 </div>
</section>
<section class="tab" id="t-rtrules">
 <div class="card" id="rp-card">
  <div class="card-h"><h2>规则集</h2><span class="tag" id="rp-cnt">0</span><span class="d">rule-providers</span><span class="sp"></span><button class="btn sm" onclick="rpUpdateAll(this)">↻ 全部更新</button></div>
  <div id="rps"></div>
 </div>
 <div class="card">
  <div class="toolbar"><b style="font-size:14px">生效规则</b><span class="tag" id="rr-cnt">0</span><span class="search"><input type="search" id="rrq" placeholder="过滤类型 / 内容 / 策略" autocomplete="off"></span><span class="sp"></span>
   <button class="btn" onclick="showTab('rules')">✎ 编辑自定义规则</button><button class="btn" onclick="loadRtRules()">↻ 刷新</button></div>
  <div class="mut" style="font-size:12px;margin-bottom:8px">从运行中的 mihomo 读取，按顺序从上到下匹配，命中即停止。</div>
  <div class="rrl" id="rrl"><div class="empty">加载中…</div></div>
  <div class="row"><button class="btn sm" id="rr-more" onclick="RR.limit+=1000;renderRtRules()" style="display:none"></button></div>
 </div>
</section>
<section class="tab" id="t-settings">
 <div class="card">
  <div class="card-h"><h2>内核设置</h2><span class="d" id="st-ver"></span><span class="sp"></span><button class="btn sm" onclick="loadSettings()">↻ 刷新</button></div>
  <div class="kwarn" style="margin-bottom:12px">这里的修改直接作用于运行中的 mihomo，热重载、更新订阅或重启后会恢复为 config.yaml 里的值；要长期生效，请写进 <a href="#override">override.yaml</a>。</div>
  <div class="sgrid" id="st-grid"><div class="empty">加载中…</div></div>
 </div>
 <div class="card">
  <div class="card-h"><h2>维护操作</h2><span class="d">不会重启服务</span></div>
  <div class="actions">
   <button class="btn" onclick="stReload()">⟳ 重载 config.yaml</button>
   <button class="btn" onclick="stAct('fakeip')">清空 FakeIP 缓存</button>
   <button class="btn" onclick="stAct('dns')">清空 DNS 缓存</button>
   <button class="btn dan" onclick="cnCloseAll(true)">断开所有连接</button>
  </div>
 </div>
 <div class="card">
  <div class="card-h"><h2>DNS 查询</h2><span class="d">用内核的 DNS 解析器查询</span></div>
  <div class="row"><input type="text" id="dq-name" placeholder="example.com" style="max-width:320px" spellcheck="false" autocomplete="off" onkeydown="if(event.key==='Enter')dnsQuery()"><select id="dq-type"><option>A</option><option>AAAA</option><option>CNAME</option><option>MX</option><option>TXT</option><option>NS</option></select><button class="btn" onclick="dnsQuery()">查询</button></div>
  <pre id="dq-out" class="mut">输入域名后查询</pre>
 </div>
</section>
<section class="tab" id="t-update">
 <div class="card">
  <div class="card-h"><h2>mihomo 内核</h2><span class="d" id="k-src">GitHub · MetaCubeX/mihomo</span><span class="sp"></span><span class="info mut" id="k-fetched" style="font-size:12px"></span><button class="btn sm" onclick="loadKernel(true)">↻ 检查更新</button></div>
  <div class="kver"><div><div class="v" id="k-cur">…</div><div class="s" id="k-cur-s"></div></div><span class="arrow" id="k-arrow" style="display:none">→</span><div id="k-latest-w" style="display:none"><div class="v new" id="k-latest"></div><div class="s" id="k-latest-s"></div></div><span class="sp"></span><span id="k-badge" class="badge"></span></div>
  <div class="tiles" id="kstat"></div>
 </div>
 <div class="card">
  <div class="card-h"><h2>安装版本</h2><span class="d">下载 .gz 资产，校验后替换二进制并重启</span></div>
  <div class="krow"><div class="fld"><span style="display:flex;align-items:center;gap:10px">版本<span class="sp"></span><label class="switch" style="font-size:11.5px;min-height:0"><input type="checkbox" id="k-pre" onchange="kFillRel()"><i></i>显示预发布</label></span><select id="k-rel" onchange="kRelChange()"></select></div><div class="fld">构建变体<select id="k-asset" onchange="kAssetChange()"></select></div><button class="btn pri" id="k-go" onclick="kernelUpdate()">⤓ 下载并安装</button></div>
  <div id="k-warn" class="kwarn" style="display:none"></div>
  <div class="steps"><span>下载</span><i>›</i><span>解压</span><i>›</i><span>新内核 -v</span><i>›</i><span>新内核 -t 校验当前 config</span><i>›</i><span>备份旧内核</span><i>›</i><span>替换</span><i>›</i><span>重启服务</span><i>›</i><span>失败自动回滚</span></div>
  <details id="k-notes"><summary>更新说明 <span id="k-notes-tag" class="mono mut"></span></summary><pre id="k-body" class="md"></pre></details>
  <div class="panel" id="p-k"><div class="panel-h"><b>安装输出</b><span id="p-k-t"></span><span class="sp"></span><button class="btn ghost sm" onclick="clearOut('k')">清空</button></div><pre id="out-k" class="mut">尚未执行</pre></div>
 </div>
 <div class="card">
  <div class="card-h"><h2>内核备份</h2><span class="tag" id="k-bkcnt">0</span><span class="d">更新时自动保留最近 3 份旧内核，可一键回滚（同样会校验 config 并重启）</span></div>
  <div class="bk" id="k-bk"></div>
 </div>
 <div class="card">
  <div class="card-h"><h2>地理数据库</h2><span class="d">GeoIP / ASN 数据，来源 <span class="mono" id="g-repo"></span>（config 里的 <code>geox-url</code> 优先）</span><span class="sp"></span><span class="info mut" id="g-fetched" style="font-size:12px"></span><button class="btn sm" onclick="loadGeo(true)">↻ 检查更新</button></div>
  <div class="geos" id="geos"></div>
  <div class="row" style="margin-top:10px"><label class="switch"><input type="checkbox" id="g-restart" checked><i></i>更新后重启服务（否则下次重启才生效）</label><span class="sp"></span>
   <button class="btn" onclick="geoUpdate(geoChecked())" id="g-go-sel">更新选中</button><button class="btn pri" onclick="geoUpdate(GEO.files.map(f=>f.file))">⤓ 全部更新</button></div>
  <div class="panel" id="p-g"><div class="panel-h"><b>更新输出</b><span id="p-g-t"></span><span class="sp"></span><button class="btn ghost sm" onclick="clearOut('g')">清空</button></div><pre id="out-g" class="mut">尚未执行。流程：下载 → 校验 MMDB 元数据 → 备份为 *.bak → 替换 → 重启</pre></div>
 </div>
</section>
<section class="tab" id="t-logs">
 <div class="card">
  <div class="toolbar">
   <span class="seg" id="lg-src"><button data-s="live" class="on">实时日志</button><button data-s="journal">systemd 日志</button></span>
   <span class="tbg" id="lg-live-tb">
   <select id="lg-level" title="日志级别"><option value="debug">debug</option><option value="info">info</option><option value="warning">warning</option><option value="error">error</option></select>
   <span class="search"><input type="search" id="lgq" placeholder="过滤关键字（本地）" autocomplete="off"></span>
   <label class="switch"><input type="checkbox" id="lg-pause"><i></i>暂停</label>
   <span class="sp"></span><span class="info" id="lg-info"></span>
   <button class="btn" onclick="LG.items=[];renderLiveLogs()">清空</button>
   </span>
   <span class="tbg" id="lg-journal-tb" style="display:none">
   <select id="logn"><option value="100">最近 100 行</option><option value="300" selected>最近 300 行</option><option value="1000">最近 1000 行</option><option value="2000">最近 2000 行</option></select>
   <span class="search"><input type="search" id="logq" placeholder="过滤关键字（本地）" autocomplete="off"></span>
   <label class="switch"><input type="checkbox" id="logauto"><i></i>自动刷新 5s</label>
   <span class="sp"></span><span class="info" id="log-info"></span>
   <button class="btn" onclick="loadLogs()">↻ 刷新</button>
   </span></div>
  <div class="llog" id="llog"></div>
  <pre id="logs" class="log" style="display:none"></pre>
 </div>
</section>
<section class="tab" id="t-config">
 <div class="card">
  <div class="toolbar"><span id="cf-info" class="info"></span><span class="search"><input type="search" id="cfq" placeholder="过滤行（本地）" autocomplete="off"></span><span class="sp"></span>
   <button class="btn" onclick="copyText(CFG_RAW,'config.yaml')">⧉ 复制</button><button class="btn" onclick="loadConfig()">↻ 刷新</button></div>
  <div class="mut" style="font-size:12px;margin-bottom:8px">只读。由 订阅 + override.yaml 合并生成；要修改请到「规则」或「override」页。</div>
  <pre id="cfg" style="max-height:75vh"></pre>
 </div>
</section>
</main>
<div id="toast"></div>
<div class="modal" id="modal" onclick="if(event.target===this)closeModal()"><div class="modal-c"><div class="modal-h"><b id="modal-t"></b><span class="sp"></span><button class="btn ghost sm" onclick="closeModal()">✕</button></div><div class="modal-b" id="modal-b"></div><div class="modal-f" id="modal-f"></div></div></div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function api(p,body){const r=await fetch(p,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});const j=await r.json();if(j.error)throw new Error(j.error);return j}
function toast(m,bad){const t=$('#toast');t.textContent=m;t.className=bad?'bad':'';t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',3500)}
function busy(on){document.querySelectorAll('button.btn').forEach(b=>b.disabled=on)}
function fmtDiff(d){return d.split('\n').map(l=>{const c=l.startsWith('+')?'b':l.startsWith('-')?'a':l.startsWith('@@')?'h':'';return c?`<span class="${c}">${esc(l)}</span>`:esc(l)}).join('\n')}
function diffStat(d){if(!d)return'';let a=0,b=0;for(const l of d.split('\n')){if(l.startsWith('+')&&!l.startsWith('+++'))b++;else if(l.startsWith('-')&&!l.startsWith('---'))a++}return`<span class="ok">+${b}</span> <span class="bad">−${a}</span>`}
function fmtBytes(n){return n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(2)+' MB'}
function fmtDur(ms){if(ms<0||isNaN(ms))return'-';const s=Math.floor(ms/1000),d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?`${d} 天 ${h} 小时`:h?`${h} 小时 ${m} 分`:m?`${m} 分钟`:`${s} 秒`}
function rel(epoch){const ms=Date.now()-epoch*1000;return ms<60000?'刚刚':fmtDur(ms)+'前'}
function parseTs(t){const m=/(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/.exec(t||'');return m?new Date(m[1]+'T'+m[2]):null}
function panel(id,state,title){const p=$('#p-'+id);if(p){p.className='panel'+(state?' '+state:'');}const t=$('#p-'+id+'-t');if(t)t.innerHTML=title||''}
function clearOut(id){$('#out-'+id).textContent='';$('#out-'+id).className='mut';panel(id,'')}
function stickTop(){const h=document.querySelector('.top').offsetHeight;document.querySelectorAll('.toolbar').forEach(e=>e.style.top=h+'px');const tb=$('#t-conns .toolbar');$('#cn-h').style.top=(h+(tb?tb.offsetHeight:0))+'px'}
async function copyText(t,what){try{await navigator.clipboard.writeText(t);toast(`已复制 ${what}`)}catch(e){toast('复制失败: '+e.message,1)}}
const LOADERS={rules:loadRules,override:loadOverride,backups:loadBackups,update:loadUpdate,proxies:loadProxies,logs:loadLogsTab,config:loadConfig,status:loadStatus,conns:cnStart,rtrules:loadRtRules,settings:loadSettings};
let CUR='status';
function showTab(t,push=true){const b=document.querySelector(`nav button[data-t="${t}"]`);if(!b)return;document.querySelectorAll('nav button,.tab').forEach(x=>x.classList.remove('on'));b.classList.add('on');$('#t-'+t).classList.add('on');stickTop();if(push)history.replaceState(null,'','#'+t);CUR=t;if(t!=='conns'&&t!=='proxies')cnStop();if(t==='status')requestAnimationFrame(liveCharts);LOADERS[t]()}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>showTab(b.dataset.t));
window.addEventListener('hashchange',()=>showTab(location.hash.slice(1),false));
window.addEventListener('resize',stickTop);

/* ---------- status ---------- */
async function loadStatus(){try{const s=await api('/api/status');const sv=s.service||{};const act=sv.ActiveState==='active';
 $('#hstate').innerHTML=`<span class="dot ${act?'ok live':'bad'}"></span>${act?'运行中':esc(sv.ActiveState||'未知')}`;
 {const d=s.dashboard,a=$('#dash');if(d?.url){const v={host:location.hostname,port:d.port||'',secret:d.secret||''};a.href=d.url.replace(/\{(host|port|secret)\}/g,(_,k)=>encodeURIComponent(v[k]));a.textContent=`${d.name} ↗`;a.style.display=''}else a.style.display='none'}
 const ver=s.version?.version,since=parseTs(sv.ActiveEnterTimestamp),now=parseTs(s.now)||new Date();
 $('#h-big').className='big '+(act?'ok':'bad');$('#h-t').textContent=act?'mihomo 运行中':`mihomo ${sv.ActiveState||'未知'} (${sv.SubState||'-'})`;
 $('#h-s').textContent=[ver?(/^v/i.test(ver)?ver:`v${ver}`):(s.version?.error?'controller 不可达':''),act&&sv.MainPID?`PID ${sv.MainPID}`:'',act&&since?`已运行 ${fmtDur(now-since)}`:'',`重启 ${sv.NRestarts??0} 次`].filter(Boolean).join(' · ');
 $('#b-start').style.display=act?'none':'';$('#b-stop').style.display=act?'':'none';
 const tile=(k,v,c='',sub='')=>`<div class="tile ${c}"><b>${k}</b><span>${v}</span>${sub?`<small>${sub}</small>`:''}</div>`;
 $('#stat').innerHTML=tile('服务状态',`${esc(sv.ActiveState)} · ${esc(sv.SubState)}`,act?'ok':'bad',sv.ActiveEnterTimestamp?'自 '+esc(sv.ActiveEnterTimestamp.replace(/^\w+ /,'').replace(/ \w+$/,'')):'')
 +tile('内核版本',esc(ver||s.version?.error||'-'),ver?'acc':'bad',s.version?.meta?'meta':'')
 +tile('config.yaml',s.config?fmtBytes(s.config.size):'缺失',s.config?'cyan':'bad',s.config?`${rel(s.config.mtime)}更新 · ${s.config.mtime_str}`:'')
 +tile('override.yaml',s.override?fmtBytes(s.override.size):'无','pur',s.override?`${rel(s.override.mtime)}修改 · ${s.override.mtime_str}`:'')
 +tile('备份',`${s.backups} 份`,'warn','保留最近 20 份')
 +tile('订阅地址',`<span class="mono" style="font-size:12px">${esc(s.sub_url||'-')}</span>`,'')
 +tile('controller',`<span class="mono" style="font-size:12px">${esc(s.controller||'-')}</span>`,'')
 +tile('服务器时间',esc(s.now),'');
 stickTop();loadMode();
}catch(e){$('#hstate').innerHTML=`<span class="dot bad"></span>状态获取失败`;$('#h-t').textContent='状态获取失败';$('#h-s').textContent=e.message}}
async function doUpdate(){if(!confirm('下载订阅、合并 override、校验后替换 config 并重启服务？'))return;busy(1);panel('status','busy','<span class="spin"></span>更新中…');$('#out-status').className='mut';$('#out-status').textContent='下载订阅 → 合并 → 校验 → 重启…';
 try{const r=await api('/api/update',{});panel('status',r.ok?'ok':'bad',`rc=${r.rc} · ${r.seconds}s`);$('#out-status').className=r.ok?'ok':'bad';$('#out-status').textContent=r.output;toast(r.ok?'更新完成':'更新失败',!r.ok)}catch(e){panel('status','bad','');$('#out-status').className='bad';$('#out-status').textContent=e.message}finally{busy(0);loadStatus()}}
async function svc(a){busy(1);panel('status','busy',`<span class="spin"></span>${a}…`);try{const r=await api('/api/service/'+a,{});panel('status',r.ok?'ok':'bad',a);$('#out-status').className=r.ok?'ok':'bad';$('#out-status').textContent=r.output;toast(a+(r.ok?' 完成':' 失败'),!r.ok)}catch(e){panel('status','bad','');$('#out-status').className='bad';$('#out-status').textContent=e.message}finally{busy(0);loadStatus()}}

/* ---------- rules ---------- */
const RTG=[['域名',['DOMAIN','DOMAIN-SUFFIX','DOMAIN-KEYWORD','DOMAIN-REGEX','GEOSITE']],['IP',['IP-CIDR','IP-CIDR6','IP-SUFFIX','IP-ASN','GEOIP','SRC-IP-CIDR']],['端口 / 进程',['DST-PORT','SRC-PORT','PROCESS-NAME','PROCESS-PATH','NETWORK']],['规则集',['RULE-SET']],['其它',['MATCH','RAW']]];
const RT=RTG.flatMap(g=>g[1]);const RTN={RAW:'原文 (完整规则行)',MATCH:'MATCH (兜底)'};
const IPT=new Set(['IP-CIDR','IP-CIDR6','IP-SUFFIX','IP-ASN','GEOIP','SRC-IP-CIDR']);
const TCLS=t=>t==='RAW'?'c-raw':t==='MATCH'?'c-match':t==='RULE-SET'?'c-rs':IPT.has(t)?'c-ip':t.startsWith('DOMAIN')||t==='GEOSITE'?'c-dom':'c-proc';
const PH={'DOMAIN':'www.example.com','DOMAIN-SUFFIX':'example.com','DOMAIN-KEYWORD':'google','DOMAIN-REGEX':'^.+\\.example\\.com$','GEOSITE':'youtube','IP-CIDR':'192.168.1.0/24','IP-CIDR6':'2001:db8::/32','IP-SUFFIX':'8.8.8.8/24','IP-ASN':'13335','GEOIP':'CN','SRC-IP-CIDR':'192.168.1.100/32','DST-PORT':'443','SRC-PORT':'7777','PROCESS-NAME':'curl','PROCESS-PATH':'/usr/bin/curl','NETWORK':'udp','RAW':'完整规则行，如 AND,((DOMAIN,a.com),(NETWORK,UDP)),DIRECT'};
const BUILTIN=['DIRECT','REJECT','REJECT-DROP'];
let RULES={prepend:[],append:[]},META={policies:[],rule_sets:[],current:[]},DIRTY=false;
const attr=v=>esc(v).replace(/"/g,'&quot;');
function parseRule(s){const p=String(s).split(',').map(x=>x.trim());const t=p[0].toUpperCase();
 if(t==='MATCH'&&p.length===2)return{t,v:'',pol:p[1],nr:false};
 if(RT.includes(t)&&t!=='RAW'&&p.length>=3&&p.slice(3).every(x=>x==='no-resolve'))return{t,v:p[1],pol:p[2],nr:p.includes('no-resolve')};
 return{t:'RAW',v:String(s),pol:'',nr:false}}
function buildRule(r){if(r.t==='RAW')return r.v.trim();if(r.t==='MATCH')return `MATCH,${r.pol.trim()}`;return [r.t,r.v.trim(),r.pol.trim()].concat(r.nr?['no-resolve']:[]).join(',')}
function ruleOk(r){if(r.t==='RAW')return !!r.v.trim();if(r.t==='MATCH')return !!r.pol.trim();return !!r.v.trim()&&!!r.pol.trim()}
function selOpts(groups,cur,custom){let h=`<option value="" disabled ${cur?'':'selected'}>— 选择 —</option>`;const known=[];
 for(const [l,a] of groups){if(!a.length)continue;h+=`<optgroup label="${attr(l)}">`+a.map(x=>{known.push(x);return `<option value="${attr(x)}" ${x===cur?'selected':''}>${esc(RTN[x]||x)}</option>`}).join('')+'</optgroup>'}
 if(cur&&!known.includes(cur))h+=`<optgroup label="其它"><option value="${attr(cur)}" selected>${esc(cur)}</option></optgroup>`;
 if(custom)h+=`<option value="__custom">✎ 自定义…</option>`;return h}
function polDot(p){return !p?'':BUILTIN[0]===p?'ok':p.startsWith('REJECT')?'bad':'acc'}
function setDirty(d){DIRTY=d;const b=$('#ru-dirty');b.textContent=d?'● 未保存':'已同步';b.className='badge'+(d?' dirty':'')}
function ruleRow(k,i,r){const n=RULES[k].length,ok=ruleOk(r),line=buildRule(r);const mode=r.t==='RAW'?'m-raw':r.t==='MATCH'?'m-match':IPT.has(r.t)?'':'no-nr';
 const dup=ok&&META.current.includes(line)&&!(k==='prepend'&&META.loadedPrepend.includes(line))&&!(k==='append'&&META.loadedAppend.includes(line));
 const vfield=r.t==='RULE-SET'?`<select class="rv">${selOpts([['规则集 (rule-providers)',META.rule_sets]],r.v,true)}</select>`:`<input type="text" class="rv" value="${attr(r.v)}" placeholder="${attr(PH[r.t]||'值')}" spellcheck="false" autocomplete="off">`;
 return `<div class="rule ${TCLS(r.t)} ${mode} ${ok?'':'err'}" data-k="${k}" data-i="${i}">
 <span class="n">${i+1}</span>
 <select class="rt" title="规则类型">${selOpts(RTG,r.t,false)}</select>
 <span class="rvw">${vfield}</span>
 <span class="arr">→</span>
 <span class="pol"><span class="dot ${polDot(r.pol)}"></span><select class="rp" title="出站策略">${selOpts([['内置',BUILTIN],['策略组',META.policies]],r.pol,true)}</select></span>
 <label class="nrw" title="不解析域名即判定（IP 类规则建议勾选）"><input type="checkbox" class="rn" ${r.nr?'checked':''}>no-resolve</label>
 <span class="ops"><button class="btn" title="上移" onclick="mvRule('${k}',${i},-1)" ${i===0?'disabled':''}>↑</button><button class="btn" title="下移" onclick="mvRule('${k}',${i},1)" ${i===n-1?'disabled':''}>↓</button><button class="btn" title="复制一条" onclick="dupRule('${k}',${i})">⧉</button><button class="btn dan" title="删除" onclick="delRule('${k}',${i})">✕</button></span>
 <span class="pv ${ok?(dup?'dup':''):'bad'}">${ok?esc(line)+(dup?'　⚠ 订阅规则中已有相同一条':''):'⚠ 请补全'+(r.t==='RAW'?'规则行':r.t==='MATCH'?'策略':!r.v.trim()?'值':'策略')}</span></div>`}
function renderRules(k){const el=$('#rl-'+k);el.innerHTML=RULES[k].map((r,i)=>ruleRow(k,i,r)).join('')||`<div class="rempty">暂无${k==='prepend'?'前置':'后置'}规则，点下方按钮添加</div>`;$('#cnt-'+k).textContent=RULES[k].length;previewRules()}
function rerenderRow(k,i){const old=$(`#rl-${k} .rule[data-i="${i}"]`);if(!old)return renderRules(k);const d=document.createElement('div');d.innerHTML=ruleRow(k,i,RULES[k][i]);old.replaceWith(d.firstElementChild);previewRules()}
function previewRules(){const y=k=>RULES[k].length?`rules-${k}:\n`+RULES[k].map(r=>'  - '+buildRule(r)).join('\n')+'\n':'';$('#ru-yaml').textContent=(y('prepend')+y('append'))||'(空：两个块都会从 override.yaml 移除)'}
function addRule(k){RULES[k].push({t:'DOMAIN-SUFFIX',v:'',pol:'',nr:false});setDirty(true);renderRules(k);const rows=$('#rl-'+k).querySelectorAll('.rule');const last=rows[rows.length-1];last?.scrollIntoView?.({block:'nearest',behavior:'smooth'});last?.querySelector('.rv')?.focus()}
function delRule(k,i){const r=RULES[k][i];if(ruleOk(r)&&!confirm(`删除规则「${buildRule(r)}」？`))return;RULES[k].splice(i,1);setDirty(true);renderRules(k)}
function dupRule(k,i){RULES[k].splice(i+1,0,{...RULES[k][i]});setDirty(true);renderRules(k)}
function mvRule(k,i,d){const a=RULES[k],j=i+d;if(j<0||j>=a.length)return;[a[i],a[j]]=[a[j],a[i]];setDirty(true);renderRules(k)}
function onRuleChange(e){const row=e.target.closest('.rule');if(!row)return;const k=row.dataset.k,i=+row.dataset.i,r=RULES[k][i];const el=e.target;
 if(el.classList.contains('rt')){const nt=el.value;if(nt==='RAW'&&r.t!=='RAW')r.v=ruleOk(r)?buildRule(r):'';else if(r.t==='RAW'&&nt!=='RAW'){const p=parseRule(r.v);if(p.t!=='RAW'){r.v=p.v;r.pol=p.pol;r.nr=p.nr}else r.v=''}
  if(nt==='RULE-SET'&&r.t!=='RULE-SET'&&!META.rule_sets.includes(r.v))r.v='';r.t=nt;if(!IPT.has(nt))r.nr=false;setDirty(true);rerenderRow(k,i);return}
 if(el.classList.contains('rp')||el.classList.contains('rv')&&el.tagName==='SELECT'){let v=el.value;if(v==='__custom'){v=prompt(el.classList.contains('rp')?'输入策略名（策略组名 / 节点名 / DIRECT…）':'输入规则集名',el.classList.contains('rp')?r.pol:r.v)?.trim();if(!v){el.value=el.classList.contains('rp')?r.pol:r.v;return}}
  if(el.classList.contains('rp'))r.pol=v;else r.v=v;setDirty(true);rerenderRow(k,i);return}
 if(el.classList.contains('rv'))r.v=el.value;else if(el.classList.contains('rn'))r.nr=el.checked;setDirty(true);
 if(e.type==='change')rerenderRow(k,i);else{const ok=ruleOk(r);row.classList.toggle('err',!ok);const pv=row.querySelector('.pv');pv.className='pv'+(ok?'':' bad');pv.textContent=ok?buildRule(r):'⚠ 请补全'+(r.t==='RAW'?'规则行':'值');previewRules()}}
document.addEventListener('input',e=>{if(e.target.matches('.rule input[type=text]'))onRuleChange(e)});
document.addEventListener('change',e=>{if(e.target.closest('.rule'))onRuleChange(e)});
window.addEventListener('beforeunload',e=>{if(DIRTY){e.preventDefault();e.returnValue=''}});
async function loadRules(discard){if(discard&&DIRTY&&!confirm('放弃未保存的修改并重新加载？'))return;try{const r=await api('/api/rules');
 META={policies:r.policies.filter(p=>!BUILTIN.includes(p)),rule_sets:r.rule_sets,current:r.current_rules,loadedPrepend:r.prepend,loadedAppend:r.append};
 RULES={prepend:r.prepend.map(parseRule),append:r.append.map(parseRule)};
 $('#ru-info').textContent=r.info?`override.yaml · 最后修改 ${r.info.mtime_str}`:'override.yaml 不存在（保存时创建）';$('#ru-cur').textContent=r.current_rules.map((x,i)=>String(i+1).padStart(3)+'  '+x).join('\n');
 const ins=$('#ru-insert');if(r.insert.length){ins.style.display='block';ins.textContent=`另有 ${r.insert.length} 条 rules-insert（锚点插入）未在表单中显示，保存时原样保留；如需修改请到 override.yaml 页。`}else ins.style.display='none';
 setDirty(false);renderRules('prepend');renderRules('append');stickTop()}catch(e){$('#out-rules').className='bad';$('#out-rules').textContent=e.message}}
async function saveRules(apply){const bad=[];const col=k=>RULES[k].map((r,i)=>{if(!ruleOk(r))bad.push(`${k==='prepend'?'前置':'后置'}规则 #${i+1} 不完整`);return buildRule(r)});
 const prepend=col('prepend'),append=col('append');if(bad.length){$('#out-rules').className='bad';$('#out-rules').textContent=bad.join('\n');toast('请补全标红的规则',1);document.querySelector('.rule.err')?.scrollIntoView?.({block:'center',behavior:'smooth'});return}
 if(!DIRTY&&!apply){toast('没有改动');return}
 if(apply&&!confirm('保存规则到 override.yaml 并立即更新订阅、重启服务？'))return;busy(1);$('#out-rules').className='mut';$('#out-rules').textContent=apply?'保存并应用中（需下载订阅）…':'保存中…';
 try{const r=await api('/api/rules/save',{prepend,append,apply});$('#out-rules').className=r.ok?'ok':'bad';$('#out-rules').textContent='已保存（旧文件备份为 override.yaml.bak.*）'+(r.apply?`\n\n[apply rc=${r.apply.rc}]\n${r.apply.output}`:'');toast(r.ok?(apply?'已保存并应用':'已保存'):'应用失败',!r.ok);setDirty(false);loadRules()}catch(e){$('#out-rules').className='bad';$('#out-rules').textContent=e.message}finally{busy(0);loadStatus()}}


/* ---------- override ---------- */
let OV_LOADED='',OV_DIRTY=false;
function ovDirty(d){OV_DIRTY=d;const b=$('#ov-dirty');b.textContent=d?'● 未保存':'已同步';b.className='badge'+(d?' dirty':'')}
function ovMeta(){const v=$('#ov').value;$('#ov-meta').textContent=`${v.split('\n').length} 行 · ${new TextEncoder().encode(v).length} B`}
$('#ov').addEventListener('input',()=>{ovDirty($('#ov').value!==OV_LOADED);ovMeta()});
$('#ov').addEventListener('keydown',e=>{if(e.key==='Tab'){e.preventDefault();const t=e.target,s=t.selectionStart,n=t.selectionEnd;t.value=t.value.slice(0,s)+'  '+t.value.slice(n);t.selectionStart=t.selectionEnd=s+2;t.dispatchEvent(new Event('input'))}});
window.addEventListener('beforeunload',e=>{if(OV_DIRTY){e.preventDefault();e.returnValue=''}});
async function loadOverride(discard){if(discard&&OV_DIRTY&&!confirm('放弃未保存的修改并重新加载？'))return;const r=await api('/api/override');OV_LOADED=r.content;$('#ov').value=r.content;ovDirty(false);ovMeta();$('#ov-info').textContent=r.info?`override.yaml · 最后修改 ${r.info.mtime_str}`:'override.yaml 不存在（保存时创建）';stickTop()}
async function validateOverride(){busy(1);panel('ov','busy','<span class="spin"></span>下载订阅并合并校验中…');$('#out-ov').className='mut';$('#out-ov').textContent='';$('#ov-diff').innerHTML='';$('#ov-diff-stat').innerHTML='';
 try{const r=await api('/api/override/validate',{content:$('#ov').value});panel('ov',r.ok?'ok':'bad',`阶段: ${esc(r.stage)} · ${r.ok?'通过':'失败'}`);$('#out-ov').className=r.ok?'ok':'bad';$('#out-ov').textContent=r.output;$('#ov-diff').innerHTML=fmtDiff(r.diff||'(无差异)');$('#ov-diff-stat').innerHTML=diffStat(r.diff);if(r.diff)$('#ov-diff').closest('details').open=true;toast(r.ok?'校验通过':'校验失败',!r.ok)}catch(e){panel('ov','bad','');$('#out-ov').className='bad';$('#out-ov').textContent=e.message}finally{busy(0)}}
async function saveOverride(apply){if(!OV_DIRTY&&!apply){toast('没有改动');return}if(apply&&!confirm('保存 override.yaml 并立即更新订阅、重启服务？'))return;busy(1);panel('ov','busy','<span class="spin"></span>'+(apply?'保存并应用中…':'保存中…'));$('#out-ov').className='mut';$('#out-ov').textContent='';
 try{const r=await api('/api/override/save',{content:$('#ov').value,apply});panel('ov',r.ok?'ok':'bad',apply?`已保存 · apply rc=${r.apply?.rc}`:'已保存');$('#out-ov').className=r.ok?'ok':'bad';$('#out-ov').textContent='已保存（旧文件备份为 override.yaml.bak.*）'+(r.apply?`\n\n${r.apply.output}`:'');toast(r.ok?(apply?'已保存并应用':'已保存'):'应用失败',!r.ok);ovDirty(false);loadOverride()}catch(e){panel('ov','bad','');$('#out-ov').className='bad';$('#out-ov').textContent=e.message}finally{busy(0);loadStatus()}}

/* ---------- backups ---------- */
async function loadBackups(){const l=await api('/api/backups');$('#bk-cnt').textContent=l.length;
 $('#bk').innerHTML=l.map((b,i)=>{const m=/(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/.exec(b.name);const when=m?`${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`:b.mtime_str;const ago=rel((parseTs(when)||new Date(b.mtime*1000)).getTime()/1000);
  return `<div class="bki" data-n="${esc(b.name)}"><input type="checkbox" class="ck" onchange="bkSel()"><span class="ic">${i===0?'★':'▤'}</span><span class="nm">${esc(b.name)}<small>${when} · ${ago}${i===0?' · 最新':''}</small></span><span class="sz">${fmtBytes(b.size)}</span>
  <span class="act"><button class="btn sm" onclick="bkDiff('${b.name}')">对比</button><button class="btn sm" onclick="bkRestore('${b.name}')">恢复</button><button class="btn sm ghost" title="删除此备份" onclick="bkDelete(['${b.name}'])">✕</button></span></div>`}).join('')||'<div class="empty">暂无备份。更新订阅时会自动生成。</div>';BK_LIST=l;$('#bk-all').checked=false;bkSel();stickTop()}
let BK_LIST=[];
function bkChecked(){return [...document.querySelectorAll('#bk .bki .ck:checked')].map(c=>c.closest('.bki').dataset.n)}
function bkSel(){const n=bkChecked();document.querySelectorAll('#bk .bki').forEach(x=>x.classList.toggle('chk',!!x.querySelector('.ck')?.checked));$('#bk-selinfo').textContent=n.length?`已选 ${n.length} / ${BK_LIST.length}`:'未选择';$('#bk-delsel').disabled=!n.length;const all=$('#bk-all');all.checked=n.length>0&&n.length===BK_LIST.length;all.indeterminate=n.length>0&&n.length<BK_LIST.length}
function bkToggleAll(on){document.querySelectorAll('#bk .bki .ck').forEach(c=>c.checked=on);bkSel()}
async function bkDelete(names,what){if(!names.length)return;const latest=BK_LIST[0]?.name;const msg=names.length===1?`删除备份 ${names[0]}？`:`删除${what||'选中的'} ${names.length} 个备份？`;if(!confirm(msg+(names.includes(latest)?'\n\n注意：包含最新一份备份。':'')+'\n此操作不可撤销。'))return;busy(1);
 try{const r=await api('/api/backups/delete',{names});toast(r.ok?`已删除 ${r.deleted.length} 个备份`:'部分删除失败',!r.ok);if(!r.ok){panel('bk','bad');$('#bk-diff-t').textContent='删除';$('#bk-diff').className='bad';$('#bk-diff').textContent=r.output}
  if(r.deleted.some(n=>$('.bki.sel')?.dataset.n===n)){$('#bk-diff').innerHTML='';$('#bk-diff-t').textContent='差异';$('#bk-diff-stat').textContent=''}}catch(e){toast(e.message,1)}finally{busy(0);loadBackups();loadStatus()}}
function bkDeleteSelected(){bkDelete(bkChecked())}
function bkCleanup(){const keep=5;const old=BK_LIST.slice(keep).map(b=>b.name);if(!old.length){toast(`备份不足 ${keep} 份，无需清理`);return}bkDelete(old,`最新 ${keep} 份以外的`)}
async function bkDiff(n){document.querySelectorAll('#bk .bki').forEach(x=>x.classList.toggle('sel',x.dataset.n===n));panel('bk','busy');$('#bk-diff-t').textContent=n+' → 当前';$('#bk-diff').className='diff';$('#bk-diff').innerHTML='<span class="spin"></span>对比中…';
 try{const r=await api(`/api/backups/${n}/diff`);panel('bk','');$('#bk-diff').innerHTML=fmtDiff(r.diff||'(与当前 config.yaml 相同)');$('#bk-diff-stat').innerHTML=diffStat(r.diff)}catch(e){panel('bk','bad');$('#bk-diff').textContent=e.message}}
async function bkRestore(n){if(!confirm(`恢复 ${n} 为当前 config.yaml 并重启服务？`))return;busy(1);panel('bk','busy');$('#bk-diff-t').textContent='恢复 '+n;$('#bk-diff-stat').textContent='';$('#bk-diff').className='diff mut';$('#bk-diff').innerHTML='<span class="spin"></span>校验 → 备份当前 → 替换 → 重启…';
 try{const r=await api(`/api/backups/${n}/restore`,{});panel('bk',r.ok?'ok':'bad');$('#bk-diff').className=r.ok?'ok':'bad';$('#bk-diff').textContent=r.output;toast(r.ok?'已恢复':'恢复失败',!r.ok)}catch(e){panel('bk','bad');$('#bk-diff').className='bad';$('#bk-diff').textContent=e.message}finally{busy(0);loadBackups();loadStatus()}}

/* ---------- 内置面板：mihomo controller（经管理页后端转发） ---------- */
const MH='/api/mihomo',enc=encodeURIComponent;
async function mh(p,method='GET',body){const o={method};if(body!==undefined){o.headers={'Content-Type':'application/json'};o.body=JSON.stringify(body)}
 const r=await fetch(MH+p,o);const t=await r.text();let j=null;if(t){try{j=JSON.parse(t)}catch(e){}}
 if(!r.ok)throw new Error((j&&(j.message||j.error))||t||('HTTP '+r.status));return j??{}}
/* NDJSON 流：断开后自动重连（指数退避）。onMsg 返回 false 表示这是一条错误消息，不重置退避 */
function openStream(url,onMsg,onErr){let stopped=false,ctl=null,delay=1000;
 (async()=>{while(!stopped){ctl=new AbortController();try{const r=await fetch(url,{signal:ctl.signal});if(!r.ok){let m='HTTP '+r.status;try{m=(await r.json()).message||m}catch(e){}throw new Error(m)}
   const rd=r.body.getReader(),dec=new TextDecoder();let buf='';
   for(;;){const {value,done}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});let i;while((i=buf.indexOf('\n'))>=0){const l=buf.slice(0,i).trim();buf=buf.slice(i+1);if(!l)continue;let m;try{m=JSON.parse(l)}catch(e){continue}if(onMsg(m)!==false)delay=1000}}}
  catch(e){if(stopped)return;onErr&&onErr(e.message)}
  if(stopped)return;await new Promise(r=>setTimeout(r,delay));delay=Math.min(delay*2,15000)}})();
 return{stop(){stopped=true;ctl&&ctl.abort()}}}
function modal(title,body,foot){$('#modal-t').textContent=title;$('#modal-b').innerHTML=body;$('#modal-f').innerHTML=foot||'';$('#modal').classList.add('on')}
function closeModal(){$('#modal').classList.remove('on')}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});
function fmtSpd(n){return fmtBytes(Math.round(n||0))+'/s'}
function fmtShort(n){return n<1024?Math.round(n)+' B':n<1048576?(n/1024).toFixed(n<10240?1:0)+' KB':n<1073741824?(n/1048576).toFixed(n<10485760?1:0)+' MB':(n/1073741824).toFixed(2)+' GB'}
function fmtDurS(ms){const s=Math.max(0,Math.floor(ms/1000));return s<60?s+'s':s<3600?`${Math.floor(s/60)}m${s%60}s`:s<86400?`${Math.floor(s/3600)}h${Math.floor(s%3600/60)}m`:`${Math.floor(s/86400)}d${Math.floor(s%86400/3600)}h`}
function fmtUpd(iso){const d=new Date(iso||0);if(isNaN(d)||d.getFullYear()<2000)return'-';return d.toLocaleString('zh-CN',{hour12:false})+' · '+rel(d.getTime()/1000)}
function hl(t,q){const s=esc(t);if(!q)return s;const re=new RegExp(esc(q).replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'ig');return s.replace(re,m=>`<mark>${m}</mark>`)}
async function btnBusy(b,fn){const h=b?b.innerHTML:'';if(b){b.disabled=true;b.innerHTML='<span class="spin"></span>'+h}try{return await fn()}finally{if(b){b.disabled=false;b.innerHTML=h}}}

/* ---------- 代理模式 ---------- */
const MODES=[['rule','规则'],['global','全局'],['direct','直连']];let MODE='';
function modeBtns(){return MODES.map(([k,l])=>`<button data-mode="${k}" class="${MODE.toLowerCase()===k?'on':''}">${l}</button>`).join('')}
function renderModeSw(){document.querySelectorAll('.modesw').forEach(e=>e.innerHTML=modeBtns())}
async function loadMode(){try{const c=await mh('/configs');MODE=String(c.mode||'');renderModeSw()}catch(e){}}
document.addEventListener('click',async e=>{const b=e.target.closest('.modesw button');if(!b)return;const m=b.dataset.mode;if(m===MODE.toLowerCase())return;
 try{await mh('/configs','PATCH',{mode:m});MODE=m;renderModeSw();toast('代理模式：'+MODES.find(x=>x[0]===m)[1]);if(CUR==='proxies')renderProxies()}catch(ex){toast(ex.message,1)}});

/* ---------- 实时概览：流量 / 内存 / 连接数 ---------- */
const LIVE={tr:[],mem:[],up:0,down:0,upT:null,downT:null,conns:null,inuse:null,s:null,ok:null,raf:0};
function liveState(ok,msg){if(LIVE.ok===ok&&ok)return;LIVE.ok=ok;$('#live-st').innerHTML=ok?'<span class="ok">●</span> 已连接 controller':`<span class="bad">● 无法连接 controller</span>${msg?' · '+esc(msg):''}`;$('#hspd').style.display=ok?'':'none'}
function liveStart(){if(LIVE.s)return;LIVE.s=openStream('/api/stream/overview',m=>{
  if(m.error){liveState(false,m.error);return false}
  if(m.traffic){const t=m.traffic;LIVE.up=t.up||0;LIVE.down=t.down||0;LIVE.tr.push([LIVE.up,LIVE.down]);if(LIVE.tr.length>60)LIVE.tr.shift()}
  if(m.memory&&m.memory.inuse){LIVE.inuse=m.memory.inuse;LIVE.mem.push(LIVE.inuse);if(LIVE.mem.length>60)LIVE.mem.shift()}
  if(m.conns){LIVE.conns=m.conns.count;LIVE.upT=m.conns.uploadTotal;LIVE.downT=m.conns.downloadTotal}
  liveState(true);if(!LIVE.raf)LIVE.raf=requestAnimationFrame(liveRender)},msg=>liveState(false,msg))}
function liveStop(){if(LIVE.s){LIVE.s.stop();LIVE.s=null}}
function liveRender(){LIVE.raf=0;$('#hspd-u').textContent='↑ '+fmtSpd(LIVE.up);$('#hspd-d').textContent='↓ '+fmtSpd(LIVE.down);
 $('#nav-conns').textContent=LIVE.conns??'';if(CUR!=='status')return;
 $('#lv-up').textContent=fmtSpd(LIVE.up);$('#lv-down').textContent=fmtSpd(LIVE.down);
 $('#lv-upt').textContent=LIVE.upT!=null?'累计 '+fmtBytes(LIVE.upT):'';$('#lv-downt').textContent=LIVE.downT!=null?'累计 '+fmtBytes(LIVE.downT):'';
 $('#lv-conns').textContent=LIVE.conns??'-';$('#lv-mem').textContent=LIVE.inuse!=null?fmtBytes(LIVE.inuse):'-';liveCharts()}
function niceMax(v){if(!(v>0))return 1024;const p=Math.pow(1024,Math.floor(Math.log(v)/Math.log(1024)));const n=v/p;return (n<=1?1:n<=2?2:n<=4?4:n<=8?8:n<=16?16:n<=32?32:n<=64?64:n<=128?128:n<=256?256:n<=512?512:1024)*p}
function drawChart(cv,series,fmt){const w=cv.clientWidth,h=cv.clientHeight;if(!w||!h)return;const dpr=window.devicePixelRatio||1;cv.width=w*dpr;cv.height=h*dpr;const c=cv.getContext('2d');c.scale(dpr,dpr);c.clearRect(0,0,w,h);
 const N=60,top=14,bot=h-2;let max=0;series.forEach(s=>s.data.forEach(v=>{if(v>max)max=v}));max=niceMax(max);
 c.font='10px ui-monospace,Menlo,monospace';c.lineWidth=1;
 for(let i=0;i<=2;i++){const y=Math.round(top+(bot-top)*i/2)+.5;c.strokeStyle='#262c37';c.beginPath();c.moveTo(0,y);c.lineTo(w,y);c.stroke();c.fillStyle='#8a91a0';c.fillText(fmt(max*(1-i/2)),4,y-3)}
 series.forEach(s=>{const d=s.data;if(d.length<2)return;const off=N-d.length,X=i=>(off+i)/(N-1)*w,Y=v=>bot-(bot-top)*(v/max);
  c.beginPath();d.forEach((v,i)=>i?c.lineTo(X(i),Y(v)):c.moveTo(X(i),Y(v)));c.strokeStyle=s.color;c.lineWidth=1.6;c.lineJoin='round';c.stroke();
  c.lineTo(X(d.length-1),bot);c.lineTo(X(0),bot);c.closePath();const g=c.createLinearGradient(0,top,0,bot);g.addColorStop(0,s.color+'40');g.addColorStop(1,s.color+'00');c.fillStyle=g;c.fill()})}
function liveCharts(){if(CUR!=='status')return;drawChart($('#ch-tr'),[{data:LIVE.tr.map(x=>x[0]),color:'#39c5cf'},{data:LIVE.tr.map(x=>x[1]),color:'#58a6ff'}],v=>fmtShort(v)+'/s');drawChart($('#ch-mem'),[{data:LIVE.mem,color:'#a371f7'}],fmtShort)}
window.addEventListener('resize',()=>requestAnimationFrame(liveCharts));

/* ---------- 代理 ---------- */
const PGT={Selector:['acc','手动'],URLTest:['cyan','自动测速'],Fallback:['warn','故障转移'],LoadBalance:['pur','负载均衡'],Relay:['','链式']};
const SELECTABLE=new Set(['Selector','URLTest','Fallback']);
const PXS={url:localStorage.getItem('px-url')||'https://www.gstatic.com/generate_204',to:+localStorage.getItem('px-to')||5000,autoclose:localStorage.getItem('px-autoclose')==='1',open:new Set(JSON.parse(localStorage.getItem('px-open')||'[]'))};
let PX={proxies:{},providers:{}};const LAT={},TESTING=new Set();
function isGroup(p){return !!(p&&Array.isArray(p.all))}
function lat(n,d=0){if(/^(REJECT|REJECT-DROP|PASS|COMPATIBLE)$/.test(n))return undefined;if(LAT[n]!==undefined)return LAT[n];const p=PX.proxies[n];if(!p)return undefined;if(isGroup(p)&&p.now&&d<6)return lat(p.now,d+1);
 const h=(p.extra&&p.extra[PXS.url]&&p.extra[PXS.url].history)||p.history;return h&&h.length?h[h.length-1].delay:undefined}
function latCls(d){return d===undefined?'':d<=0?'r':d<300?'g':d<800?'y':'r'}
function latTxt(d){return d===undefined?'—':d<=0?'超时':d+'ms'}
function pxGroups(){const P=PX.proxies;const order=((P.GLOBAL&&P.GLOBAL.all)||[]).filter(n=>isGroup(P[n]));const seen=new Set(order);
 Object.keys(P).forEach(n=>{if(isGroup(P[n])&&n!=='GLOBAL'&&!seen.has(n))order.push(n)});
 const gs=order.map(n=>P[n]).filter(g=>!g.hidden);if(MODE.toLowerCase()==='global'&&P.GLOBAL)gs.unshift(P.GLOBAL);return gs}
function pxNodes(g,q){let ns=(g.all||[]).slice();if(q&&!g.name.toLowerCase().includes(q))ns=ns.filter(n=>n.toLowerCase().includes(q));
 if($('#pxhide').checked)ns=ns.filter(n=>n===g.now||lat(n)!==0);
 const s=$('#pxsort').value;if(s==='name')ns.sort((a,b)=>a.localeCompare(b,'zh'));else if(s==='delay'){const k=n=>{const d=lat(n);return d>0?d:d===0?1e9:1e8};ns.sort((a,b)=>k(a)-k(b))}return ns}
/* 策略组 logo：取 proxy-groups 的 icon；加载失败或未配置时显示首字 */
function gIcon(p,cls=''){const u=String((p&&p.icon)||''),n=String((p&&p.name)||'?');const av=`<span class="av"${/^(https?:|data:image\/)/i.test(u)?' style="display:none"':''}>${esc(Array.from(n)[0]||'?')}</span>`;
 return `<span class="gic ${cls}">${av}${/^(https?:|data:image\/)/i.test(u)?`<img src="${attr(u)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.previousElementSibling.style.display='';this.remove()">`:''}</span>`}
const GAUGE='<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 14l4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></svg>';
const CHEV='<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>';
function ndHtml(g,n){const p=PX.proxies[n]||{},d=lat(n),on=g.now===n,sel=SELECTABLE.has(g.type);
 return `<div class="nd ${on?'on':''} ${sel?'sel':''}" data-n="${attr(n)}" title="${attr(n)}"><div class="nd-n">${isGroup(p)?gIcon(p,'sm'):''}<span>${esc(n)}</span></div><div class="nd-m"><span>${esc(p.type||'')}</span>${p.udp?'<span class="u">UDP</span>':''}${isGroup(p)?`<span class="sub">→ ${esc(p.now||'')}</span>`:''}<span class="sp"></span><button class="lat ${latCls(d)}" data-n="${attr(n)}" title="点击测速">${TESTING.has(n)?'<span class="spin"></span>':latTxt(d)}</button></div></div>`}
function latBar(ns){const c={g:0,y:0,r:0};ns.forEach(n=>{const k=latCls(lat(n));if(k)c[k]++});const t=ns.length||1;
 return `<span class="lbar" title="${c.g} 个低延迟 · ${c.y} 个中等 · ${c.r} 个超时或高延迟 · ${ns.length-c.g-c.y-c.r} 个未测">${['g','y','r'].map(k=>c[k]?`<i class="${k}" style="width:${(c[k]/t*100).toFixed(2)}%"></i>`:'').join('')}</span>`}
function renderProxies(){const q=$('#pxq').value.trim().toLowerCase();const gs=pxGroups();$('#px-ng').textContent=gs.length;
 const html=gs.map(g=>{const ns=pxNodes(g,q);if(q&&!ns.length&&!g.name.toLowerCase().includes(q))return'';const open=PXS.open.has(g.name)||!!q;const [,l]=PGT[g.type]||['',g.type];const d=lat(g.now);
  return `<div class="pgc ${open?'open':''}" data-g="${attr(g.name)}"><div class="pgc-h" data-act="toggle">${gIcon(g)}<b title="${attr(g.name)}">${esc(g.name)}</b><span class="cnt">${(g.all||[]).length}</span><span class="sp"></span><button class="pgc-t" data-act="gtest" title="测速此组">${TESTING.has('g:'+g.name)?'<span class="spin"></span>':GAUGE}</button></div>
  <div class="pgc-b" data-act="toggle"><div class="pgc-tags"><span class="gpill ${esc(g.type)}" title="${attr(g.type+' › '+(g.now||''))}">${esc(l)}<i>›</i><span class="nm">${esc(g.now||'-')}</span></span><span class="gpill spd idle" data-spd="${attr(g.name)}">0 B/s</span></div>${CHEV}</div>
  <div class="pgc-f" data-act="toggle">${ns.length>10?latBar(ns):`<span class="dots">${ns.map(n=>`<i class="${latCls(lat(n))} ${n===g.now?'on':''}" title="${attr(n)} · ${latTxt(lat(n))}"></i>`).join('')}</span>`}<span class="latv ${latCls(d)}">${g.now&&d!==undefined?(d>0?d:'超时'):''}</span></div>
  ${open?`<div class="nds">${ns.map(n=>ndHtml(g,n)).join('')||'<span class="mut" style="font-size:12px">无匹配节点</span>'}</div>`:''}</div>`}).join('');
 $('#px').innerHTML=html||`<div class="empty" style="grid-column:1/-1">${q?'没有匹配的节点':'没有策略组'}</div>`;
 $('#px-expand').textContent=gs.length&&gs.every(g=>PXS.open.has(g.name))?'全部折叠':'全部展开';pxSpeeds()}
/* 每个策略组当前的实时速度 = 经过它的连接的上下行速度之和（来自连接列表的轮询） */
function pxSpeeds(){if(CUR!=='proxies')return;const sp={};for(const r of CN.rows){const v=r.dls+r.uls;if(v)for(const g of new Set(r.chains))sp[g]=(sp[g]||0)+v}
 document.querySelectorAll('#px .gpill.spd').forEach(e=>{const v=sp[e.dataset.spd]||0;e.textContent=fmtSpd(v);e.classList.toggle('idle',!v)})}
function pxView(v){PXS.view=v;$('#px-view').querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));
 document.querySelectorAll('#t-proxies .px-g').forEach(e=>e.style.display=v==='groups'?'':'none');document.querySelectorAll('#t-proxies .px-p').forEach(e=>e.style.display=v==='providers'?'':'none');
 $('#pxq').placeholder=v==='groups'?'搜索节点 / 策略组':'搜索代理集合 / 节点';v==='groups'?renderProxies():renderProviders()}
$('#px-view').addEventListener('click',e=>{const b=e.target.closest('button[data-v]');if(b)pxView(b.dataset.v)});
function pxSaveOpen(){localStorage.setItem('px-open',JSON.stringify([...PXS.open]))}
function pxToggle(g){PXS.open.has(g)?PXS.open.delete(g):PXS.open.add(g);pxSaveOpen();renderProxies()}
function pxExpandAll(){const gs=pxGroups();if(gs.every(g=>PXS.open.has(g.name)))PXS.open.clear();else gs.forEach(g=>PXS.open.add(g.name));pxSaveOpen();renderProxies()}
$('#px').addEventListener('click',e=>{const card=e.target.closest('.pgc');if(!card)return;const g=card.dataset.g;
 const lb=e.target.closest('.lat');if(lb){pxTestOne(lb.dataset.n);return}
 const a=e.target.closest('[data-act]');if(a&&a.dataset.act==='gtest'){pxTestGroup(g);return}
 const nd=e.target.closest('.nd');if(nd){if(nd.classList.contains('sel'))pxSelect(g,nd.dataset.n);return}
 if(a&&a.dataset.act==='toggle')pxToggle(g)});
['pxsort','pxhide'].forEach(id=>$('#'+id).addEventListener('change',renderProxies));$('#pxq').addEventListener('input',()=>PXS.view==='providers'?renderProviders():renderProxies());
/* 订阅（proxy-providers）里的节点不一定出现在 /proxies 中，合并进来才有类型 / UDP / 延迟历史 */
function pxMerge(p,pv){const P=Object.assign({},p.proxies||{});Object.values(pv.providers||{}).forEach(v=>(v.proxies||[]).forEach(x=>{if(x&&x.name&&!P[x.name])P[x.name]=x}));return P}
async function pxRefresh(){try{const [p,pv]=await Promise.all([mh('/proxies'),mh('/providers/proxies').catch(()=>({providers:{}}))]);PX.proxies=pxMerge(p,pv);PX.providers=pv.providers||{}}catch(e){}renderProxies()}
async function loadProxies(){cnTimer();$('#px-url').value=PXS.url;$('#px-to').value=PXS.to;$('#px-autoclose').checked=PXS.autoclose;
 try{const [p,pv,c]=await Promise.all([mh('/proxies'),mh('/providers/proxies').catch(()=>({providers:{}})),mh('/configs').catch(()=>null)]);
  PX.proxies=pxMerge(p,pv);PX.providers=pv.providers||{};if(c){MODE=String(c.mode||'');renderModeSw()}pxView(PXS.view||'groups');renderProviders()}
 catch(e){$('#px').innerHTML=`<div class="empty bad">无法读取代理列表：${esc(e.message)}</div>`}}
function pxSaveSet(){const u=$('#px-url').value.trim(),t=parseInt($('#px-to').value,10);if(!/^https?:\/\//.test(u)){toast('测速地址需以 http(s):// 开头',1);return}
 PXS.url=u;PXS.to=t>0?t:5000;PXS.autoclose=$('#px-autoclose').checked;localStorage.setItem('px-url',PXS.url);localStorage.setItem('px-to',PXS.to);localStorage.setItem('px-autoclose',PXS.autoclose?'1':'0');toast('已保存');renderProxies()}
async function pxSelect(g,n){const grp=PX.proxies[g];if(!grp||grp.now===n)return;
 try{await mh('/proxies/'+enc(g),'PUT',{name:n});grp.now=n;renderProxies();toast(`${g} → ${n}`);
  if(PXS.autoclose){const s=await mh('/connections');const ids=(s.connections||[]).filter(c=>(c.chains||[]).includes(g)&&!(c.chains||[]).includes(n)).map(c=>c.id);await Promise.all(ids.map(id=>mh('/connections/'+enc(id),'DELETE').catch(()=>{})));if(ids.length)toast(`${g} → ${n}，已断开 ${ids.length} 个旧连接`)}
  pxRefresh()}catch(e){toast(e.message,1)}}
async function pxTestOne(n){if(!n||TESTING.has(n))return;TESTING.add(n);renderProxies();
 try{const r=await mh(`/proxies/${enc(n)}/delay?url=${enc(PXS.url)}&timeout=${PXS.to}`);LAT[n]=r.delay||0}catch(e){LAT[n]=0}finally{TESTING.delete(n);renderProxies()}}
async function pxTestGroup(name,quiet){const g=PX.proxies[name];if(!g||TESTING.has('g:'+name))return;const all=g.all||[];TESTING.add('g:'+name);all.forEach(n=>TESTING.add(n));if(!quiet)renderProxies();
 try{const r=await mh(`/group/${enc(name)}/delay?url=${enc(PXS.url)}&timeout=${PXS.to}`);all.forEach(n=>{LAT[n]=r[n]||0})}
 catch(e){all.forEach(n=>{LAT[n]=0});if(!quiet)toast(`${name} 测速失败：${e.message}`,1)}
 finally{TESTING.delete('g:'+name);all.forEach(n=>TESTING.delete(n));if(!quiet)await pxRefresh()}}
async function pxTestAll(){const b=$('#px-testall');const names=pxGroups().filter(g=>g.name!=='GLOBAL').map(g=>g.name);if(!names.length)return;
 await btnBusy(b,async()=>{names.forEach(n=>{TESTING.add('g:'+n);(PX.proxies[n].all||[]).forEach(x=>TESTING.add(x))});renderProxies();
  let i=0;const worker=async()=>{while(i<names.length){const n=names[i++];TESTING.delete('g:'+n);await pxTestGroup(n,true);renderProxies()}};
  await Promise.all([worker(),worker(),worker()]);TESTING.clear();await pxRefresh();toast(`已测速 ${names.length} 个策略组`)})}
function renderProviders(){const all=Object.values(PX.providers||{}).filter(p=>p.vehicleType&&p.vehicleType!=='Compatible');$('#px-np').textContent=all.length;const q=$('#pxq').value.trim().toLowerCase();
 const list=q?all.filter(p=>p.name.toLowerCase().includes(q)||(p.proxies||[]).some(x=>String(x.name).toLowerCase().includes(q))):all;if(PXS.view==='providers'&&!list.length){$('#pvs').innerHTML=`<div class="empty" style="grid-column:1/-1">${q?'没有匹配的代理集合':'没有代理集合（proxy-providers）'}</div>`;return}
 $('#pvs').innerHTML=list.map(p=>{const si=p.subscriptionInfo;let sub='';if(si&&(si.Total||si.Download||si.Upload)){const used=(si.Upload||0)+(si.Download||0),tot=si.Total||0,pct=tot?Math.min(100,used/tot*100):0;
   sub=`<div class="mut" style="font-size:11.5px;margin-top:6px">已用 ${fmtBytes(used)} / ${tot?fmtBytes(tot):'不限'}${tot?` (${pct.toFixed(1)}%)`:''}${si.Expire?` · 到期 ${new Date(si.Expire*1000).toLocaleDateString('zh-CN')}`:''}</div>${tot?`<div class="bar"><i class="${pct>90?'bad':pct>70?'warn':''}" style="width:${pct.toFixed(1)}%"></i></div>`:''}`}
  const ps=p.proxies||[];const ok=ps.filter(x=>{const h=x.history||[];return h.length&&h[h.length-1].delay>0}).length;
  return `<div class="prv" data-p="${attr(p.name)}"><div class="prv-h"><b title="${attr(p.name)}">${esc(p.name)}</b><span class="tag">${esc(p.vehicleType)}</span><span class="sp"></span><span class="mut" style="font-size:12px">${ps.length} 个节点${ok?` · <span class="ok">${ok} 可用</span>`:''}</span></div>
   <div class="mut" style="font-size:11.5px">更新于 ${fmtUpd(p.updatedAt)}</div>${sub}<div class="act"><button class="btn sm" data-act="up">↻ 更新</button><button class="btn sm" data-act="hc">⚡ 健康检查</button></div></div>`}).join('')}
$('#pvs').addEventListener('click',async e=>{const b=e.target.closest('[data-act]');if(!b)return;const n=b.closest('.prv').dataset.p;
 await btnBusy(b,async()=>{try{if(b.dataset.act==='up'){await mh('/providers/proxies/'+enc(n),'PUT');toast(`${n} 已更新`)}else{await mh(`/providers/proxies/${enc(n)}/healthcheck`);toast(`${n} 健康检查完成`)}}catch(ex){toast(`${n}：${ex.message}`,1)}});loadProxies()});
async function pvUpdateAll(b){const names=Object.values(PX.providers||{}).filter(p=>p.vehicleType&&p.vehicleType!=='Compatible').map(p=>p.name);
 await btnBusy(b,async()=>{const r=await Promise.allSettled(names.map(n=>mh('/providers/proxies/'+enc(n),'PUT')));const bad=r.filter(x=>x.status==='rejected').length;toast(bad?`${bad} 个代理集合更新失败`:`已更新 ${names.length} 个代理集合`,!!bad)});loadProxies()}

/* ---------- 连接 ---------- */
const CSORT=[['start:-1','最新优先'],['start:1','最早优先'],['dls:-1','下载速度'],['uls:-1','上传速度'],['dl:-1','下载总量'],['ul:-1','上传总量'],['host:1','主机'],['src:1','来源'],['proc:1','进程'],['rule:1','规则'],['chain:1','链路']];
const CNUM=new Set(['dls','uls','dl','ul','start']);
const CN={rows:[],prev:new Map(),closed:[],view:'active',sk:'start',sd:-1,timer:null,busy:false,last:0,err:'',el:new Map(),devKey:''};
$('#cn-sort').innerHTML=CSORT.map(([v,l])=>`<option value="${v}">${l}</option>`).join('');$('#cn-sort').value='start:-1';
function cnRow(c,now){const m=c.metadata||{};const h=m.host||m.sniffHost||m.destinationIP||'';const host=h.includes(':')&&!m.host?`[${h}]`:h;const ch=(c.chains||[]).slice().reverse();
 return {id:c.id,host:host+(m.destinationPort?':'+m.destinationPort:''),net:(m.network||'').toUpperCase(),type:m.type||'',rule:(c.rule||'')+(c.rulePayload?` (${c.rulePayload})`:''),chain:ch.join(' → '),chains:ch,
  dl:c.download||0,ul:c.upload||0,dls:0,uls:0,ip:m.sourceIP||'',src:(m.sourceIP||'')+(m.sourcePort?':'+m.sourcePort:''),proc:m.process||(m.processPath||'').split('/').pop()||'',start:Date.parse(c.start)||now,raw:c}}
async function cnPoll(){if(CN.busy||(CUR==='conns'&&$('#cn-pause').checked))return;CN.busy=true;
 try{const s=await mh('/connections');const now=Date.now(),dt=CN.last?(now-CN.last)/1000:0;CN.last=now;const next=new Map();
  CN.rows=(s.connections||[]).map(c=>{const r=cnRow(c,now),p=CN.prev.get(r.id);if(p&&dt>0){r.dls=Math.max(0,(r.dl-p.dl)/dt);r.uls=Math.max(0,(r.ul-p.ul)/dt)}next.set(r.id,r);return r});
  for(const [id,p] of CN.prev)if(!next.has(id)){p.end=now;p.dls=p.uls=0;CN.closed.unshift(p)}if(CN.closed.length>500)CN.closed.length=500;CN.prev=next;CN.err=''}
 catch(e){CN.err=e.message}finally{CN.busy=false}cnRender();pxSpeeds()}
function cnTimer(){if(CN.timer)return;CN.last=0;cnPoll();CN.timer=setInterval(()=>{if(!document.hidden)cnPoll()},1000)}
function cnStart(){stickTop();cnRender();cnTimer()}
function cnStop(){if(CN.timer){clearInterval(CN.timer);CN.timer=null}}
function cnList(){const src=CN.view==='active'?CN.rows:CN.closed;const q=$('#cnq').value.trim().toLowerCase(),dev=$('#cn-dev').value;
 const l=src.filter(r=>(!dev||r.ip===dev)&&(!q||[r.host,r.rule,r.chain,r.proc,r.src,r.net,r.type,(r.raw.metadata||{}).destinationIP||''].some(x=>String(x).toLowerCase().includes(q))));
 const k=CN.sk,d=CN.sd;l.sort((a,b)=>(CNUM.has(k)?a[k]-b[k]:String(a[k]).localeCompare(String(b[k])))*d);return l}
function cnDevs(){const sel=$('#cn-dev');if(document.activeElement===sel)return;const cnt=new Map();(CN.view==='active'?CN.rows:CN.closed).forEach(r=>{if(r.ip)cnt.set(r.ip,(cnt.get(r.ip)||0)+1)});
 const cur=sel.value;if(cur&&!cnt.has(cur))cnt.set(cur,0);const list=[...cnt].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0]));const key=list.map(x=>x.join('=')).join(',');if(key===CN.devKey)return;CN.devKey=key;
 sel.innerHTML=`<option value="">全部来源</option>`+list.map(([ip,n])=>`<option value="${attr(ip)}">${esc(ip)} (${n})</option>`).join('');sel.value=cur}
function cnChain(r){const c=r.chains;if(!c.length)return'';const last=c[c.length-1],cls=last==='DIRECT'?'ok':/^REJECT/.test(last)?'bad':'';return c.slice(0,-1).map(x=>esc(x)+' → ').join('')+`<b class="${cls}">${esc(last)}</b>`}
function cnRowHtml(r,now,act){const dur=fmtDurS((r.end||now)-r.start),sp=(v,c,a)=>v?`<span class="${c}">${a} ${fmtSpd(v)}</span>`:`<span class="z">${a} -</span>`;
 return `<div class="c-main"><div class="c-l1"><span class="c-host" title="${attr(r.host)}">${esc(r.host)}</span><span class="tag">${esc(r.net)}</span><span class="c-in">${esc(r.type)}</span></div>
 <div class="c-l2" title="${attr(r.rule+' → '+r.chain)}"><span class="c-rule">${esc(r.rule)}</span><i>→</i><span class="c-chain">${cnChain(r)}</span></div>
 <div class="c-l3"><span>${esc(r.src)}${r.proc?' · '+esc(r.proc):''}</span><b>· ${dur} · ↓${fmtShort(r.dl)} ↑${fmtShort(r.ul)}</b></div></div>
 <div class="c-src"><span>${esc(r.src)}</span><small>${esc(r.proc||'-')}</small></div><div class="c-time">${dur}</div>
 <div class="c-tot"><span>↓ ${fmtShort(r.dl)}</span><span>↑ ${fmtShort(r.ul)}</span></div><div class="c-spd">${sp(r.dls,'d','↓')}${sp(r.uls,'u','↑')}</div>
 ${act?'<button class="c-x" data-close title="断开此连接">✕</button>':'<span></span>'}`}
function cnRender(){if(CUR!=='conns')return;const now=Date.now(),act=CN.view==='active';cnDevs();const l=cnList(),LIM=300,filtered=!!($('#cnq').value.trim()||$('#cn-dev').value);
 $('#cn-na').textContent=CN.rows.length;$('#cn-nc').textContent=CN.closed.length;$('#cn-closeall').textContent=act?(filtered?`断开筛选的 ${l.length} 个`:'断开全部'):'清空记录';$('#cn-closeall').disabled=!l.length;
 const ud=CN.rows.reduce((a,r)=>[a[0]+r.dls,a[1]+r.uls],[0,0]);
 $('#cn-info').innerHTML=CN.err?`<span class="bad">${esc(CN.err)}</span>`:act?`↓ ${fmtSpd(ud[0])} ↑ ${fmtSpd(ud[1])}`:'';
 $('#cn-h').querySelectorAll('span[data-k]').forEach(s=>{const on=s.dataset.k===CN.sk||(s.dataset.k==='dl'&&CN.sk==='ul')||(s.dataset.k==='dls'&&CN.sk==='uls');s.className=(s.classList.contains('r')?'r ':'')+(on?'s'+(CN.sd>0?' asc':''):'')});
 const box=$('#cn-rows'),seen=new Set();let prev=null;
 for(const r of l.slice(0,LIM)){const h=cnRowHtml(r,now,act),cls='cr'+(act&&(r.dls>1024||r.uls>1024)?' hot':'')+(act?'':' closed');let e=CN.el.get(r.id);
  if(!e){const el=document.createElement('div');el.dataset.id=r.id;e={el,h:'',cls:''};CN.el.set(r.id,e)}
  if(e.h!==h){e.el.innerHTML=h;e.h=h}if(e.cls!==cls){e.el.className=cls;e.cls=cls}
  const ref=prev?prev.nextSibling:box.firstChild;if(e.el!==ref)box.insertBefore(e.el,ref);prev=e.el;seen.add(r.id)}
 for(const [id,e] of CN.el)if(!seen.has(id)){e.el.remove();CN.el.delete(id)}
 const em=$('#cn-empty');em.style.display=l.length?'none':'';em.textContent=CN.err&&!CN.rows.length?'无法读取连接：'+CN.err:filtered?'没有匹配的连接':act?'没有活动连接':'本页打开后还没有连接关闭';
 $('#cn-more').textContent=l.length>LIM?`共 ${l.length} 条，只显示前 ${LIM} 条，可用过滤或来源缩小范围`:(act?'':'记录本页打开期间关闭的连接，最多 500 条')}
$('#cn-h').addEventListener('click',e=>{const s=e.target.closest('span[data-k]');if(!s)return;const k=s.dataset.k;if(CN.sk===k)CN.sd=-CN.sd;else{CN.sk=k;CN.sd=CNUM.has(k)?-1:1}
 const v=`${CN.sk}:${CN.sd}`;$('#cn-sort').value=CSORT.some(x=>x[0]===v)?v:'';cnRender()});
$('#cn-sort').addEventListener('change',e=>{const [k,d]=e.target.value.split(':');CN.sk=k;CN.sd=+d;cnRender()});
$('#cn-rows').addEventListener('click',e=>{const row=e.target.closest('.cr');if(!row)return;if(e.target.closest('[data-close]')){cnClose(row.dataset.id);return}if(getSelection().toString())return;cnDetail(row.dataset.id)});
$('#cn-view').addEventListener('click',e=>{const b=e.target.closest('button[data-v]');if(!b)return;CN.view=b.dataset.v;CN.devKey='';$('#cn-view').querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));cnRender()});
$('#cnq').addEventListener('input',cnRender);$('#cn-dev').addEventListener('change',cnRender);$('#cn-pause').addEventListener('change',()=>{CN.last=0;cnRender()});
async function cnClose(id){try{await mh('/connections/'+enc(id),'DELETE');toast('已断开连接');closeModal();cnPoll()}catch(e){toast(e.message,1)}}
async function cnCloseAll(all){if(!all&&CN.view==='closed'){CN.closed=[];cnRender();return}
 const q=all?'':($('#cnq').value.trim()||$('#cn-dev').value);if(q){const ids=cnList().map(r=>r.id);if(!ids.length)return;if(!confirm(`断开筛选出的 ${ids.length} 个连接？`))return;
  const r=await Promise.allSettled(ids.map(id=>mh('/connections/'+enc(id),'DELETE')));toast(`已断开 ${r.filter(x=>x.status==='fulfilled').length} 个连接`)}
 else{if(!confirm('断开所有活动连接？'))return;try{await mh('/connections','DELETE');toast('已断开所有连接')}catch(e){toast(e.message,1)}}
 if(CUR==='conns')cnPoll()}
function cnDetail(id){const r=CN.rows.find(x=>x.id===id)||CN.closed.find(x=>x.id===id);if(!r)return;const m=r.raw.metadata||{};
 const kv=[['主机',m.host],['嗅探主机',m.sniffHost],['目标 IP',m.destinationIP],['目标端口',m.destinationPort],['远端目标',m.remoteDestination],['来源',r.src],['入站',[m.type,m.inboundName,m.inboundUser].filter(Boolean).join(' · ')],['网络',m.network],['DNS 模式',m.dnsMode],
  ['进程',m.process],['进程路径',m.processPath],['UID',m.uid],['规则',r.rule],['链路',r.chain],['下载',fmtBytes(r.dl)],['上传',fmtBytes(r.ul)],['开始',new Date(r.start).toLocaleString('zh-CN',{hour12:false})],['结束',r.end?new Date(r.end).toLocaleString('zh-CN',{hour12:false}):''],['ID',r.id]]
  .filter(x=>x[1]!==undefined&&x[1]!==null&&x[1]!=='');
 modal('连接详情',`<div class="kvs">${kv.map(([k,v])=>`<b>${k}</b><span>${esc(v)}</span>`).join('')}</div><details><summary>原始 JSON</summary><pre>${esc(JSON.stringify(r.raw,null,2))}</pre></details>`,
  r.end?'':`<button class="btn dan" onclick="cnClose('${attr(r.id)}')">断开此连接</button>`)}

/* ---------- 生效规则 / 规则集 ---------- */
const RR={rules:[],prov:{},limit:500};
const RRC=t=>/^match$/i.test(t)?'c-match':/ruleset/i.test(t)?'c-rs':/^(domain|geosite)/i.test(t)?'c-dom':/(ip|asn)/i.test(t)?'c-ip':/^(and|or|not|subrules)$/i.test(t)?'':'c-proc';
async function loadRtRules(){try{const [r,p]=await Promise.all([mh('/rules'),mh('/providers/rules').catch(()=>({providers:{}}))]);RR.rules=r.rules||[];RR.prov=p.providers||{};RR.limit=500;renderRtRules();renderRuleProviders()}
 catch(e){$('#rrl').innerHTML=`<div class="empty bad">无法读取规则：${esc(e.message)}</div>`}}
function renderRtRules(){const q=$('#rrq').value.trim().toLowerCase();const all=RR.rules.map((r,i)=>Object.assign({i},r));const l=q?all.filter(r=>`${r.type} ${r.payload} ${r.proxy}`.toLowerCase().includes(q)):all;
 $('#rr-cnt').textContent=q?`${l.length} / ${all.length}`:all.length;
 const size=r=>r.size>0?r.size:(/ruleset/i.test(r.type)&&RR.prov[r.payload]?RR.prov[r.payload].ruleCount:0);
 $('#rrl').innerHTML=l.slice(0,RR.limit).map(r=>`<div class="rr ${RRC(r.type)}"><span class="i">${r.i+1}</span><span class="ty">${esc(r.type)}</span><span class="pl" title="${attr(r.payload)}">${hl(r.payload||'-',q)}${size(r)>0?` <span class="tag">${size(r).toLocaleString()} 条</span>`:''}</span><span class="px"><span class="dot ${polDot(r.proxy)}"></span>${hl(r.proxy,q)}</span></div>`).join('')||'<div class="empty">没有匹配的规则</div>';
 const more=$('#rr-more');more.style.display=l.length>RR.limit?'':'none';more.textContent=`显示更多（还有 ${l.length-RR.limit} 条）`}
$('#rrq').addEventListener('input',()=>{RR.limit=500;renderRtRules()});
function renderRuleProviders(){const l=Object.values(RR.prov);$('#rp-cnt').textContent=l.length;$('#rp-card').style.display=l.length?'':'none';
 $('#rps').innerHTML='<div class="lst">'+l.map(p=>`<div class="li" data-p="${attr(p.name)}"><span class="nm"><b>${esc(p.name)}</b> <span class="tag cyan">${esc(p.behavior||'')}</span> <span class="tag">${esc(p.vehicleType||'')}</span>${p.format?` <span class="tag">${esc(p.format)}</span>`:''}<small>${(p.ruleCount||0).toLocaleString()} 条规则 · 更新于 ${fmtUpd(p.updatedAt)}</small></span><span class="act"><button class="btn sm" data-act="up">↻ 更新</button></span></div>`).join('')+'</div>'}
$('#rps').addEventListener('click',async e=>{const b=e.target.closest('[data-act]');if(!b)return;const n=b.closest('.li').dataset.p;
 await btnBusy(b,async()=>{try{await mh('/providers/rules/'+enc(n),'PUT');toast(`${n} 已更新`)}catch(ex){toast(`${n}：${ex.message}`,1)}});loadRtRules()});
async function rpUpdateAll(b){const names=Object.keys(RR.prov);await btnBusy(b,async()=>{const r=await Promise.allSettled(names.map(n=>mh('/providers/rules/'+enc(n),'PUT')));const bad=r.filter(x=>x.status==='rejected').length;toast(bad?`${bad} 个规则集更新失败`:`已更新 ${names.length} 个规则集`,!!bad)});loadRtRules()}

/* ---------- 内核设置 ---------- */
let ST={};
async function loadSettings(){try{const [c,v]=await Promise.all([mh('/configs'),mh('/version').catch(()=>({}))]);ST=c;MODE=String(c.mode||'');$('#st-ver').textContent=v.version?`内核 ${v.version}${v.meta?' · Meta':''}`:'';renderSettings();renderModeSw()}
 catch(e){$('#st-grid').innerHTML=`<div class="empty bad">无法读取内核配置：${esc(e.message)}</div>`}}
function renderSettings(){const c=ST,t=c.tun||{};
 const num=(k,l,d)=>`<div class="sf"><b>${l}</b><input type="number" min="0" max="65535" data-k="${k}" value="${+c[k]||0}"><small>${d}</small></div>`;
 const sw=(k,l,v,d)=>`<div class="sf"><b>${l}</b><label class="switch"><input type="checkbox" data-k="${k}" ${v?'checked':''}><i></i>${v?'已开启':'已关闭'}</label><small>${d}</small></div>`;
 const sel=(k,l,opts,cur,d)=>`<div class="sf"><b>${l}</b><select data-k="${k}">${opts.map(o=>`<option value="${o.toLowerCase()}" ${String(cur||'').toLowerCase()===o.toLowerCase()?'selected':''}>${o}</option>`).join('')}</select><small>${d}</small></div>`;
 $('#st-grid').innerHTML=`<div class="sf"><b>代理模式</b><span class="seg modesw" style="align-self:flex-start">${modeBtns()}</span><small>规则 / 全局 / 直连</small></div>`
  +sel('log-level','日志级别',['silent','error','warning','info','debug'],c['log-level'],'决定实时日志能看到的内容')
  +num('mixed-port','混合端口','HTTP + SOCKS5，0 表示关闭')+num('port','HTTP 端口','')+num('socks-port','SOCKS5 端口','')+num('redir-port','Redir 端口','Linux 透明代理')+num('tproxy-port','TProxy 端口','Linux 透明代理')
  +sw('allow-lan','允许局域网连接',c['allow-lan'],c['bind-address']?`绑定地址 ${esc(c['bind-address'])}`:'')+sw('ipv6','IPv6',c.ipv6,'')+sw('sniffing','域名嗅探',c.sniffing,'')+sw('tcp-concurrent','TCP 并发',c['tcp-concurrent'],'同时连接解析出的所有 IP')
  +sw('tun.enable','TUN 模式',t.enable,t.device?`设备 ${esc(t.device)}`:'')+sel('tun.stack','TUN 协议栈',['gVisor','System','Mixed'],t.stack,'')
  +`<div class="sf"><b>出口网卡</b><input type="text" data-k="interface-name" value="${attr(c['interface-name']||'')}" placeholder="自动" spellcheck="false"><small>回车或移出输入框后应用</small></div>`}
$('#st-grid').addEventListener('change',async e=>{const el=e.target,k=el.dataset.k;if(!k)return;let v=el.type==='checkbox'?el.checked:el.type==='number'?parseInt(el.value||'0',10):el.value.trim();
 if(el.type==='number'&&(isNaN(v)||v<0||v>65535)){toast('端口范围 0–65535',1);renderSettings();return}
 if(k==='tun.enable'&&!confirm(v?'开启 TUN 模式？':'关闭 TUN 模式？经 TUN 转发的流量会失去代理。')){renderSettings();return}
 const body=k.startsWith('tun.')?{tun:{[k.slice(4)]:v}}:{[k]:v};
 try{await mh('/configs','PATCH',body);toast(`已应用 ${k} = ${v}`)}catch(ex){toast(ex.message,1)}loadSettings()});
async function stAct(a){const m={fakeip:['/cache/fakeip/flush','FakeIP 缓存已清空'],dns:['/cache/dns/flush','DNS 缓存已清空']}[a];try{await mh(m[0],'POST');toast(m[1])}catch(e){toast(e.message,1)}}
async function stReload(){try{const r=await api('/api/service/reload',{});toast(r.ok?'已重载 config.yaml':r.output,!r.ok)}catch(e){toast(e.message,1)}}
async function dnsQuery(){const n=$('#dq-name').value.trim();if(!n)return;const o=$('#dq-out');o.className='mut';o.textContent='查询中…';
 try{const r=await mh(`/dns/query?name=${enc(n)}&type=${$('#dq-type').value}`);const ans=(r.Answer||[]).map(a=>`${a.name}  ${a.TTL}s  ${a.data}`).join('\n');o.className='';o.textContent=ans||`没有结果（Status ${r.Status??'-'}）`}
 catch(e){o.className='bad';o.textContent=e.message}}

/* ---------- kernel ---------- */
let K=null;
const VLABEL={'':'默认','compatible':'compatible (v1，老 CPU)','v1':'v1','v2':'v2 (SSE4.2)','v3':'v3 (AVX2)'};
const VLEVEL={'':3,'v3':3,'v2':2,'compatible':1,'v1':1};
function semver(v){const m=/(\d+)\.(\d+)\.(\d+)/.exec(v||'');return m?[+m[1],+m[2],+m[3]]:null}
function cmpVer(a,b){const x=semver(a),y=semver(b);if(!x||!y)return 0;for(let i=0;i<3;i++)if(x[i]!==y[i])return x[i]-y[i];return 0}
function fmtDate(iso){const d=parseTs((iso||'').replace('T',' '));if(!d)return'-';const u=new Date(iso);return isNaN(u)?iso:u.toLocaleString('zh-CN',{hour12:false})}
async function loadKernel(refresh){if(refresh){$('#k-fetched').innerHTML='<span class="spin"></span>查询 GitHub…'}
 try{K=await api('/api/kernel'+(refresh?'?refresh=1':''));const c=K.current,cur=c.version||'?';
  $('#k-cur').textContent=c.ok?('v'+cur):'无法运行';$('#k-cur').className='v'+(c.ok?'':' bad');$('#k-cur-s').textContent=(c.raw||'').split('\n')[0].replace(/^Mihomo Meta v[\w.\-]+ /,'')||c.binary;
  const latest=K.latest_stable,isAlpha=/^alpha/.test(cur),upd=latest&&!isAlpha&&cmpVer(latest,cur)>0;
  $('#k-arrow').style.display=upd?'':'none';$('#k-latest-w').style.display=upd?'':'none';if(upd){const r=K.releases.find(x=>x.tag===latest);$('#k-latest').textContent=latest;$('#k-latest-s').textContent='最新稳定版 · '+fmtDate(r?.published)}
  const b=$('#k-badge');b.className='badge '+(K.error?'bad':upd?'dirty':latest?'ok':'');b.textContent=K.error?'检查失败':upd?`可更新到 ${latest}`:latest?'已是最新稳定版':'未获取';
  $('#k-fetched').textContent=K.fetched?`列表更新于 ${new Date(K.fetched*1000).toLocaleTimeString('zh-CN',{hour12:false})}`:'';
  const tile=(k,v,c='',sub='')=>`<div class="tile ${c}"><b>${k}</b><span>${v}</span>${sub?`<small>${sub}</small>`:''}</div>`;
  $('#kstat').innerHTML=tile('二进制',`<span class="mono" style="font-size:12px">${esc(c.binary)}</span>`,'acc',c.info?`${fmtBytes(c.info.size)} · ${c.info.mtime_str}`:'不存在')
   +tile('架构',`linux-${esc(c.arch)}`,'cyan',esc(c.machine))
   +tile('CPU 等级',`amd64 ${esc(c.cpu_level)}`,c.cpu_level==='v1'?'warn':'ok',c.arch==='amd64'?`推荐变体: ${VLABEL[K.recommended_variant]??K.recommended_variant}`:'')
   +tile('内核备份',`${K.backups.length} 份`,'pur','保留最近 3 份');
  if(isAlpha)$('#k-pre').checked=true;kFillRel();
  $('#k-bkcnt').textContent=K.backups.length;$('#k-bk').innerHTML=K.backups.map((b,i)=>`<div class="bki"><span class="ic">▣</span><span class="nm">${esc(b.name)}<small>v${esc(b.version)} · ${b.mtime_str} · ${rel(b.mtime)}</small></span><span class="sz">${fmtBytes(b.size)}</span>
   <span class="act"><button class="btn sm" onclick="kernelRollback('${b.name}')">回滚到此版本</button><button class="btn sm ghost" title="删除" onclick="kernelBkDel('${b.name}')">✕</button></span></div>`).join('')||'<div class="empty">暂无内核备份，首次更新后自动生成。</div>';
  if(K.error){panel('k','bad','');$('#out-k').className='bad';$('#out-k').textContent='获取版本列表失败：'+K.error+'\n可稍后点「检查更新」重试。'}
  stickTop()}catch(e){$('#k-cur').textContent='加载失败';$('#k-cur-s').textContent=e.message;$('#k-fetched').textContent=''}}
function kFillRel(){const cur=K.current.version||'',showPre=$('#k-pre').checked,latest=K.latest_stable;const list=K.releases.filter(r=>showPre||!r.prerelease);
 const rs=$('#k-rel');rs.innerHTML=list.map(r=>{const alpha=r.tag==='Prerelease-Alpha';const h=alpha?(/alpha-([0-9a-f]+)/.exec(r.assets[0]?.name||'')||[])[1]:'';const same=alpha?cur==='alpha-'+h:(!!semver(cur)&&cmpVer(r.tag,cur)===0);
   return `<option value="${attr(r.tag)}">${esc(alpha?`Prerelease-Alpha (${h})`:r.tag)}${r.prerelease?' · 预发布':''}${same?' · 当前':''} — ${fmtDate(r.published).slice(0,10)}</option>`}).join('')||'<option value="">（无数据）</option>';
 rs.value=latest&&list.some(r=>r.tag===latest)?latest:(list[0]?.tag||'');kRelChange()}
function kRelChange(){const r=K?.releases.find(x=>x.tag===$('#k-rel').value);const as=$('#k-asset');if(!r){as.innerHTML='';$('#k-go').disabled=true;return}
 as.innerHTML=r.assets.map(a=>`<option value="${attr(a.url)}" data-name="${attr(a.name)}" data-v="${attr(a.variant)}">${esc(VLABEL[a.variant]??a.variant)}${a.variant===K.recommended_variant?' ★ 推荐':''} · ${fmtBytes(a.size)}</option>`).join('');
 const rec=[...as.options].find(o=>o.dataset.v===K.recommended_variant);if(rec)as.value=rec.value;$('#k-go').disabled=!as.options.length;
 $('#k-notes-tag').textContent=r.tag;$('#k-body').textContent=r.body||'（无说明）';kAssetChange()}
function kAssetChange(){const o=$('#k-asset').selectedOptions[0];const w=$('#k-warn');if(!o||!K){w.style.display='none';return}const lv={v1:1,v2:2,v3:3}[K.current.cpu_level]||1,need=VLEVEL[o.dataset.v];
 const r=K.releases.find(x=>x.tag===$('#k-rel').value);let msg='';if(K.current.arch==='amd64'&&need!==undefined&&need>lv)msg=`⚠ 当前 CPU 仅支持 amd64 ${K.current.cpu_level}，「${VLABEL[o.dataset.v]}」构建需要 ${o.dataset.v||'v3'} 指令集，启动会直接失败（会自动回滚，但服务会短暂中断）。建议选「${VLABEL[K.recommended_variant]}」。`;
 else if(r?.prerelease)msg='⚠ 预发布版本（alpha）可能不稳定；若出问题可在下方「内核备份」一键回滚。';
 else if(cmpVer(r?.tag,K.current.version)<0)msg='ℹ 这是比当前更旧的版本（降级）。';
 w.textContent=msg;w.style.display=msg?'':'none'}
async function kernelUpdate(){const o=$('#k-asset').selectedOptions[0];if(!o)return;const name=o.dataset.name;if(!confirm(`下载并安装 ${name}？\n\n校验通过后会替换 ${K.current.binary} 并重启 mihomo（连接会短暂中断）。`))return;
 busy(1);panel('k','busy','<span class="spin"></span>下载并安装中，约 20MB，请勿关闭页面…');$('#out-k').className='mut';$('#out-k').textContent='';
 try{const r=await api('/api/kernel/update',{url:o.value,name});panel('k',r.ok?'ok':'bad',r.ok?`完成 · ${r.seconds}s · 现在 v${r.version?.version}`:(r.rolled_back?'失败，已自动回滚':'失败'));$('#out-k').className=r.ok?'ok':'bad';$('#out-k').textContent=r.output;toast(r.ok?`内核已更新到 v${r.version?.version}`:'内核更新失败',!r.ok)}
 catch(e){panel('k','bad','');$('#out-k').className='bad';$('#out-k').textContent=e.message}finally{busy(0);loadKernel();loadStatus()}}
async function kernelRollback(n){if(!confirm(`回滚到 ${n} 并重启 mihomo？`))return;busy(1);panel('k','busy','<span class="spin"></span>回滚中…');$('#out-k').className='mut';$('#out-k').textContent='';
 try{const r=await api('/api/kernel/rollback',{name:n});panel('k',r.ok?'ok':'bad',r.ok?`已回滚 · 现在 v${r.version?.version}`:'回滚失败');$('#out-k').className=r.ok?'ok':'bad';$('#out-k').textContent=r.output;toast(r.ok?'已回滚':'回滚失败',!r.ok)}
 catch(e){panel('k','bad','');$('#out-k').className='bad';$('#out-k').textContent=e.message}finally{busy(0);loadKernel();loadStatus()}}
async function kernelBkDel(n){if(!confirm(`删除内核备份 ${n}？`))return;try{await api('/api/kernel/backup/delete',{name:n});toast('已删除');loadKernel()}catch(e){toast(e.message,1)}}

function loadUpdate(){loadKernel();loadGeo()}
let GEO={files:[]};
function geoChecked(){return [...document.querySelectorAll('.geo .ck:checked')].map(c=>c.dataset.f)}
async function loadGeo(refresh){if(refresh)$('#g-fetched').innerHTML='<span class="spin"></span>查询 GitHub…';
 try{GEO=await api('/api/geo'+(refresh?'?refresh=1':''));$('#g-repo').textContent=GEO.repo;$('#g-fetched').textContent=GEO.fetched?`远端信息更新于 ${new Date(GEO.fetched*1000).toLocaleTimeString('zh-CN',{hour12:false})}`:'';
  $('#geos').innerHTML=GEO.files.map(f=>{const m=f.meta||{};const rem=f.remote;const remT=rem?.updated?new Date(rem.updated):null;const loc=f.info?new Date(f.info.mtime*1000):null;const same=!!(rem&&f.info&&rem.size===f.info.size);
   const newer=!!(rem&&f.exists&&!same&&remT&&loc&&remT>loc);const st=!f.exists?'miss':newer?'new':rem?'ok':'';const badge=!f.exists?['bad','缺失']:newer?['dirty','有更新']:rem?['ok','已是最新']:f.custom_url?['','自定义源']:['','未知'];
   return `<div class="geo ${st}"><input type="checkbox" class="ck" data-f="${attr(f.file)}" ${newer||!f.exists?'checked':''} onchange="$('#g-go-sel').disabled=!geoChecked().length"><span class="nm">${esc(f.file)}</span><span class="badge ${badge[0]}">${badge[1]}</span>
   <span class="d">${esc(f.desc)}</span>
   <div class="kv"><b>本地</b><span>${f.exists?`${fmtBytes(f.info.size)} · 下载 ${f.info.mtime_str}${m.build_str?' · 数据构建 '+m.build_str:''}${m.type?' · '+esc(m.type):''}${m.nodes?' · '+m.nodes.toLocaleString()+' 节点':''}${m.error?' · <span class="bad">'+esc(m.error)+'</span>':''}`:'<span class="bad">文件不存在</span>'}</span>
   <b>远端</b><span>${rem?`${fmtBytes(rem.size)} · 发布 ${fmtDate(rem.updated)}`:f.custom_url?'自定义 URL，无法比对':'<span class="mut">未获取</span>'}</span>
   <b>来源</b><span title="${attr(f.url)}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.url.replace(/^https?:\/\//,''))}</span></div>
   <div class="act"><button class="btn sm" onclick="geoUpdate(['${attr(f.file)}'])">⤓ 更新此文件</button>${f.bak?`<button class="btn sm ghost" title="恢复 ${attr(f.file)}.bak（${f.bak.mtime_str}）" onclick="geoRollback('${attr(f.file)}')">↶ 恢复 .bak</button>`:''}</div></div>`}).join('')||'<div class="empty">未找到地理数据库文件</div>';
  $('#g-go-sel').disabled=!geoChecked().length;
  if(GEO.error){$('#g-fetched').innerHTML=`<span class="bad">远端查询失败</span>`;$('#g-fetched').title=GEO.error}
  stickTop()}catch(e){$('#geos').innerHTML=`<div class="empty bad">${esc(e.message)}</div>`}}
async function geoUpdate(names){if(!names.length){toast('请先勾选要更新的数据库',1);return}const rs=$('#g-restart').checked;if(!confirm(`下载并更新 ${names.join(', ')}？`+(rs?'\n完成后将重启 mihomo（连接短暂中断）。':'\n不重启，下次重启后生效。')))return;
 busy(1);panel('g','busy','<span class="spin"></span>下载中…');$('#out-g').className='mut';$('#out-g').textContent='';
 try{const r=await api('/api/geo/update',{names,restart:rs});panel('g',r.ok?'ok':'bad',`${r.updated.length}/${names.length} · ${r.seconds}s`);$('#out-g').className=r.ok?'ok':'bad';$('#out-g').textContent=r.output;toast(r.ok?'地理数据库已更新':'部分更新失败',!r.ok)}
 catch(e){panel('g','bad','');$('#out-g').className='bad';$('#out-g').textContent=e.message}finally{busy(0);loadGeo();loadStatus()}}
async function geoRollback(n){if(!confirm(`用 ${n}.bak 覆盖 ${n} 并重启 mihomo？`))return;busy(1);panel('g','busy','<span class="spin"></span>恢复中…');
 try{const r=await api('/api/geo/rollback',{name:n});panel('g',r.ok?'ok':'bad','恢复');$('#out-g').className=r.ok?'ok':'bad';$('#out-g').textContent=r.output;toast(r.ok?'已恢复':'恢复失败',!r.ok)}catch(e){panel('g','bad','');$('#out-g').className='bad';$('#out-g').textContent=e.message}finally{busy(0);loadGeo();loadStatus()}}

/* ---------- logs ---------- */
let LOG_RAW='';
function renderLogs(){const q=$('#logq').value.trim().toLowerCase();const lines=LOG_RAW.split('\n');let n=0;const re=q?new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'ig'):null;
 const html=lines.filter(l=>!q||l.toLowerCase().includes(q)).map(l=>{n++;const c=/level=error|\berror\b|fatal|panic/i.test(l)?'e':/level=warn|\bwarn/i.test(l)?'w':/level=debug/i.test(l)?'d':/systemd\[|Started|Stopped|Stopping/.test(l)?'s':'';let t=esc(l);if(re)t=t.replace(re,m=>`<mark>${m}</mark>`);return `<span class="l ${c}">${t}</span>`}).join('\n');
 const p=$('#logs');p.innerHTML=html||'<span class="mut">（无匹配）</span>';$('#log-info').textContent=q?`${n} / ${lines.length} 行`:`${lines.length} 行`;p.scrollTop=p.scrollHeight}
async function loadLogs(){try{const r=await api('/api/logs?n='+$('#logn').value);LOG_RAW=r.output;renderLogs()}catch(e){$('#logs').innerHTML=`<span class="l e">${esc(e.message)}</span>`}}
$('#logq').addEventListener('input',renderLogs);$('#logn').addEventListener('change',loadLogs);
setInterval(()=>{if($('#logauto').checked&&CUR==='logs'&&LG.src==='journal'&&!document.hidden)loadLogs()},5000);
/* 实时内核日志（controller /logs 流） */
const LG={items:[],max:1000,level:localStorage.getItem('lg-level')||'info',src:localStorage.getItem('lg-src')||'live',s:null,started:false,t:0};
$('#lg-level').value=LG.level;
function lgStart(){if(LG.s)return;LG.started=true;LG.s=openStream(`${MH}/logs?level=${enc(LG.level)}`,m=>{if($('#lg-pause').checked||!m.type)return;
  LG.items.push({t:new Date(),type:String(m.type),payload:String(m.payload??'')});if(LG.items.length>LG.max)LG.items.splice(0,LG.items.length-LG.max);if(!LG.t)LG.t=setTimeout(renderLiveLogs,250)},
  msg=>{if(CUR==='logs')$('#lg-info').innerHTML=`<span class="bad">${esc(msg)}</span>`})}
function lgStop(){if(LG.s){LG.s.stop();LG.s=null}}
function renderLiveLogs(){LG.t=0;if(CUR!=='logs'||LG.src!=='live')return;const box=$('#llog');const atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<40;const q=$('#lgq').value.trim().toLowerCase();
 const l=q?LG.items.filter(x=>x.payload.toLowerCase().includes(q)||x.type.includes(q)):LG.items;
 box.innerHTML=l.map(x=>`<div class="ll ${esc(x.type)}"><span class="tm">${x.t.toLocaleTimeString('zh-CN',{hour12:false})}</span><span class="lv">${esc(x.type)}</span><span class="msg">${hl(x.payload,q)}</span></div>`).join('')||`<div class="mut" style="padding:8px 12px">${q?'（无匹配）':`等待日志…（级别 ${esc(LG.level)}，内核自身的 log-level 更高时也看不到更低级别）`}</div>`;
 $('#lg-info').textContent=q?`${l.length} / ${LG.items.length} 条`:`${LG.items.length} 条`;if(atBottom||!LG.scrolled){box.scrollTop=box.scrollHeight;LG.scrolled=true}}
function lgSrc(s){LG.src=s;localStorage.setItem('lg-src',s);const live=s==='live';$('#lg-src').querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.s===s));
 $('#lg-live-tb').style.display=live?'':'none';$('#lg-journal-tb').style.display=live?'none':'';$('#llog').style.display=live?'':'none';$('#logs').style.display=live?'none':'';
 if(live){lgStart();renderLiveLogs()}else loadLogs()}
function loadLogsTab(){lgSrc(LG.src)}
$('#lg-src').addEventListener('click',e=>{const b=e.target.closest('button[data-s]');if(b)lgSrc(b.dataset.s)});
$('#lg-level').addEventListener('change',e=>{LG.level=e.target.value;localStorage.setItem('lg-level',LG.level);lgStop();lgStart();toast('日志级别：'+LG.level)});
$('#lgq').addEventListener('input',renderLiveLogs);$('#lg-pause').addEventListener('change',renderLiveLogs);
document.addEventListener('visibilitychange',()=>{if(document.hidden){liveStop();lgStop()}else{liveStart();if(LG.started)lgStart()}});

/* ---------- config ---------- */
let CFG_RAW='';
function renderCfg(){const q=$('#cfq').value.trim().toLowerCase();$('#cfg').textContent=q?CFG_RAW.split('\n').filter(l=>l.toLowerCase().includes(q)).join('\n')||'（无匹配）':CFG_RAW}
async function loadConfig(){const r=await api('/api/config');CFG_RAW=r.content;renderCfg();$('#cf-info').textContent=r.info?`config.yaml · ${r.info.mtime_str} · ${fmtBytes(r.info.size)} · ${CFG_RAW.split('\n').length} 行`:'';stickTop()}
$('#cfq').addEventListener('input',renderCfg);

const H0=location.hash.slice(1);renderModeSw();if(H0&&H0!=='status'&&LOADERS[H0])showTab(H0,false);loadStatus();liveStart();setInterval(loadStatus,30000);stickTop();api('/api/kernel').then(k=>{if(k.latest_stable&&cmpVer(k.latest_stable,k.current.version)>0)$('#nav-upd').className='dotn'}).catch(()=>{});
</script></body></html>
"""


def main():
    host, port = LISTEN.rsplit(":", 1)
    if SESSION_MAX_AGE <= 0:
        raise ValueError("MANAGER_SESSION_MAX_AGE must be positive")
    if not Handler.PASSWORD and not Handler.TOKEN:
        print("WARN: no manager password or token configured, running WITHOUT auth", file=sys.stderr)
    srv = ThreadingHTTPServer((host, int(port)), Handler)
    srv.daemon_threads = True
    print(f"mihomo-manager listening on {LISTEN}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
