# DeltaMusicLauncher 源码与构建说明

当前推荐的公开发布方式是 Windows EXE：用户从 GitHub Releases 下载 ZIP、完整解压并双击 `DeltaMusic.exe`，无需安装 Python。

## 维护者快速入口

在 DeltaMusic 根目录中保留四个内置曲目目录（`croatian`、`mariage`、`daoxiang`、`autumn`），然后进入本目录：

```powershell
cd .\coding\DeltaMusicLauncher
python -m unittest discover -s tests -v
.\build_exe_release.ps1 -Force
```

构建结果包含 `DeltaMusicLauncher.zip` 与 `SHA256SUMS.txt`。发布前应核对校验值，并在普通 Windows 环境中测试。

旧的 Python 启动流程仍由 `bootstrap.ps1` 保留给开发调试；它不是面向粉丝的推荐安装路径。

完整说明见仓库根目录的 `README.md`、`CHANGELOG.md` 和 `CONTRIBUTING.md`。
