# 张雪峰

在 iOS 14+ 真机上模拟跑步 / 定点定位：按设定配速沿路线逐点设置虚拟定位，可无限循环，
带曲线拟合、跑步摆动和配速随机起伏，看起来像真人在跑。另有 WebUI（健康检查 + 表单配置 +
路线预览 + 一键启动/停止），不用敲命令行。

> 连接方式按系统版本自动切换：**iOS 17+** 走 tun 隧道（需要 root / 管理员）；
> **iOS 14~16** 免隧道，直接连设备并自动挂载 DeveloperDiskImage，普通用户权限即可。

## 1. 准备工作

1. 电脑是 macOS 或 Windows（Windows 需安装 iTunes 官方版并打开过一次）。
2. iPhone / iPad 系统版本 ≥ 14。**iOS 16 及以上**还需打开
   `设置 → 隐私与安全性 → 开发者模式`；**iOS 15 及以下没有这个开关，无需开启**。
3. 电脑安装 Python 3.10+。
4. 同一时间只能连一台设备。
5. 设备用数据线直连电脑，解锁，弹出"信任此电脑"时点信任。

## 2. 安装（一键）

进入项目目录后，一键装依赖（建 `.venv` + `pip install`，不启动，不连设备，不要 root）：

```shell
./start-ui.sh --setup
```

```powershell
.\start-ui.ps1 -Setup   # Windows
```

依赖已就绪时会直接跳过；想强制重装用 `--reinstall` / `-Reinstall`。手动装（备用）：

```shell
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Mac 上如果手动装到 `sslpsk-pmd3` 时报错 `openssl/ssl.h file not found`，先 `brew install openssl@3`
（一键脚本已自动处理 Apple Silicon / Intel 两条路径，手动装才需 export）：

```shell
export LDFLAGS="-L/opt/homebrew/opt/openssl@3/lib"
export CPPFLAGS="-I/opt/homebrew/opt/openssl@3/include"
export PKG_CONFIG_PATH="/opt/homebrew/opt/openssl@3/lib/pkgconfig"
pip install -r requirements.txt
```

注意 `qh3==1.9.4` 是钉死的版本（`pymobiledevice3==2.46.1` 与 `qh3 2.x` 不兼容），不要升级。
`ipsw-parser` 也钉在 `1.3.9`（`>=1.4` 移除了 `ipsw_parser.img4`，会让 iOS 14~16 挂载
DeveloperDiskImage 直接报错），同样不要升级。

## 3. 启动（推荐 WebUI）

```shell
./start-ui.sh                  # 默认端口 8123，健康检查通过后自动开浏览器
./start-ui.sh --port 8124      # 换端口
./start-ui.sh --kill           # 只清理上次没退出的同类服务，然后退出
./start-ui.sh --force          # 清理同类服务后继续启动
```

Windows（以管理员身份打开 PowerShell）：

```powershell
.\start-ui.ps1 [-Port 8123] [-Kill] [-Force]
```

服务器在前台跑，停服按 `Ctrl+C`（会先清虚拟定位再退，不要直接关窗口 / kill -9）。

命令行直跑（不要 WebUI 时）：

```shell
sudo .venv/bin/python main.py                    # 跑步
sudo .venv/bin/python main.py --pin "30.30,120.08"  # 定点（WGS-84）
sudo .venv/bin/python main.py --pair-only         # 只做预检配对
```

`iOS 17+` 必须 root（建 tun 隧道必需），且用虚拟环境里的 python（sudo 下直接 `python3` 会找不到依赖）。
`iOS 14~16` 免隧道，但启动脚本默认仍会提权，照常用 `./start-ui.sh` / `sudo` 即可。

### 老系统（iOS 14~16）的开发者镜像

首次跑老系统会自动挂载 DeveloperDiskImage，顺序是：本机 Xcode 的
`DeviceSupport/<版本>` → 本地缓存 → 联网下载。下载源依次尝试 GitHub 官方源和国内可达的
加速镜像（`ghproxy.net` / `gh-proxy.com`），镜像内容由手机端签名校验，走镜像不改变信任根。
镜像缓存在 `~/.pymobiledevice3/DeveloperDiskImages/<版本>`（不可写时退到项目 `.ddi/<版本>`），
**同一版本只下一次**。

GitHub 全不通时，任选一种手动方式（三种都支持，`config.yaml` 里写不会被 WebUI 覆盖）：

| 方式 | 做法 |
| --- | --- |
| 本地镜像目录 | `config.yaml` 里加 `ddi_dir: /你放镜像的目录`（含 `DeveloperDiskImage.dmg` 和 `.dmg.signature`）；CLI 也可 `export ZXF_DDI_DIR=...` |
| 自选镜像站 | `config.yaml` 里加 `ddi_mirror: https://你的镜像/`（会拼成 `<镜像>/<仓库>/<分支>/<路径>`）；CLI 也可 `export ZXF_DDI_MIRROR=...` |
| 走代理 | `export HTTPS_PROXY=http://127.0.0.1:7890`（注意 WebUI 是 `sudo` 起的，需 `sudo -E` 或直接改 config.yaml） |

