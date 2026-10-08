#!/usr/bin/env python3
import argparse
import datetime
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
STATIC_DIR = os.path.join(ROOT, "static")
TEMPLATE_DIR = os.path.join(ROOT, "templates")
TEMPLATE_PATH = os.path.join(TEMPLATE_DIR, "index.html")
SERVICE_TAG = "ZXF-WebUI/1.0"

DEFAULTS = {
    "pace": "4:25",
    "routeConfig": "zjg.json",
    "dt": 0.01,
    "fit_curve": True,
    "fit_samples": 8,
    "wander_m": 1.0,
    "wander_step_m": 0.25,
    "wander_step_hz": 1.8,
    "lap_jitter_s": 15,
    "pace_var_s": 10,
}
KNOWN_KEYS = list(DEFAULTS.keys())
PROGRESS_RE = re.compile(r"^第\d+圈 \d+/")


def load_config():
    import yaml
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    merged = dict(DEFAULTS)
    merged.update({k: cfg.get(k, DEFAULTS[k]) for k in KNOWN_KEYS})
    return merged


def _dump_scalar(value):
    import yaml
    text = yaml.safe_dump(value, default_flow_style=True,
                          allow_unicode=True).strip().splitlines()
    return text[0] if text else "null"


def save_config_inplace(updates):
    with open(CONFIG_PATH) as f:
        lines = f.read().splitlines()
    rest = dict(updates)
    out = []
    for line in lines:
        m = re.match(r"^(\s*)([A-Za-z_]\w*)\s*:(.*)$", line)
        if m and m.group(2) in rest:
            key = m.group(2)
            tail = m.group(3)
            c = tail.find("#")
            comment = tail[c:] if c != -1 else ""
            out.append(f"{m.group(1)}{key}: {_dump_scalar(rest.pop(key))}"
                       + (f"  {comment}" if comment else ""))
        else:
            out.append(line)
    for key in KNOWN_KEYS:
        if key in rest:
            out.append(f"{key}: {_dump_scalar(rest.pop(key))}")
    with open(CONFIG_PATH, "w") as f:
        f.write("\n".join(out) + "\n")


def validate_config(cfg):
    errs = []
    pace = cfg.get("pace")
    if pace in (None, "", "null"):
        errs.append("pace 不能为空 (如 4:25)")
    else:
        try:
            from run import parse_pace
            if parse_pace(pace) is None:
                errs.append("pace 为空")
        except Exception as e:
            errs.append(f"pace 非法 ({e})")
    try:
        if float(cfg.get("dt")) <= 0:
            errs.append("dt 必须 > 0")
    except (TypeError, ValueError):
        errs.append("dt 不是数字")
    try:
        if int(cfg.get("fit_samples")) < 1:
            errs.append("fit_samples 必须 >= 1")
    except (TypeError, ValueError):
        errs.append("fit_samples 不是整数")
    for k in ("wander_m", "wander_step_m", "lap_jitter_s", "pace_var_s"):
        try:
            if float(cfg.get(k)) < 0:
                errs.append(f"{k} 不能 < 0")
        except (TypeError, ValueError):
            errs.append(f"{k} 不是数字")
    try:
        if float(cfg.get("wander_step_hz")) <= 0:
            errs.append("wander_step_hz 必须 > 0")
    except (TypeError, ValueError):
        errs.append("wander_step_hz 不是数字")
    rc = cfg.get("routeConfig")
    if not rc or not os.path.isfile(os.path.join(ROOT, str(rc))):
        errs.append(f"路线文件不存在: {rc}")
    return errs


def segment_lengths(loc):
    from run import geodistance
    n = len(loc)
    return [geodistance(loc[i], loc[(i + 1) % n]) for i in range(n)]


def spacing_stats(loc):
    segs = segment_lengths(loc)
    if not segs:
        return {"n": 0, "total": 0, "mean": 0, "std": 0, "min": 0, "max": 0}
    return {"n": len(loc), "total": round(sum(segs), 1),
            "mean": round(statistics.mean(segs), 2),
            "std": round(statistics.stdev(segs), 2) if len(segs) > 1 else 0.0,
            "min": round(min(segs), 2), "max": round(max(segs), 2)}


def even_resample(loc, spacing_m):
    n = len(loc)
    if n < 2:
        return [p.copy() for p in loc]
    segs = segment_lengths(loc)
    total = sum(segs)
    if total <= 0:
        return [loc[0].copy()]
    count = max(3, int(round(total / spacing_m)))
    step = total / count
    cum = [0.0]
    for L in segs:
        cum.append(cum[-1] + L)
    out = []
    for k in range(count):
        s = (k * step) % total
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if cum[mid + 1] < s:
                lo = mid + 1
            else:
                hi = mid
        i = min(lo, n - 1)
        L = segs[i]
        f = 0.0 if L <= 0 else (s - cum[i]) / L
        f = min(1.0, max(0.0, f))
        a, b = loc[i], loc[(i + 1) % n]
        out.append({"lat": a["lat"] + (b["lat"] - a["lat"]) * f,
                    "lng": a["lng"] + (b["lng"] - a["lng"]) * f})
    return out


