# LANPush

局域网一对多文件分发工具：一台主控电脑，一次操作，把文件或整个文件夹自动推到多台电脑的指定目录，无人值守、断点续传。

界面是浏览器网页，被控端后台静默运行。被控端只主动连接主控端，**不需要开放防火墙、不需要配 IP**，局域网内自动发现。

## 特性

- **一对多群发**：勾选多台设备一次下发，每台独立进度条
- **零配置被控端**：双向自动发现（收主控广播 + 主动询问），不依赖主控自报 IP，多网卡/VPN 环境也稳；托盘可一键重新发现
- **系统托盘**：两端常驻任务栏托盘，右键打开控制台 / 重新发现 / 查看日志 / 退出，双击快捷操作；被控端防重复启动，双开不会出现两个托盘
- **分发进度 / 历史分离**：进度只显示本次、关闭主控自动清空；历史按电脑名分组长期保存每台收过什么文件
- **断点续传**：1MB 分块 + HTTP Range，断网 / 重启后从断点继续
- **文件夹整包分发**：保留子目录结构，空目录忽略
- **落盘可靠**：临时文件 + mtime 校验，传完校验大小，不会留下半个坏文件
- **三种同名策略**：覆盖 / 跳过 / 自动加序号，目标目录不存在自动创建
- **纯 Python 标准库**：主控端、被控端均无第三方依赖

## 快速开始

详见 [使用说明.md](使用说明.md)。简要流程：

1. 把 `bin/LANPush-Host.exe` 放到主控电脑，双击运行（首次弹防火墙时选「专用网络」并允许）。
2. 打开页面顶部显示的地址（默认 `http://127.0.0.1:8080`），点「打开文件夹」，把要发的文件或文件夹放进弹出的 `待分发` 目录。
3. 把 `bin/` 里的 `LANPush-Agent.exe` 和 `安装_开机自动运行.bat` 拷到每台被控电脑（建议 `C:\LANPush` 这样的无空格路径），双击安装脚本。
4. 设备自动出现在网页列表 → 勾文件、勾设备 → 点「开始分发」。

## 目录结构

```
bin/                     免安装 exe 与被控端安装脚本（直接用这个）
  LANPush-Host.exe       主控端，双击运行
  LANPush-Agent.exe      被控端，后台静默运行
  安装_开机自动运行.bat
  卸载_取消自动运行.bat
  查看日志.bat
host_main.py             主控端源码（Python 3）
agent_main.py            被控端源码（Python 3）
tray.py                  系统托盘模块（纯 ctypes，无第三方依赖）
web/index.html           网页控制台源码
使用说明.md              完整部署与常见问题
```

## 从源码运行 / 自己打包

需要 Python 3，无需 pip 安装任何依赖。

```bat
:: 主控端
python host_main.py

:: 被控端
python agent_main.py --server http://主控端IP:8080 --dir D:\LAN接收

:: 打包成 exe（需 pip install pyinstaller）
pyinstaller --onefile --noconsole --name LANPush-Host --add-data "web;web" --hidden-import tray host_main.py
pyinstaller --onefile --noconsole --name LANPush-Agent --hidden-import tray agent_main.py
```

打包时若 `--add-data` 用相对路径报错，把 `web` 换成其绝对路径即可。

## 端口与配置

- 主控端默认 8080，被占用自动顺延；网页顶部显示实际地址。
- 主控端 `data/host_config.json` 可改 `port`、仓库路径；被控端 `data/agent_config.json` 可手工指定主控端 `server`、`saveDir`。
- 跨网段无法自动发现时，在被控端配置里手工填主控端地址。

## 安全

数据全程只在局域网内，不连外网。控制台**没有账号密码**，任何能访问 8080 端口的人都能下发文件，请只部署在可信内网；多用户环境建议用防火墙把 8080 限制为指定的几台 IP。

exe 未做数字签名，首次运行 Windows SmartScreen 可能提示，选「更多信息 → 仍要运行」。

## 许可

见 [LICENSE](LICENSE)。