## 4. 配置（`config.yaml`）

| 参数 | 含义 | 默认值 |
| --- | --- | --- |
| `pace` | 目标配速（分/公里），如 `"4:25"` | `"4:25"` |
| `routeConfig` | 路线文件名 | `"zjg.json"` |
| `dt` | 发 GPS 定位的间隔（秒/点） | `0.01` |
| `fit_curve` | 是否拟合曲线（点稀疏、折线生硬时变平滑） | `true` |
| `fit_samples` | 拟合密度，原始每段插值点数 | `8` |
| `wander_m` | 跑步左右摆动幅度（米），`0` 关闭 | `1.0` |
| `wander_step_m` | 跑步前后/起伏抖动幅度（米），`0` 关闭 | `0.25` |
| `wander_step_hz` | 步频（Hz），真人慢跑约 1.6~2.0 | `1.8` |
| `pace_var_s` | 圈内配速起伏半幅（秒/公里），`0` 关闭 | `10` |
| `lap_jitter_s` | 每圈平均配速起伏（秒/公里） | `15` |

`pace` 必填，删掉它或留空会直接报错，不再回退到其他参数。

## 5. 路线文件

路线是 `.json` 文件（默认 `zjg.json`），内容是一串百度坐标（BD-09）点，程序会自动转成
手机用的 WGS-84 坐标。格式：

```
{"lng":"120.08821","lat":"30.31166"},{"lng":"120.08817","lat":"30.31165"},...
```

去百度地图取点，把点依次粘进来即可，点顺序就是跑步顺序，首尾自动闭环。
点的疏密无所谓（程序按配速重采样），折线生硬也没关系（开了 `fit_curve` 会拟合平滑）。
页面上可切换路线、新建路线、一键均匀化（点距不均匀会导致跑起来像"飞"）。

## 6. 结束与端口占用

- 必须按 `Ctrl+C` 或点页面**停止**退出，程序会清掉虚拟定位；直接关窗口、kill -9 都不会恢复。
- 如果定位没恢复，重启手机即可。
- 端口被占时先看是不是上次没退出的同类服务，是就 `./start-ui.sh --kill` 清理，
  不是就换端口（不要 kill 别人的服务）。

## 7. 常见问题

- 一直提示没设备：确认线是数据线、设备已解锁并点了信任，一次只连一台。
- 提示没开开发者模式：仅 iOS 16 及以上有此要求，去手机设置里打开，重启手机后再跑；
  iOS 15 及以下没有这个设置，程序会自动跳过。
- iOS 14~16 报"挂载开发者镜像失败/下载失败"：先保持联网重试（官方源不通会自动走国内镜像）；
  还不行就按第 3 节"老系统（iOS 14~16）的开发者镜像"手动放镜像或指定镜像站。
- 跑起来像"飞"：路线里混进了很远的点，删掉它，或用一键均匀化。
- macOS 反复要密码：`start-ui.sh` 只在启动时验证一次，后续不再打扰。

## 8. 项目结构

```
main.py            命令行入口（预检 → 路线 → 连接[17+: 隧道 / 14~16: 免隧道+挂镜像] → 跑步/定点）
run.py             跑步逻辑（配速 / 拟合 / 摆动 / BD-09→WGS-84）
webui.py           WebUI 后端（标准库 http.server，只绑定 127.0.0.1）
templates/         页面模板    static/  前端资源（Bootstrap/Leaflet 已离线打包）
driver/            设备定位 / 连接 / QUIC 兼容补丁
init/              预检 / 路线读取 / 隧道子进程
util/              端口占用检测 / 机型映射 / 路线解析
config.yaml        仿真参数    zjg.json  默认路线
start-ui.sh / start-ui.ps1  一键启动脚本
```

## 9. 致谢

本项目基于原开源项目二次开发，感谢原作者：

https://github.com/iOSRealRun/iOSRealRun-cli-17（MPL-2.0）
