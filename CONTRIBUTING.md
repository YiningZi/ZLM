# 修改与贡献

主要源码为 `bflm_loop_measure.py`。使用 Python 3.11+ 构建，运行几何测试需要 Blender 4.4+，不需要安装第三方 Python 包。

## 开发流程

```sh
git clone https://github.com/YiningZi/ZLM.git
cd ZLM
git switch -c fix/describe-the-change
python tools/check_project.py
python tools/build.py
python tools/run_blender_tests.py --blender /path/to/blender
```

Windows 可运行 `./build_extension.ps1 -Python "你的 Python 可执行文件"`；测试入口的 `--blender` 参数也接受 Windows 绝对路径。

修改主源码，**不要编辑生成的 `build/zlm/__init__.py`**。构建会输出 `dist/ZLM-版本号.zip` 和 `dist/SHA256SUMS.txt`。

## 提交要求

- 说明问题、触发步骤及修改后的行为。
- 影响算法时，加入能复现问题的几何回归，检查坐标和网格连接关系。
- UI 小调整按风险验证，不必增加只重复实现的测试。
- 新的未发布变更写到 `CHANGELOG.md` 的“未发布”中。
- 提交中不包含 Blender 安装目录、真实用户模型、密钥、构建产物或私人文件路径。
- 默认从分支发起 PR；维护者在检查通过后合并。

提交贡献即表示允许按本项目 GPL-3.0-or-later 许可证分发相应贡献。保留已有作者及版权信息。