def read_route_file(name):
    from util import route as route_util
    with open(os.path.join(ROOT, name)) as f:
        return route_util.parse_route(f.read())


def write_route_file(name, loc):
    body = ",".join('{"lng":"%r","lat":"%r"}' % (p["lng"], p["lat"]) for p in loc)
    with open(os.path.join(ROOT, name), "w") as f:
        f.write(body)


BACKUP_DIR = os.path.join(ROOT, "backups")
BACKUP_KEEP = 10


def _fix_owner(path):
    # root 启动时文件属 root, 尽量还给 sudo 用户, 免得删不动
    try:
        os.chmod(path, 0o644)
        uid = gid = None
        try:
            uid = int(os.environ.get("SUDO_UID", ""))
            gid = int(os.environ.get("SUDO_GID", ""))
        except ValueError:
            uid = gid = None
        if uid is not None:
            os.chown(path, uid, gid if gid is not None else -1)
    except OSError:
        pass


def _prune_backups(name, keep=BACKUP_KEEP):
    try:
        files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith(name + ".bak-"))
    except OSError:
        return
    for f in files[:-keep] if len(files) > keep else []:
        try:
            os.remove(os.path.join(BACKUP_DIR, f))
        except OSError:
            pass


def optimize_route_file(name, spacing_m=5.0, fit=True, fit_samples=8):
    from run import fit_closed_curve
    loc = read_route_file(name)
    if len(loc) < 3:
        raise ValueError(f"只有 {len(loc)} 个点, 至少 3 个")
    before = spacing_stats(loc)
    pts = fit_closed_curve(loc, samples_per_segment=max(1, int(fit_samples))) if fit else loc
    pts = even_resample(pts, float(spacing_m))
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    bak = os.path.join(BACKUP_DIR, name + ".bak-" + ts)
    full = os.path.join(ROOT, name)
    os.replace(full, bak)
    _fix_owner(bak)
    try:
        write_route_file(name, pts)
        read_route_file(name)
    except Exception:
        os.replace(bak, full)
        raise
    _prune_backups(name)
    return {"before": before, "after": spacing_stats(pts),
            "backup": os.path.relpath(bak, ROOT)}


def check_root():
    try:
        ok = (os.geteuid() == 0)
    except AttributeError:
        import ctypes
        try:
            ok = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            ok = False
    return {"id": "root", "name": "root 权限",
            "status": "pass" if ok else "fail",
            "detail": "已是 root" if ok else "当前不是 root, main.py 会直接退出",
            "hint": "" if ok else "用 ./start-ui.sh 启动 (会一次性 sudo 提权, 建 tun 隧道必需)"}


def check_python():
    ok = sys.version_info >= (3, 10)
    return {"id": "python", "name": "Python 版本",
            "status": "pass" if ok else "fail",
            "detail": sys.version.split()[0],
            "hint": "" if ok else "建议 Python 3.10+"}


def check_deps():
    import importlib
    mods = ["pymobiledevice3", "yaml", "geopy", "coloredlogs", "qh3"]
    bad, vers = [], []
    for m in mods:
        try:
            mod = importlib.import_module(m)
            try:
                from importlib.metadata import version as _v
                dist = {"yaml": "PyYAML"}.get(m, m)
                v = _v(dist)
            except Exception:
                v = getattr(mod, "__version__", "?")
            if m == "qh3":
                if str(v).split(".")[0] != "1":
                    bad.append(f"qh3 必须是 1.x (现 {v}), 与 pymobiledevice3 2.46.1 冲突")
                    continue
            vers.append(f"{m} {v}")
        except Exception as e:
            bad.append(f"{m} 导入失败: {e}")
    if bad:
        return {"id": "deps", "name": "依赖", "status": "fail",
                "detail": "; ".join(bad),
                "hint": "点本行「一键安装依赖」; 或 .venv 下 pip install -r requirements.txt; "
                        "Mac 报 openssl/ssl.h 缺失先装 openssl@3 (见 README 第 2 节); "
                        "qh3 不要升到 2.x"}
    return {"id": "deps", "name": "依赖", "status": "pass",
            "detail": ", ".join(vers), "hint": ""}


DEPS_LOCK = threading.Lock()


def _openssl_env():
    env = dict(os.environ)
    for base in ("/opt/homebrew/opt/openssl@3", "/usr/local/opt/openssl@3"):
        if os.path.isdir(os.path.join(base, "include")):
            env.setdefault("LDFLAGS", "-L%s/lib" % base)
            env.setdefault("CPPFLAGS", "-I%s/include" % base)
            env.setdefault("PKG_CONFIG_PATH", "%s/lib/pkgconfig" % base)
            break
    return env


def _fix_venv_owner():
    # sudo 起的 WebUI 装依赖会留下 root 文件, 还给 sudo 用户
    if os.name == "nt":
        return
    try:
        if os.geteuid() != 0:
            return
        uid = int(os.environ.get("SUDO_UID", ""))
    except (AttributeError, ValueError):
        return
    try:
        gid = int(os.environ.get("SUDO_GID", ""))
    except ValueError:
        gid = -1
    try:
        subprocess.run(["chown", "-R", "%d:%d" % (uid, gid),
                        os.path.join(ROOT, ".venv")],
                       timeout=120, capture_output=True)
    except Exception:
        pass


