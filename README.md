# DeltaMusic

DeltaMusic 是一个用于《三角洲行动》游戏内口琴演奏的 Windows 曲库启动器。下载发布包后不需要安装 Python：选择曲目、同意播放时出现的 Windows 管理员权限请求，然后切回游戏即可。

当前推荐版本：**v1.2.0（曲库管理 + 安全多 MIDI 导入）**。

## 下载哪个版本

只维护**一个** GitHub 仓库，下载版使用 GitHub Releases 管理：

- **v1.2.0**：最新版，默认推荐。支持个人曲目重命名、删除、一次选择或拖入多个 MIDI，并会自动生成安全的曲目 ID。
- **v1.1.0**：历史 F10 修复版。保留给需要回退测试的用户；不含新版曲库管理功能。

请优先从仓库右侧的 **Releases** 下载最新 `DeltaMusic-v1.2.0-windows-x64.zip`，完整解压后双击 `DeltaMusic.exe`。不要单独运行 `DeltaMusicPlayerHost.exe`，也不要在压缩包预览窗口里直接运行。

## 第一次使用

1. 从 GitHub Releases 下载最新版 ZIP，并完整解压到普通文件夹，例如桌面或下载目录。
2. 双击 `DeltaMusic.exe`。
3. 在曲库中选歌，点击“开始演奏”。只有真正播放时才会请求管理员权限。
4. 切回游戏、拿出口琴，等待倒计时结束。

内置 Windows EXE 运行环境，**不需要安装 Python**。

## F10 紧急停止

对于内置曲目和“安全 MIDI”导入的默认播放器，单按 `F10` 可在倒计时、休止、长音和正常演奏时请求立即停止；也可回到启动器点击“紧急停止”。

- 笔记本若将 F10 设为功能键，请使用 `Fn + F10`。
- “导入 Python + MIDI（高级）”的第三方脚本是独立代码，是否支持 F10 由其作者决定，启动器不能强制终止未知脚本。

## 管理自己的曲库

个人曲目保存在：

```text
%LOCALAPPDATA%\DeltaMusic\user_library\
```

界面提供三种导入方式：

- **选择 MIDI（可多选）**：一次安全导入一个或多个 `.mid` / `.midi` 文件。
- **拖入 MIDI**：把一个或多个 MIDI 文件拖到界面下方区域，效果与上项相同。若电脑的拖放组件不可用，界面会自动提示改用“选择 MIDI（可多选）”。
- **导入 Python + MIDI（高级）**：只适用于你自己写的或完全信任的脚本；不要把陌生 Python 代码当作安全曲目导入或以管理员权限运行。

安全导入会自动创建曲目资料：英文文件名使用规范化英文 ID；中文文件名自动转为拼音 ID；若 ID 已存在，会依次追加 `1`、`2`、`3`。右键个人曲目可改显示名称或永久删除；四首内置曲目不能被修改或删除。

## 当前内置曲目

- Croatian Rhapsody / 克罗地亚狂想曲
- Mariage d'Amour / 梦中的婚礼
- 稻香
- Vivaldi - Autumn / 维瓦尔第《秋》方案 B

## 源码目录

```text
DeltaMusic/
├─ README.md
├─ CHANGELOG.md
├─ CONTRIBUTING.md
├─ croatian/                 # 发布内置曲目
├─ mariage/
├─ daoxiang/
├─ autumn/
└─ coding/DeltaMusicLauncher/
   ├─ launcher/              # 启动器、默认 MIDI 播放器与导入逻辑
   ├─ tests/                 # 自动化回归测试
   ├─ build_exe_release.ps1  # 构建 Windows EXE 发布包
   └─ README_EXE.md          # 发布 ZIP 内的使用说明
```

`findMusic/`、个人曲库、构建缓存和历史发布 ZIP 都是本地工作资料，不进入公开源码仓库。

## 开发与构建

运行测试：

```powershell
cd .\coding\DeltaMusicLauncher
python -m unittest discover -s tests -v
```

构建 EXE 发布包（需要 Windows、Python 3.10+）：

```powershell
cd .\coding\DeltaMusicLauncher
.\build_exe_release.ps1 -Force
```

构建结束后会生成 ZIP 和 `SHA256SUMS.txt`。发布前应验证 ZIP 校验值，并用一台普通用户环境的 Windows 电脑实测。

## 贡献曲目

请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。最容易、安全地分享新曲目的方式是提供单旋律 MIDI，让用户通过“选择 MIDI（可多选）”导入；不要要求用户运行陌生 Python 脚本。

## 安全、权限与版权

- 播放时申请管理员权限，是为了向同等权限运行的游戏窗口发送按键；这不绕过游戏规则、反作弊或安全机制。
- 当前 EXE 尚未进行公开代码签名。学校、单位或受应用控制策略管理的电脑可能拦截未签名程序；不要要求用户关闭安全策略，应使用签名版或联系设备管理员。
- 本仓库的代码采用 [MIT License](LICENSE)。MIDI、歌曲名称、编曲和原作权利不因代码许可证而自动开放；提交或重新分发 MIDI 前，请确认拥有相应授权。

完整版本记录见 [CHANGELOG.md](CHANGELOG.md)。
