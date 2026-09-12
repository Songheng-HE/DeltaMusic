# DeltaMusic 启动器：开发用说明

对普通 Windows 用户，推荐直接下载 GitHub Releases 中的 `DeltaMusic-v1.2.0-windows-x64.zip`，完整解压后双击 `DeltaMusic.exe`；不需要安装 Python，也不要单独运行 `DeltaMusicPlayerHost.exe`。

本文件只保留给维护者：`bootstrap.ps1` 是旧的 Python 启动与依赖检查流程，可用于源码调试，但不是当前推荐的粉丝发布方式。当前 Windows 发布包由 `build_exe_release.ps1` 构建。

完整用户说明、版本记录和贡献规范位于仓库根目录：

- `../../README.md`
- `../../CHANGELOG.md`
- `../../CONTRIBUTING.md`
