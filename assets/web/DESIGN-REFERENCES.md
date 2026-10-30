# Daynest · 当前设计参考

用户提供的 **Ui — clinical blueprint on frosted paper**，原文见 `design/ui-style-reference.md`。本次将该规格应用到Daynest 工作台，继续使用 Bulma 和原生 JavaScript。

- 黑白灰三层表面，细灰边界，黑底白字主按钮，浅灰次按钮；红色仅用于错误和取消/删除。
- Geist 本地字体、400/500/600 字重，正文 14px；标题和真实概况数字通过字号与字距建立层级。计数全部读取现有数据。
- 18px 控件与 24px 面板圆角、20px 面板内边距、轻投影。概况统计和内容列表保留排版结构，月历内部保持连续网格。
- 桌面保留紧凑侧栏及日程主区，移动端导航两行；助手保持按需打开的模态抽屉。资料库、重复日程、自动化和 API 配置一并覆盖。
- “frosted paper”作为中性表面的视觉描述处理，不加入背景模糊、玻璃、渐变、无意义动效或虚构内容。Geist 按 SIL OFL 本地打包，运行时无字体 CDN 请求。

字体来源与校验见 `vendor/geist-provenance.json`，许可见 `vendor/GEIST-LICENSE`。样式入口与维护规则见 `STYLING.md`。
