# 来源与修改说明

本项目基于 [Herdr app v0.1.15](https://github.com/herdrdev/herdr-app/tree/v0.1.15) 随附的默认布局和模块，由 ssdiwu 整理为中文定制包。

- `layouts/zh.json`：克隆默认主布局，保留结构，引用中文模块。
- `mods/*-zh/main.js`：基于对应上游模块修改展示文案、引入中文词典，并调整显式打开模块时的引用。
- `mods/*-zh/mod.json`：保留并记录克隆来源与版本。
- `translations/`：中文词典和按页面组织的源码文案替换规则。
- `manage.py`、`test_manage.py`、维护说明：用于本包管理与验证的新增内容。
- `mods/*-zh/i18n.js`：由本包唯一词典生成的副本。

代码采用 Apache License 2.0，完整文本见 [LICENSE](LICENSE)。上游代码注释保留；本项目不代表 Herdr 官方，也不提供商标许可。

模块内的 SVG 图标来源为 Lucide，部分衍生自 Feather，分别遵循 ISC / MIT 许可；完整版权与许可文本见 [licenses/LICENSE-Lucide.txt](licenses/LICENSE-Lucide.txt)，该文件取自上游 v0.1.15。运行时引用但未随本包分发的应用图标、字体等资产仍由 Herdr 安装提供。
