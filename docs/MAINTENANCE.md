# 维护和发布

维护者：Yining Zi（GitHub：YiningZi）。此仓库包含源码、开发记录、自动化检查与发布入口；后续功能修改仍需明确需求、代码审核与验证。

## 版本约定

- `v1.10.0` 是最早实际保留的源码和安装包快照。
- `v1.11.1` 是此次导入的当前版本。
- 缺失的历史版本不补造标签、提交或安装包。
- Git 提交日期反映实际导入/修改时间，原开发日期记录在版本说明内。
- 已发布标签及附件不覆盖，修复另发新版本。

## 日常修改

1. 创建修改分支，说明复现条件与预期行为。
2. 修改 `bflm_loop_measure.py` 或配套文件，将变化记入 CHANGELOG 的“未发布”。
3. 构建并执行回归。GitHub Actions 对 `main` 提交及 PR 自动运行检查。
4. 审核并合并。公开的问题使用 Issues，修改使用 PR。

## 发布一个新版本

1. 同步修改 `bl_info["version"]`、`ZLM_VERSION` 和 `blender_manifest.toml` 的版本号。
2. 将未发布说明移到新版本标题，新增 `docs/releases/vX.Y.Z.md`。更新 README 当前版本与兼容范围。
3. 运行以下检查和构建，复核 Release 说明与安装包：

```sh
python tools/check_project.py
python tools/build.py
python tools/run_blender_tests.py --blender /path/to/blender
blender --factory-startup --command extension validate dist/ZLM-X.Y.Z.zip
```

4. 提交并推送，通过 CI 后给该提交打标签：

```sh
git tag -a vX.Y.Z -m "ZLM X.Y.Z"
git push origin vX.Y.Z
```

5. `Publish release` 工作流核对标签与源码版本，再执行 Blender 回归和清单校验；通过后创建 Release，上传 ZIP 和 SHA-256 摘要。测试作业只有读取权限，发布作业才有写入权限。

工作流重试不会覆盖已存在的 Release。手动触发时，必须在 Actions 界面选择同一个版本标签作为运行 ref，并把 `tag` 参数设为该标签；避免用其他提交的测试结果发布。首次归档的 1.10.0 没有现代构建工作流，直接上传留存原包并注明历史用途。

## 自动检查范围

CI 当前固定 Blender **4.4.3 / Linux**，校验官方发行包的固定 SHA-256 摘要，再运行五组既有回归。Blender 5.2 已通过作者测试（2026-10-12 确认），其测试记录与 CI 覆盖范围分别标注。界面交互、不同显卡、其他 Blender 版本仍需按修改范围人工复核。没有执行的版本不要写为“已验证”。

## 完整性与许可

构建会规范文本换行，包含 LICENSE，并生成 `integrity.json`。校验仅检测文件变化；它不是数字签名。合法修改后重新构建即可生成对应摘要。

公开仓库不包含教程制作工程、原始手掌模型或私有接口配置。README 使用专用插件展示图，以重新渲染的模型和测量环呈现主要功能。
