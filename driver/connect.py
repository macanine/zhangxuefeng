import asyncio
import logging
import multiprocessing
import os
import time

logger = logging.getLogger(__name__)


class ConnectError(Exception):

    def __init__(self, msg, hint=""):
        super().__init__(msg)
        self.hint = hint

    def full(self):
        return f"{self} {('➜ ' + self.hint) if self.hint else ''}".strip()


class NoDeviceError(ConnectError):
    pass


class PairingNeededError(ConnectError):

    def __init__(self, msg="设备尚未配对/信任",
                 hint="手机保持解锁亮屏, 弹出\"信任此电脑?\"时点信任并输入锁屏密码, 然后重试. "
                      "同一时间只连一台, 用数据线直连 (别用拓展坞)."):
        super().__init__(msg, hint)


class LockedError(ConnectError):
    def __init__(self):
        super().__init__("设备已锁定",
                         "解锁 iPhone (输密码进主屏, 保持亮屏), 然后重试.")


class DeveloperModeError(ConnectError):
    def __init__(self, detail="开发者模式未开启"):
        super().__init__(detail,
                         "手机 设置-隐私与安全性-开发者模式 打开, 重启手机, "
                         "重启后按提示确认, 再重跑. 或命令行 idevicedevmodectl enable")


class TunnelError(ConnectError):
    pass


def is_root():
    try:
        return os.geteuid() == 0
    except AttributeError:
        import ctypes
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False


def list_usb_devices():
    try:
        from pymobiledevice3.usbmux import list_devices
        return list(list_devices()), ""
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def _explain_usbmux_error(e):
    s = f"{type(e).__name__}: {e}"
    if "NoDevice" in type(e).__name__ or "No device" in str(e):
        return ("没发现设备", "数据线直连、解锁、点信任; 同一时间只连一台; "
                "Windows 先装 iTunes 并打开过一次.")
    return (f"usbmux 握手失败 ({s})", "重插数据线、解锁后重试; 只连一台.")


def get_lockdown(autopair=False, pair_timeout=None):
    from pymobiledevice3.lockdown import create_using_usbmux
    try:
        return create_using_usbmux(autopair=autopair,
                                   pair_timeout=pair_timeout)
    except Exception as e:
        name = type(e).__name__
        msg = str(e)
        if "NoDevice" in name or "No device" in msg:
            raise NoDeviceError("没发现设备",
                                "数据线直连、解锁、点信任; 同一时间只连一台.") from e
        if "PairingDialog" in name or "信任" in msg or "trust" in msg.lower():
            raise PairingNeededError() from e
        if "Passcode" in name or "Password" in name:
            raise PairingNeededError("设备要求输入密码后才能配对",
                                     "手机上输锁屏密码进主屏, 保持亮屏, 再重试.") from e
        if "Pairing" in name or "pair" in msg.lower():
            raise PairingNeededError(f"配对失败 ({name}: {msg})") from e
        detail, hint = _explain_usbmux_error(e)
        raise ConnectError(detail, hint) from e


def wait_for_trust(pair_timeout=60, poll=1.0, on_wait=None):
    from pymobiledevice3.exceptions import PairingDialogResponsePendingError
    deadline = time.time() + max(1, pair_timeout)
    last_msg = ""
    while True:
        try:
            ld = get_lockdown(autopair=True, pair_timeout=0)
            return ld
        except PairingNeededError as e:
            remain = int(deadline - time.time())
            if remain <= 0:
                raise PairingNeededError(
                    f"配对超时 ({pair_timeout}s 内没在手机上点信任)",
                    "手机保持解锁亮屏, 弹出\"信任此电脑?\"时点信任并输密码, 然后重跑. "
                    "没弹框就重插线.") from e
            msg = f"等待信任… ({remain}s) 请在手机上点\"信任此电脑\"并输密码"
            if msg != last_msg and on_wait:
                on_wait(msg)
            last_msg = msg
            time.sleep(poll)
        except (NoDeviceError, LockedError):
            raise
        except ConnectError:
            raise