def install_deps(timeout=420):
    if not DEPS_LOCK.acquire(blocking=False):
        return False, "依赖正在安装中, 稍等半分钟再点刷新看结果"
    try:
        req = os.path.join(ROOT, "requirements.txt")
        p = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req],
            capture_output=True, text=True, timeout=timeout,
            env=_openssl_env(), cwd=ROOT)
        blob = (p.stdout or "") + "\n" + (p.stderr or "")
        tail = "; ".join(blob.strip().splitlines()[-3:])
        if p.returncode != 0:
            hint = ""
            if "openssl/ssl.h" in blob:
                hint = " ➜ Mac 缺 OpenSSL 头文件: brew install openssl@3 后重试 (见 README 第 2 节)"
            elif "qh3" in tail:
                hint = " ➜ qh3 固定 1.9.4, 别升 2.x"
            return False, "依赖安装失败: %s%s" % (tail[:500], hint)
        _fix_venv_owner()
        return True, "依赖安装成功, 点刷新重新检查"
    except subprocess.TimeoutExpired:
        return False, "pip 安装超时, 网络慢时重试, 或手动 .venv 下 pip install"
    finally:
        try:
            DEPS_LOCK.release()
        except RuntimeError:
            pass


def check_devices():
    try:
        from pymobiledevice3.usbmux import list_devices
        devs = list_devices()
    except Exception as e:
        return {"id": "device", "name": "设备连接", "status": "fail",
                "detail": f"usbmux 查询失败: {e}",
                "hint": "Windows 先装 iTunes 并打开过一次; 重插数据线"}
    n = len(devs)
    if n == 0:
        return {"id": "device", "name": "设备连接", "status": "fail",
                "detail": "没发现设备",
                "hint": "数据线直连、解锁、点信任; 同一时间只连一台"}
    if n > 1:
        return {"id": "device", "name": "设备连接", "status": "warn",
                "detail": f"发现 {n} 台: " + ", ".join(
                    f"{d.serial} {str(d.connection_type).upper()}" for d in devs),
                "hint": "同一时间只能连一台, 多台会出问题, 先拔掉多余的"}
    d = devs[0]
    return {"id": "device", "name": "设备连接", "status": "pass",
            "detail": f"{d.serial} {str(d.connection_type).upper()}", "hint": ""}


def device_info_from_values(vals):
    vals = vals or {}
    name = (vals.get("DeviceName") or vals.get("Device Name")
            or "iPhone")
    product_type = vals.get("ProductType") or "?"
    model_number = vals.get("ModelNumber") or vals.get("Model Number") or ""
    product_name = vals.get("ProductName") or ""
    ios = vals.get("ProductVersion") or "?"
    build = vals.get("BuildVersion") or ""
    serial = vals.get("SerialNumber") or ""
    try:
        from util import iphone_models as _ipm
        marketing = _ipm.marketing_name(product_type)
        display = _ipm.display_label(product_type, model_number)
    except Exception:
        marketing, display = str(product_type), str(product_type)
    region = vals.get("RegionInfo") or vals.get("ModelNumber") or ""
    return {"name": str(name), "product_type": str(product_type),
            "marketing_name": str(marketing), "display_model": str(display),
            "product_name": str(product_name), "model_number": str(model_number),
            "ios": str(ios), "build": str(build), "serial": str(serial)}


def query_device():
    info = {"connected": False, "name": "未连接", "product_type": "—",
            "marketing_name": "—", "display_model": "—",
            "product_name": "", "model_number": "", "ios": "—", "build": "",
            "serial": "", "connection": "", "locked": None, "devmode": None}
    try:
        from pymobiledevice3.usbmux import list_devices
        devs = list_devices()
        if len(devs) == 1:
            d = devs[0]
            info["serial"] = getattr(d, "serial", "") or ""
            info["connection"] = str(getattr(d, "connection_type", "") or "").upper()
        elif len(devs) > 1:
            info["connection"] = f"{len(devs)} 台设备"
    except Exception:
        pass
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        ld = create_using_usbmux(autopair=False)
        vals = getattr(ld, "all_values", {}) or {}
        info.update(device_info_from_values(vals))
        info["connected"] = True
        try:
            info["locked"] = bool(vals.get("PasswordProtected"))
        except Exception:
            pass
        try:
            major = int(str(vals.get("ProductVersion") or "").split(".")[0])
        except ValueError:
            major = 0
        if major >= 16:
            try:
                ok, dev_or_err = _call_with_timeout(lambda: bool(ld.developer_mode_status),
                                                    timeout=8)
                info["devmode"] = bool(dev_or_err) if ok else None
            except Exception:
                info["devmode"] = None
        try:
            ld.close()
        except Exception:
            pass
    except Exception:
        pass
    return info


