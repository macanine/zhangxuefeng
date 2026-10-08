import sys

from driver import connect


class InitError(Exception):
    def __init__(self, msg, hint=""):
        super().__init__(msg)
        self.hint = hint


def check_platform():
    if sys.platform == "win32":
        return
    if sys.platform == "darwin":
        return
    raise InitError(f"不支持的系统: {sys.platform}", "仅支持 macOS 和 Windows.")


def check_root():
    if not connect.is_root():
        if sys.platform == "win32":
            raise InitError("请以管理员权限运行",
                            "右键终端-以管理员身份运行, 再执行.")
        raise InitError("请以 root 权限运行",
                        "Mac 用 sudo .venv/bin/python main.py; "
                        "WebUI 用 ./start-ui.sh 启动 (会自动提权). 建 tun 隧道必需.")


def preflight(pair_timeout=60):
    check_platform()
    check_root()
    print("[1/4] 查设备与配对… (保持手机解锁亮屏)", flush=True)
    try:
        lockdown = connect.ensure_usbmux_paired(
            pair_timeout=pair_timeout,
            on_wait=lambda m: print(f"  {m}", flush=True))
    except connect.ConnectError as e:
        raise InitError(str(e), getattr(e, "hint", "")) from e
    print("[2/4] 查系统版本与开发者模式…", flush=True)
    try:
        ver = connect.check_ios_version(lockdown)
    except connect.ConnectError as e:
        raise InitError(str(e), getattr(e, "hint", "")) from e
    print(f"  系统版本 {ver}", flush=True)
    major = int(str(ver).split(".")[0]) if str(ver).split(".")[0].isdigit() else 0
    if major >= 16:
        try:
            connect.check_developer_mode(lockdown)
        except connect.DeveloperModeError as e:
            try:
                connect.reveal_developer_mode(lockdown)
            except Exception:
                pass
            raise InitError(str(e), getattr(e, "hint", "")) from e
        except connect.ConnectError as e:
            raise InitError(str(e), getattr(e, "hint", "")) from e
        print("  开发者模式已开", flush=True)
    else:
        print("  开发者模式: 无需 (iOS <16)", flush=True)
    return {"lockdown": lockdown, "version": ver}


def init(pair_timeout=60):
    try:
        preflight(pair_timeout=pair_timeout)
    except InitError as e:
        print(f"预检失败: {e}", flush=True)
        if e.hint:
            print(f"解决办法: {e.hint}", flush=True)
        sys.exit(1)
    print("预检通过.", flush=True)