def ensure_usbmux_paired(pair_timeout=60, on_wait=None):
    devs, err = list_usb_devices()
    if err:
        raise ConnectError(f"usbmux 查询失败: {err}",
                           "重插数据线; Windows 先装 iTunes 并打开过一次.")
    if len(devs) == 0:
        raise NoDeviceError("没发现设备",
                            "数据线直连、解锁、点信任; 同一时间只连一台; "
                            "换根数据线/口试试.")
    if len(devs) > 1:
        serials = ", ".join(getattr(d, "serial", "?") for d in devs)
        raise ConnectError(f"发现 {len(devs)} 台设备 ({serials}), 一次只能连一台",
                           "拔掉多余的设备, 只留一台再跑.")
    try:
        ld = get_lockdown(autopair=False)
    except PairingNeededError:
        ld = None
    except ConnectError:
        raise
    if ld is not None:
        if bool(getattr(ld, "paired", False)):
            vals = getattr(ld, "all_values", {}) or {}
            if vals.get("PasswordProtected"):
                raise LockedError()
            return ld
    print(f"需配对: 请在手机上点\"信任此电脑\" (最多等 {pair_timeout}s)…")
    ld = wait_for_trust(pair_timeout=pair_timeout, on_wait=on_wait)
    vals = getattr(ld, "all_values", {}) or {}
    if vals.get("PasswordProtected"):
        raise LockedError()
    print("配对成功.")
    return ld


def get_ios_version(lockdown):
    vals = getattr(lockdown, "all_values", {}) or {}
    return vals.get("ProductVersion") or "?"


def check_ios_version(lockdown, minimum=14):
    ver = str(get_ios_version(lockdown))
    try:
        major = int(ver.split(".")[0])
    except ValueError:
        raise ConnectError(f"读不到 iOS 版本 ({ver})", "重插线解锁后重试.")
    if major < minimum:
        raise ConnectError(f"iOS {ver} 太旧, 仅支持 iOS {minimum}+",
                           f"升级到 iOS {minimum}+ 后再跑.")
    return ver


def check_developer_mode(lockdown):
    try:
        ok = bool(lockdown.developer_mode_status)
    except Exception as e:
        raise ConnectError(f"开发者模式查询失败 ({type(e).__name__}: {e})",
                           "解锁后重试; 若持续失败, 用 idevicedevmodectl enable 试试.") from e
    if not ok:
        raise DeveloperModeError()
    return True


DDI_REPO = "doronz88/DeveloperDiskImage"
DDI_REF = "main"
DDI_IMAGE_NAME = "DeveloperDiskImage.dmg"
DDI_SIGNATURE_NAME = "DeveloperDiskImage.dmg.signature"
# 官方源放最前, 后面是国内可达的 GitHub 加速镜像 (前缀代理). 镜像内容由设备端签名校验,
# 对不上会被拒绝挂载, 所以走第三方镜像不改变信任根.
DDI_MIRROR_TEMPLATES = (
    "https://raw.githubusercontent.com/{repo}/{ref}",
    "https://ghproxy.net/https://raw.githubusercontent.com/{repo}/{ref}",
    "https://gh-proxy.com/https://raw.githubusercontent.com/{repo}/{ref}",
)
DDI_TIMEOUT = 30
DDI_MIN_IMAGE_BYTES = 1024 * 1024


def _ddi_setting(name, env):
    import os
    from pathlib import Path
    import yaml
    val = os.environ.get(env)
    if val:
        return val.strip()
    try:
        with open(Path(__file__).resolve().parent.parent / "config.yaml") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return ""
    got = cfg.get(name)
    return str(got).strip() if got not in (None, "") else ""


def _ddi_version(lockdown):
    ver = str(get_ios_version(lockdown))
    parts = ver.split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0]}.{parts[1]}"
    return ver


def _ddi_override_dir():
    from pathlib import Path
    val = _ddi_setting("ddi_dir", "ZXF_DDI_DIR")
    return Path(val).expanduser() if val else None


def _ddi_cache_dir(version):
    from pymobiledevice3.common import get_home_folder
    return get_home_folder() / "DeveloperDiskImages" / version


def _ddi_project_dir(version):
    from pathlib import Path
    return Path(__file__).resolve().parent.parent / ".ddi" / version


