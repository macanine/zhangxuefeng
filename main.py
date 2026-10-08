import argparse
import logging
import os
import signal
import sys
import time

import coloredlogs

import config
import run
from driver import connect, location
from init import init as preflight_mod
from init import route as route_mod
from init import tunnel as tunnel_mod

DEBUG = os.environ.get("DEBUG", False)

coloredlogs.install(level=logging.DEBUG if DEBUG else logging.INFO)
for noisy in ("wintun", "quic", "asyncio", "zeroconf", "urllib3.connectionpool"):
    logging.getLogger(noisy).setLevel(logging.DEBUG if DEBUG else logging.WARNING)

logger = logging.getLogger(__name__)


def die(msg, hint="", code=1):
    print(f"失败: {msg}", flush=True)
    if hint:
        print(f"解决办法: {hint}", flush=True)
    sys.exit(code)


def hold_pin(dvt, lat, lng, refresh_s=2.0):
    location.set_location(dvt, lat=lat, lng=lng)
    print(f"定点就绪: {lat},{lng} (保持中, Ctrl+C 清除退出)", flush=True)
    while True:
        time.sleep(refresh_s)
        location.set_location(dvt, lat=lat, lng=lng)


def _clear_location(dvt):
    logger.debug("Start to clear location")
    try:
        location.clear_location(dvt)
    except Exception as e:
        print(f"清定位失败 ({e}), 重启手机可恢复.", flush=True)
    else:
        logger.info("Location cleared")


def _serve_device(dvt, pin, loc):
    if pin is not None:
        try:
            print(f"设备已连接, 定点 {pin[0]},{pin[1]}, "
                  "Ctrl+C 清除并退出...", flush=True)
            hold_pin(dvt, pin[0], pin[1])
        except KeyboardInterrupt:
            logger.debug("get KeyboardInterrupt (pin)")
        finally:
            _clear_location(dvt)
        return
    try:
        v = run.resolve_speed()
        print("设备已连接, 开始模拟跑步...", flush=True)
        run.run(dvt, loc, v)
    except ValueError as e:
        die(f"配速非法 ({e})", "config.yaml 的 pace 如 \"4:25\", 不能为空.")
    except KeyboardInterrupt:
        logger.debug("get KeyboardInterrupt (inner)")
    finally:
        _clear_location(dvt)


def main():
    ap = argparse.ArgumentParser(description="iOS 14+ 真机模拟跑步 / 定点定位")
    ap.add_argument("--pair-timeout", type=float, default=60,
                    help="等手机点信任的最长秒数 (默认60)")
    ap.add_argument("--tunnel-timeout", type=float, default=90,
                    help="建隧道最长等待秒数 (默认90)")
    ap.add_argument("--pair-only", action="store_true",
                    help="只做预检配对, 不建隧道不跑步")
    ap.add_argument("--pin", type=str, default="",
                    help='定点模式: "纬度,经度" (WGS-84, 与地图点击值一致), '
                         '如 --pin "30.304123,120.085456"')
    args = ap.parse_args()

    pin = None
    if args.pin:
        try:
            lat_s, lng_s = str(args.pin).split(",")
            pin = (float(lat_s.strip()), float(lng_s.strip()))
            if not (-90 <= pin[0] <= 90 and -180 <= pin[1] <= 180):
                raise ValueError("超出经纬度范围")
        except Exception as e:
            die(f"定点坐标非法: {args.pin!r} ({e})",
                '格式: --pin "纬度,经度", 如 --pin "30.304123,120.085456"')

    try:
        pf = preflight_mod.preflight(pair_timeout=args.pair_timeout)
    except preflight_mod.InitError as e:
        die(e, getattr(e, "hint", ""))
    print("预检通过.", flush=True)
    version = pf.get("version", "17")
    lockdown = pf.get("lockdown")
    major = int(str(version).split(".")[0]) if str(version).split(".")[0].isdigit() else 0
    if args.pair_only:
        print("只做配对检查, 退出.", flush=True)
        return

    loc = None
    if pin is None:
        try:
            loc = route_mod.get_route()
            print(f"路线就绪: {config.config.routeConfig} ({len(loc)}点)", flush=True)
        except FileNotFoundError as e:
            die(f"路线文件不存在: {config.config.routeConfig}", "新建 .json 粘百度 BD-09 点, 见 README 第4节.")
        except Exception as e:
            die(f"路线解析失败: {type(e).__name__}: {e}",
                "格式: {\"lng\":\"120.x\",\"lat\":\"30.x\"} 逗号分隔, BD-09 坐标.")
    else:
        print(f"定点模式: {pin[0]},{pin[1]} (WGS-84)", flush=True)

    from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import DvtSecureSocketProxyService

    if major >= 17:
        print(f"[3/4] 建隧道… (QUIC优先, 失败回落TCP, 最多等{args.tunnel_timeout:.0f}s)", flush=True)
        original_sigint_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            process, address, port = tunnel_mod.tunnel(timeout=args.tunnel_timeout)
        except connect.ConnectError as e:
            signal.signal(signal.SIGINT, original_sigint_handler)
            die(e, getattr(e, "hint", ""))
        except Exception as e:
            signal.signal(signal.SIGINT, original_sigint_handler)
            die(f"建隧道异常 ({type(e).__name__}: {e})", "重插线解锁后重试; 必须 root; 只连一台.")
        signal.signal(signal.SIGINT, original_sigint_handler)
        print(f"隧道就绪 {address}:{port}", flush=True)

        try:
            from pymobiledevice3.cli.remote import RemoteServiceDiscoveryService
            print("[4/4] 连设备服务…", flush=True)
            try:
                with RemoteServiceDiscoveryService((address, port)) as rsd:
                    try:
                        with DvtSecureSocketProxyService(rsd) as dvt:
                            _serve_device(dvt, pin, loc)
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        die(f"DVT 服务失败 ({type(e).__name__}: {e})",
                            "开发者模式开了吗? 手机解锁亮屏了吗? 重插线+重启手机再试. "
                            "若刚点过信任, 等几秒再跑.")
            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except Exception as e:
                if "die" in str(type(e).__name__).lower():
                    raise
                die(f"RSD 连接失败 ({type(e).__name__}: {e})",
                    "隧道建好了但连不上手机服务: 手机保持解锁亮屏, 重插线, 重启手机再试.")
        except KeyboardInterrupt:
            logger.debug("get KeyboardInterrupt (outer)")
        finally:
            logger.debug("terminating tunnel process")
            tunnel_mod.stop_tunnel(process)
            logger.info("tunnel process terminated")
            print("Bye", flush=True)
    else:
        print(f"[3/3] 连设备服务… (iOS {version} 老路径, 免隧道)", flush=True)
        try:
            print("  挂开发者镜像 (DeveloperDiskImage)…", flush=True)
            print(f"  开发者镜像 {connect.ensure_developer_image(lockdown)}", flush=True)
            with DvtSecureSocketProxyService(lockdown) as dvt:
                _serve_device(dvt, pin, loc)
        except KeyboardInterrupt:
            logger.debug("get KeyboardInterrupt (legacy outer)")
        except connect.ConnectError as e:
            die(e, getattr(e, "hint", ""))
        except SystemExit:
            raise
        except Exception as e:
            die(f"DVT 服务失败 ({type(e).__name__}: {e})",
                "开发者镜像没挂上 / 手机没解锁? 解锁亮屏、重插线后重试; "
                "首次挂载需要联网下载镜像.")
        print("Bye", flush=True)


if __name__ == "__main__":
    main()