def _call_with_timeout(fn, timeout=8):
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn)
        try:
            return True, fut.result(timeout=timeout)
        except Exception as e:
            return False, e


def check_lockdown():
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
    except Exception as e:
        return {"id": "lockdown", "name": "设备握手", "status": "fail",
                "detail": f"pymobiledevice3 异常: {e}", "hint": ""}
    ok, ld_or_err = _call_with_timeout(lambda: create_using_usbmux(autopair=False),
                                       timeout=8)
    if not ok:
        e = ld_or_err
        import concurrent.futures as _cf
        if isinstance(e, _cf.TimeoutError):
            return {"id": "lockdown", "name": "设备握手", "status": "fail",
                    "detail": "握手超时 (8s 没响应)",
                    "hint": "手机保持解锁亮屏、重插数据线; 只连一台; 没点过信任就先点信任"}
        if "NoDevice" in type(e).__name__ or "No device" in str(e):
            return {"id": "lockdown", "name": "设备握手", "status": "fail",
                    "detail": "无设备", "hint": "先连设备并信任"}
        if "Pairing" in type(e).__name__ or "pair" in str(e).lower():
            return {"id": "lockdown", "name": "设备握手", "status": "fail",
                    "detail": f"未配对/未信任 ({type(e).__name__})",
                    "hint": "手机解锁亮屏, 弹出\"信任此电脑?\"时点信任并输密码; "
                            "没弹框就重插线. 配对好再点启动"}
        return {"id": "lockdown", "name": "设备握手", "status": "fail",
                "detail": f"{type(e).__name__}: {e}",
                "hint": "设备端点信任 / 解锁后重试"}
    ld = ld_or_err
    try:
        vals = getattr(ld, "all_values", {}) or {}
    except Exception:
        vals = {}
    out = {}
    ver = (vals.get("ProductVersion") or "?")
    try:
        major = int(str(ver).split(".")[0])
    except ValueError:
        major = 0
    try:
        paired = bool(getattr(ld, "paired", True))
    except Exception:
        paired = True
    if not paired:
        out["pair"] = {
            "id": "pair", "name": "配对信任", "status": "fail",
            "detail": "未配对/未信任",
            "hint": "手机上点\"信任此电脑\"并输密码, 然后再启动. "
                    "没弹框就重插线等几秒."}
    out["ios"] = {
        "id": "ios", "name": "iOS 版本", "status": "pass" if major >= 14 else "fail",
        "detail": f"iOS {ver}",
        "hint": "" if major >= 14 else "仅支持 iOS 14 及以上"}
    locked = bool(vals.get("PasswordProtected"))
    out["unlock"] = {
        "id": "unlock", "name": "设备解锁", "status": "fail" if locked else "pass",
        "detail": "已锁, 需解锁" if locked else "已解锁",
        "hint": "解锁设备 (输密码进主屏) 后重试" if locked else ""}
    if major >= 16:
        ok2, dev_or_err = _call_with_timeout(lambda: bool(ld.developer_mode_status),
                                             timeout=8)
        if not ok2:
            import concurrent.futures as _cf2
            e = dev_or_err
            if isinstance(e, _cf2.TimeoutError):
                dev, dev_detail = False, "查询超时"
                dev_hint = "手机解锁亮屏后点刷新重试"
            else:
                dev, dev_detail = False, f"查询失败: {e}"
                dev_hint = "解锁后重试, 或用 idevicedevmodectl enable"
        else:
            dev = bool(dev_or_err)
            dev_detail = "已开启" if dev else "未开启"
            dev_hint = "" if dev else "手机 设置-隐私与安全性-开发者模式 打开, 重启手机, 或用 idevicedevmodectl enable"
    else:
        dev, dev_detail = True, "无需 (iOS <16)"
        dev_hint = ""
    out["devmode"] = {
        "id": "devmode", "name": "开发者模式", "status": "pass" if dev else "fail",
        "detail": dev_detail, "hint": dev_hint}
    try:
        ld.close()
    except Exception:
        pass
    return out


def check_route_file(path=None):
    cfg_path = path or load_config()["routeConfig"]
    full = os.path.join(ROOT, str(cfg_path))
    if not os.path.isfile(full):
        return {"id": "route", "name": "路线文件", "status": "fail",
                "detail": f"不存在: {cfg_path}", "hint": "新建 .json 粘百度 BD-09 点"}
    try:
        loc = read_route_file(cfg_path)
        st = spacing_stats(loc)
        if len(loc) < 3:
            raise ValueError(f"只有 {len(loc)} 个点, 至少 3 个")
        hint = ""
        if st["std"] > st["mean"] * 0.5:
            hint = f"点不均匀 (均值{st['mean']}m/标准差{st['std']}m), 可点一键均匀化"
        elif st["max"] > 100:
            hint = "混进了很远的点, 跑起来会像飞, 删掉它"
        return {"id": "route", "name": "路线文件", "status": "pass",
                "detail": f"{cfg_path}: {st['n']}点, 环长约{st['total']:.0f}m, "
                          f"点距{st['mean']}±{st['std']}m", "hint": hint}
    except Exception as e:
        return {"id": "route", "name": "路线文件", "status": "fail",
                "detail": f"{cfg_path} 解析失败: {e}",
                "hint": "格式: {\"lng\":\"120.x\",\"lat\":\"30.x\"} 逗号分隔, BD-09 坐标"}