def _ddi_dirs(version):
    from pathlib import Path
    dirs = []
    override = _ddi_override_dir()
    if override is not None:
        dirs.append(override)
    dirs.append(_ddi_cache_dir(version))
    dirs.append(_ddi_project_dir(version))
    for root in (Path("/Applications/Xcode.app"), Path.home() / "Xcode.app"):
        dirs.append(root / "Contents" / "Developer" / "Platforms"
                    / "iPhoneOS.platform" / "DeviceSupport" / version)
    return dirs


def _ddi_local(version):
    for d in _ddi_dirs(version):
        image = d / DDI_IMAGE_NAME
        signature = d / DDI_SIGNATURE_NAME
        if image.is_file() and signature.is_file():
            return image, signature
    return None


def _ddi_download(version):
    import requests
    override = _ddi_setting("ddi_mirror", "ZXF_DDI_MIRROR")
    templates = ((override,) if override else ()) + DDI_MIRROR_TEMPLATES
    rel = f"DeveloperDiskImages/{version}"
    last_err = ""
    for tmpl in templates:
        base = tmpl.format(repo=DDI_REPO, ref=DDI_REF)
        try:
            img = requests.get(f"{base}/{rel}/{DDI_IMAGE_NAME}", timeout=DDI_TIMEOUT)
            sig = requests.get(f"{base}/{rel}/{DDI_SIGNATURE_NAME}", timeout=DDI_TIMEOUT)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.warning(f"下载开发者镜像失败 ({base}): {last_err}")
            continue
        if img.status_code == 404 or sig.status_code == 404:
            raise ConnectError(
                f"镜像仓库里没有 iOS {version} 的 DeveloperDiskImage",
                "确认手机系统版本号; 或手动放镜像 (见 README 第8节).")
        if img.status_code != 200 or sig.status_code != 200:
            last_err = f"HTTP {img.status_code}/{sig.status_code}"
            continue
        if (len(img.content) < DDI_MIN_IMAGE_BYTES
                or img.content[:1024].lstrip().lower().startswith(b"<")):
            last_err = "镜像内容异常 (可能被镜像站返回了错误页)"
            continue
        saved = _ddi_save(version, img.content, sig.content)
        if saved is not None:
            logger.info(f"开发者镜像已保存到 {saved[0].parent}")
            return saved
        last_err = "下载成功但没找到可写目录"
    raise ConnectError(
        f"下载 DeveloperDiskImage 失败 ({last_err})",
        "保持联网; 或用 ZXF_DDI_DIR 指定本地镜像目录, "
        "ZXF_DDI_MIRROR 指定可用镜像站, 或设 HTTPS_PROXY 后重试.")


def _ddi_save(version, image, signature):
    from pathlib import Path
    override = _ddi_override_dir()
    dests = [override] if override is not None else []
    dests += [_ddi_cache_dir(version), _ddi_project_dir(version)]
    for dest in dests:
        try:
            Path(dest).mkdir(parents=True, exist_ok=True)
            (Path(dest) / DDI_IMAGE_NAME).write_bytes(image)
            (Path(dest) / DDI_SIGNATURE_NAME).write_bytes(signature)
        except OSError as e:
            logger.warning(f"写镜像缓存失败 ({dest}: {e}), 换下一个位置")
            continue
        return Path(dest) / DDI_IMAGE_NAME, Path(dest) / DDI_SIGNATURE_NAME
    return None


def ensure_developer_image(lockdown):
    from pymobiledevice3.exceptions import (AlreadyMountedError,
                                            DeveloperModeIsNotEnabledError)
    from pymobiledevice3.services.mobile_image_mounter import DeveloperDiskImageMounter

    mounter = DeveloperDiskImageMounter(lockdown)
    try:
        if mounter.is_image_mounted(mounter.IMAGE_TYPE):
            return "已挂载"
    except Exception:
        pass

    version = _ddi_version(lockdown)
    found = _ddi_local(version)
    if found is None:
        found = _ddi_download(version)
    image, signature = found
    try:
        mounter.mount(image, signature)
    except AlreadyMountedError:
        return "已挂载"
    except DeveloperModeIsNotEnabledError as e:
        raise ConnectError("开发者镜像需要先开开发者模式",
                           "手机 设置-隐私与安全性-开发者模式 打开, 重启手机后再跑.") from e
    except Exception as e:
        raise ConnectError(
            f"挂载开发者镜像失败 ({type(e).__name__}: {e})",
            f"镜像版本要和系统一致 (需要 {version}); 手机解锁亮屏、"
            "重插线后重试; 反复失败可重插线重启手机.") from e
    return f"已挂载 ({version})"