def status_payload():
    checks = []
    for fn in (check_root, check_python, check_deps, check_devices):
        try:
            checks.append(fn())
        except Exception as e:
            checks.append({"id": "?", "name": "检查", "status": "fail",
                           "detail": f"{type(e).__name__}: {e}", "hint": "点↻刷新重试"})
    try:
        ld = check_lockdown()
    except Exception as e:
        ld = {"lockdown": {"id": "lockdown", "name": "设备握手", "status": "fail",
                           "detail": f"{type(e).__name__}: {e}",
                           "hint": "解锁/信任后重试"}}
    if isinstance(ld, dict) and "ios" in ld:
        for k in ("pair", "ios", "unlock", "devmode"):
            if k in ld:
                checks.append(ld[k])
    else:
        checks.append(ld if isinstance(ld, dict) else
                      {"id": "lockdown", "name": "设备握手", "status": "fail",
                       "detail": str(ld), "hint": ""})
    try:
        cfg = load_config()
        errs = validate_config(cfg)
        checks.append({"id": "config", "name": "配置", "status": "pass" if not errs else "fail",
                       "detail": "合法" if not errs else "; ".join(errs),
                       "hint": "" if not errs else "在下面表单里改"})
    except Exception as e:
        checks.append({"id": "config", "name": "配置", "status": "fail",
                       "detail": f"config.yaml 读取失败: {e}", "hint": ""})
    try:
        checks.append(check_route_file())
    except Exception as e:
        checks.append({"id": "route", "name": "路线文件", "status": "fail",
                       "detail": f"{type(e).__name__}: {e}", "hint": ""})
    try:
        ok, dev_or_err = _call_with_timeout(query_device, timeout=10)
        device = dev_or_err if ok else {"connected": False, "name": "查询超时",
                                        "product_type": "—", "ios": "—"}
    except Exception:
        device = {"connected": False, "name": "未知"}
    try:
        run_state = RUN.state()
    except Exception:
        run_state = {"running": False, "pid": None}
    return {"checks": checks, "run": run_state, "device": device, "ts": time.time()}


class RunManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.proc = None
        self.logs = deque(maxlen=3000)
        self.seq = 0
        self.started_at = None
        self.exit_code = None
        self.mode = None
        self.pin = None

    def state(self):
        with self._lock:
            running = self.proc is not None and self.proc.poll() is None
            code = self.exit_code
            if running:
                code = None
            elif self.proc is not None and code is None:
                code = self.proc.poll()
            mode = self.mode if running else None
            return {"running": running, "pid": self.proc.pid if self.proc else None,
                    "started_at": self.started_at, "exit_code": code,
                    "log_seq": self.seq, "mode": mode,
                    "pin": dict(self.pin) if (mode == "pin" and self.pin) else None}

    def _emit(self, line):
        with self._lock:
            self.seq += 1
            self.logs.append({"seq": self.seq, "line": line})
        tail = line.split("\r")[-1].strip()
        if tail and not PROGRESS_RE.match(tail):
            print(tail, flush=True)

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self._emit(line.rstrip("\n"))
        except Exception as e:
            self._emit(f"[webui] 日志读取结束: {e}")
        finally:
            with self._lock:
                try:
                    self.exit_code = self.proc.poll()
                except Exception:
                    pass

    def _gate(self, extra_gates=()):
        try:
            st = status_payload()["checks"]
        except Exception as e:
            return False, f"预检失败 ({e}), 点↻刷新重试", {}
        by_id = {c["id"]: c for c in st}
        if "lockdown" in by_id and by_id["lockdown"]["status"] != "pass":
            c = by_id["lockdown"]
            return False, (f"启动被拦下: 设备握手失败({c['detail']})"
                           + (f" ➜ {c['hint']}" if c.get("hint") else "")), by_id
        gates = ["root", "device", "pair", "unlock", "ios", "devmode"] + list(extra_gates)
        blocked = []
        for g in gates:
            c = by_id.get(g)
            if c is None:
                continue
            if c["status"] != "pass":
                blocked.append(c)
        if blocked:
            msg = "启动被拦下: " + "; ".join(
                f"{c['name']}({c['detail']})"
                + (f" ➜ {c['hint']}" if c.get("hint") else "")
                for c in blocked)
            return False, msg, by_id
        return True, "", by_id

    def _spawn(self, argv, mode, pin=None):
        try:
            if os.name == "nt":
                proc = subprocess.Popen(
                    argv, cwd=ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                proc = subprocess.Popen(
                    argv, cwd=ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, start_new_session=True)
        except Exception as e:
            return False, f"启动失败: {e}"
        with self._lock:
            self.proc = proc
            self.logs.clear()
            self.exit_code = None
            self.started_at = time.time()
            self.mode = mode
            self.pin = dict(pin) if pin else None
        threading.Thread(target=self._reader, daemon=True).start()
        label = "跑步" if mode == "run" else f"定点 {pin['lat']},{pin['lng']}"
        self._emit(f"[webui] 已启动{label} (pid {proc.pid}), 按 停止 则发 SIGINT 正常清理定位")
        return True, f"已启动{label} (pid {proc.pid})"

    def start(self):
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "已经在跑了 (pid %s)" % self.proc.pid
        ok, msg, by_id = self._gate(extra_gates=("route",))
        if not ok:
            return False, msg
        if by_id.get("config", {}).get("status") != "pass":
            return False, "配置非法: %s" % by_id.get("config", {}).get("detail", "?")
        return self._spawn([sys.executable, "main.py"], "run")

    @staticmethod
    def check_pin(lat, lng):
        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            return False, "经纬度必须是数字", (None, None)
        import math
        if not (math.isfinite(lat) and math.isfinite(lng)):
            return False, "经纬度非法", (None, None)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return False, f"超出范围 (纬度-90~90, 经度-180~180): {lat},{lng}", (None, None)
        return True, "", (round(lat, 6), round(lng, 6))

    def start_pin(self, lat, lng):
        ok, msg, (lat, lng) = self.check_pin(lat, lng)
        if not ok:
            return False, msg
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "已经在跑了 (pid %s), 先停止再定点" % self.proc.pid
        ok, msg, _ = self._gate()
        if not ok:
            return False, msg
        return self._spawn([sys.executable, "main.py", "--pin", f"{lat},{lng}"],
                           "pin", {"lat": lat, "lng": lng})

    def stop(self):
        with self._lock:
            proc = self.proc
        if proc is None:
            return False, "没在跑"
        if proc.poll() is not None:
            return False, f"进程已退出 (code {proc.poll()})"
        graceful = True
        if os.name == "nt":
            graceful = False
            self._emit("[webui] Windows 下直接结束进程 (不清定位, 需重启手机恢复)...")
            try:
                proc.terminate()
            except Exception as e:
                return False, f"结束进程失败: {e}"
        else:
            self._emit("[webui] 发 SIGINT, 等 main.py 清定位并退出 (勿直接关页面)...")
            try:
                proc.send_signal(signal.SIGINT)
            except Exception as e:
                return False, f"发信号失败: {e}"
        try:
            proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            self._emit("[webui] 25s 未退出, 升级为 terminate")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        with self._lock:
            self.exit_code = proc.poll()
        if graceful:
            self._emit(f"[webui] 已停止 (code {self.exit_code}), 定位应已清除; "
                       "若手机定位没恢复请重启手机")
            return True, "已停止, 定位已清理"
        self._emit(f"[webui] 已停止 (code {self.exit_code}), "
                   "Windows 下定位可能没清除, 请重启手机恢复")
        return True, "已停止 (Windows 下请重启手机恢复定位)"

    def snapshot_log(self, cursor):
        with self._lock:
            lines = [e for e in self.logs if e["seq"] > cursor]
            return {"cursor": self.seq, "lines": lines}

    def shutdown(self):
        with self._lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        self._emit("[webui] 清理子进程 (先 SIGINT 清定位)...")
        try:
            if os.name != "nt":
                try:
                    proc.send_signal(signal.SIGINT)
                except Exception:
                    proc.terminate()
            else:
                proc.terminate()
            try:
                proc.wait(timeout=15)
                return
            except subprocess.TimeoutExpired:
                pass
            self._emit("[webui] 子进程 15s 未退出, terminate...")
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:
                pass
            self._emit("[webui] 子进程仍未退出, kill...")
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        finally:
            with self._lock:
                try:
                    self.exit_code = proc.poll()
                except Exception:
                    pass


RUN = RunManager()


class Handler(BaseHTTPRequestHandler):
    server_version = SERVICE_TAG

    def log_message(self, fmt, *args):
        msg = fmt % args
        if '"GET /api/' in msg:
            return
        sys.stderr.write("[webui-http] " + msg + "\n")

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json")

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode() or "{}")

    _STATIC_TYPES = {".css": "text/css", ".js": "text/javascript",
                     ".svg": "image/svg+xml"}

    def _serve_static(self, name):
        full = os.path.normpath(os.path.join(STATIC_DIR, name))
        if not full.startswith(os.path.abspath(STATIC_DIR) + os.sep):
            self._json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(full)[1].lower()
        if ext not in self._STATIC_TYPES or not os.path.isfile(full):
            self._json({"error": "not found"}, 404)
            return
        with open(full, "rb") as f:
            self._send(200, f.read(), self._STATIC_TYPES[ext])

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(p.query)
        if p.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html")
        elif p.path.startswith("/static/"):
            self._serve_static(p.path[len("/static/"):])
        elif p.path == "/api/status":
            try:
                self._json(status_payload())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif p.path == "/api/config":
            try:
                self._json({"config": load_config()})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif p.path == "/api/routes":
            try:
                files = sorted(f for f in os.listdir(ROOT) if f.endswith(".json"))
                active = load_config()["routeConfig"]
                items = []
                for f in files:
                    chk = check_route_file(f)
                    items.append({"file": f, "active": f == active,
                                  "status": chk["status"], "detail": chk["detail"]})
                self._json({"routes": items, "active": active})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif p.path == "/api/run/state":
            self._json(RUN.state())
        elif p.path == "/api/server/info":
            self._json({"service": SERVICE_TAG, "run": RUN.state(),
                        "pid": os.getpid(), "port_hint": "张雪峰 WebUI"})
        elif p.path == "/api/port/check":
            try:
                from util import ports as _ports
                port = int(q.get("port", ["0"])[0] or 0)
                if port <= 0:
                    self._json({"error": "缺少 ?port="}, 400)
                else:
                    self._json(_ports.check_port(port))
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif p.path == "/api/log":
            try:
                cursor = int(q.get("cursor", ["0"])[0])
            except ValueError:
                cursor = 0
            self._json(RUN.snapshot_log(cursor))
        elif p.path == "/api/preview":
            try:
                from run import bd09Towgs84, build_lap, loop_length
                from run import resolve_speed, speed_to_pace_str
                cfg = load_config()
                loc = read_route_file(cfg["routeConfig"])
                v = resolve_speed()
                dt = float(cfg["dt"])
                geo_dt = max(dt, 0.5)
                lap = build_lap(loc, v, dt=geo_dt, seed=42)
                lap_s = len(lap) * geo_dt
                lap_points = int(round(lap_s / dt)) if dt > 0 else len(lap)
                thin = max(1, len(lap) // 300)
                lap_show = lap[::thin]
                raw = loc if len(loc) <= 2000 else loc[::len(loc) // 2000 + 1]
                self._json({
                    "points": len(loc),
                    "loop_m": round(loop_length(loc), 1),
                    "spacing": spacing_stats(loc),
                    "raw": raw,
                    "lap": lap_show,
                    "lap_points": lap_points,
                    "lap_s": round(lap_s, 1),
                    "speed": round(v, 2),
                    "pace": speed_to_pace_str(v),
                    "wgs_raw": [[bd09Towgs84(p)["lat"], bd09Towgs84(p)["lng"]] for p in raw],
                    "wgs_lap": [[bd09Towgs84(p)["lat"], bd09Towgs84(p)["lng"]] for p in lap_show],
                })
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        body = self._body_json()
        if p.path == "/api/run/start":
            ok, msg = RUN.start()
            self._json({"ok": ok, "msg": msg}, 200 if ok else 409)
        elif p.path == "/api/run/stop":
            ok, msg = RUN.stop()
            self._json({"ok": ok, "msg": msg}, 200 if ok else 409)
        elif p.path == "/api/pin/start":
            ok, msg = RUN.start_pin(body.get("lat"), body.get("lng"))
            self._json({"ok": ok, "msg": msg,
                        "state": RUN.state()}, 200 if ok else 409)
        elif p.path == "/api/pin/stop":
            ok, msg = RUN.stop()
            self._json({"ok": ok, "msg": msg,
                        "state": RUN.state()}, 200 if ok else 409)
        elif p.path == "/api/port/kill":
            try:
                from util import ports as _ports
                port = int(body.get("port") or 0)
                if port <= 0:
                    self._json({"ok": False, "msg": "缺少 port"}, 400)
                    return
                st = _ports.check_port(port)
                if not st["open"]:
                    self._json({"ok": True, "msg": f"端口 {port} 已空闲", "result": {}})
                    return
                if not st["same"]:
                    self._json({"ok": False,
                                "msg": f"端口 {port} 被其他服务占用 ({st['detail']}), "
                                       "拒绝一键 kill, 请手动确认",
                                "check": st}, 409)
                    return
                want = body.get("pids") or st["pids"]
                try:
                    pids = [int(x) for x in want]
                except (TypeError, ValueError):
                    self._json({"ok": False, "msg": "pids 非法"}, 400)
                    return
                pids = [x for x in pids if x in st["pids"]]
                if not pids:
                    self._json({"ok": False, "msg": "没有可杀的同类进程"}, 404)
                    return
                res = _ports.kill_pids(pids)
                self._json({"ok": True, "msg": f"已处理端口 {port} 的同类进程: {res}",
                            "result": res, "check": st})
            except Exception as e:
                self._json({"ok": False, "msg": str(e)}, 500)
        elif p.path == "/api/deps/install":
            try:
                ok, msg = install_deps()
                self._json({"ok": ok, "msg": msg}, 200 if ok else 500)
            except Exception as e:
                self._json({"ok": False, "msg": str(e)}, 500)
        elif p.path == "/api/routes/select":
            try:
                name = body.get("file", "")
                full = os.path.join(ROOT, name)
                if not name.endswith(".json") or not os.path.isfile(full):
                    self._json({"ok": False, "msg": "文件不存在"}, 400)
                    return
                save_config_inplace({"routeConfig": name})
                self._json({"ok": True, "msg": f"已切换到 {name}"})
            except Exception as e:
                self._json({"ok": False, "msg": str(e)}, 500)
        elif p.path == "/api/routes/create":
            try:
                name = (body.get("file") or "").strip()
                content = body.get("content") or ""
                if not re.fullmatch(r"[\w\-]+\.json", name):
                    self._json({"ok": False,
                                "msg": "文件名只能字母/数字/下划线/横杠, 以 .json 结尾"}, 400)
                    return
                from util import route as route_util
                loc = route_util.parse_route(content)
                if len(loc) < 3:
                    self._json({"ok": False, "msg": f"只有 {len(loc)} 个点, 至少 3 个"}, 400)
                    return
                with open(os.path.join(ROOT, name), "w") as f:
                    f.write(content)
                save_config_inplace({"routeConfig": name})
                self._json({"ok": True, "msg": f"已保存 {name} ({len(loc)}点) 并设为当前路线"})
            except Exception as e:
                self._json({"ok": False, "msg": f"解析失败: {e}"}, 400)
        elif p.path == "/api/routes/optimize":
            try:
                name = (body.get("file") or load_config()["routeConfig"]).strip()
                spacing = float(body.get("spacing_m", 5.0))
                if not name.endswith(".json") or not os.path.isfile(os.path.join(ROOT, name)):
                    self._json({"ok": False, "msg": "文件不存在"}, 400)
                    return
                if not 1.0 <= spacing <= 50.0:
                    self._json({"ok": False, "msg": "间距建议 1~50 米"}, 400)
                    return
                res = optimize_route_file(name, spacing_m=spacing)
                b, a = res["before"], res["after"]
                self._json({"ok": True,
                            "msg": f"{name}: {b['n']}点→{a['n']}点, "
                                   f"点距 {b['mean']}±{b['std']}m → {a['mean']}±{a['std']}m, "
                                   f"原文件备份为 {res['backup']}",
                            "result": res})
            except Exception as e:
                self._json({"ok": False, "msg": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def do_PUT(self):
        p = urllib.parse.urlparse(self.path)
        if p.path != "/api/config":
            self._json({"error": "not found"}, 404)
            return
        body = self._body_json()
        try:
            cfg = load_config()
            for k in KNOWN_KEYS:
                if k in body:
                    cfg[k] = body[k]
            if cfg.get("pace") in ("", "null"):
                cfg["pace"] = None
            errs = validate_config(cfg)
            if errs:
                self._json({"ok": False, "msg": "; ".join(errs)}, 400)
                return
            save_config_inplace({k: cfg[k] for k in KNOWN_KEYS})
            self._json({"ok": True, "msg": "已保存", "config": load_config()})
        except Exception as e:
            self._json({"ok": False, "msg": str(e)}, 500)


def load_page():
    try:
        with open(TEMPLATE_PATH, encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return ("<h1>模板缺失</h1><p>templates/index.html 读取失败: %s</p>"
                % e)


PAGE = load_page()


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="张雪峰 WebUI")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8123)))
    args = ap.parse_args()
    try:
        srv = QuietThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        import errno
        if e.errno in (errno.EADDRINUSE, 48, 98):
            try:
                from util import ports as _ports
                st = _ports.check_port(args.port)
            except Exception:
                st = None
            print(f"失败: 端口 {args.port} 已被占用.", flush=True)
            if st and st.get("open"):
                for pid in st.get("pids", []):
                    cmd = (st.get("cmds") or {}).get(str(pid), "")
                    print(f"  占用进程 pid {pid} {cmd}", flush=True)
                print(f"  判断: {st.get('detail')}", flush=True)
                if st.get("same"):
                    print(f"解决办法: 同类服务 (上次 WebUI 没退). 一键清理后重起:", flush=True)
                    print(f"  ./start-ui.sh --port {args.port} --kill", flush=True)
                    print(f"  或换端口: ./start-ui.sh --port {args.port + 1}", flush=True)
                else:
                    print("解决办法: 被其他服务占用. 换端口重起:", flush=True)
                    print(f"  ./start-ui.sh --port {args.port + 1}", flush=True)
                    print(f"  查谁占着: lsof -i :{args.port}", flush=True)
            else:
                print(f"解决办法: 换端口 (./start-ui.sh --port {args.port + 1}) "
                      f"或查占用 (lsof -i :{args.port})", flush=True)
            sys.exit(1)
        raise

    def _on_term(signum, frame):
        print(f"收到信号 {signum}, 停止子进程并退出...", flush=True)
        RUN.shutdown()
        try:
            srv.server_close()
        except Exception:
            pass
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _on_term)
    if signal.getsignal(signal.SIGINT) == signal.default_int_handler:
        signal.signal(signal.SIGINT, _on_term)
    print(f"WebUI: http://127.0.0.1:{args.port}")
    print("说明: 需 root 才能启动跑步 (建 tun); 用 ./start-ui.sh 一键启动.")
    print("停止 WebUI 请 Ctrl+C (会先给 main.py 发 SIGINT 清理定位, 等子进程退出).")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("WebUI 退出, 清理子进程...")
        RUN.shutdown()
        try:
            srv.server_close()
        except Exception:
            pass
        print("清理完成.", flush=True)


if __name__ == "__main__":
    main()