def discover_rsd(timeout=15):
    if not is_root():
        raise ConnectError("建隧道需要 root/管理员权限, 当前不是 root",
                           "Mac 用 sudo .venv/bin/python main.py, "
                           "WebUI 用 ./start-ui.sh 启动 (它会一次性 sudo 提权).")
    try:
        from pymobiledevice3.exceptions import AccessDeniedError
    except Exception:
        AccessDeniedError = ()
    try:
        from pymobiledevice3.cli.remote import get_device_list
        devs = get_device_list()
    except Exception as e:
        if AccessDeniedError and isinstance(e, AccessDeniedError):
            raise ConnectError("无权暂停系统 remoted (需要 root)",
                               "用 sudo / ./start-ui.sh 以 root 启动后再试.") from e
        raise ConnectError(f"RSD 发现失败 ({type(e).__name__}: {e})",
                           "手机解锁亮屏、只连一台; 重插线后重试.") from e
    if not devs:
        raise NoDeviceError("RSD 发现为空 (Bonjour 没看到手机)",
                            "①必须 root (sudo/start-ui.sh); ②数据线直连+解锁+已信任; "
                            "③只连一台; ④重插线, 等 5 秒再跑; ⑤换线/口.")
    if len(devs) > 1:
        raise ConnectError(f"发现 {len(devs)} 台 RSD 设备, 一次只能连一台",
                           "拔掉多余设备只留一台.")
    return devs[0]


async def open_tunnel(rsd, queue=None, prefer=None):
    from driver.quic_compat import ensure_patched
    from pymobiledevice3.cli.remote import start_tunnel
    from pymobiledevice3.remote.common import TunnelProtocol
    ensure_patched()
    order = [TunnelProtocol.QUIC, TunnelProtocol.TCP] if prefer is None else list(prefer)
    last_err = None
    for proto in order:
        try:
            logger.warning(f"建隧道… ({proto.name}, 最多等 60s)")
            async with start_tunnel(rsd, None, protocol=proto) as r:
                if queue is not None:
                    queue.put({"ok": True, "address": r.address, "port": r.port,
                               "protocol": proto.name})
                await r.client.wait_closed()
                return
        except Exception as e:
            last_err = e
            logger.warning(f"{proto.name} 隧道失败 ({type(e).__name__}: {e}), "
                           f"{'回落 TCP' if proto == TunnelProtocol.QUIC else '无后路了'}")
    if queue is not None:
        queue.put({"ok": False, "error": f"{type(last_err).__name__}: {last_err}"})
    else:
        raise TunnelError(f"隧道都建不起来 (QUIC/TCP 全败: {last_err})",
                          "手机解锁亮屏、重插线; 必须 root; 换线/口; "
                          "若刚升级 iOS, 重启手机+电脑再试.")


def get_usbmux_lockdownclient(pair_timeout=60):
    return ensure_usbmux_paired(pair_timeout=pair_timeout,
                                on_wait=lambda m: print(m, flush=True))


def get_version(lockdown):
    return get_ios_version(lockdown)


def get_developer_mode_status(lockdown):
    return lockdown.developer_mode_status


def reveal_developer_mode(lockdown):
    from pymobiledevice3.services.amfi import AmfiService
    AmfiService(lockdown).create_amfi_show_override_path_file()


def enable_developer_mode(lockdown):
    from pymobiledevice3.services.amfi import AmfiService
    AmfiService(lockdown).enable_developer_mode()


def get_serverrsd(timeout=15):
    from pymobiledevice3.cli.remote import install_driver_if_required, verify_tunnel_imports
    install_driver_if_required()
    if not verify_tunnel_imports():
        raise TunnelError("隧道依赖缺失 (qh3/openssl 等)",
                          ".venv 下 pip install -r requirements.txt; qh3 固定 1.x 别升 2.x.")
    return discover_rsd(timeout=timeout)


def tunnel(rsd: "object", queue: multiprocessing.Queue):
    asyncio.run(open_tunnel(rsd, queue=queue))
