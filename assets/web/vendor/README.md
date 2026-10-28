# Bulma 1.0.4

本地保存官方 npm 发行版的编译 CSS，运行时不访问 CDN，也不需要 npm 或 Sass。框架文件保持原样，品牌覆盖放在上一级 `brand.css`。

- 官方说明：https://bulma.io/documentation/start/installation/
- CSS 变量：https://bulma.io/documentation/features/css-variables/
- 发行包：https://registry.npmjs.org/bulma/-/bulma-1.0.4.tgz
- 许可证：MIT，完整文本见 `BULMA-LICENSE`。
- CSS SHA-256：`67fa26df1ca9e95d8f2adc7c04fa1b15fa3d24257470ebc10cc68b9aab914bee`。
- 下载时已校验 npm 元数据中的 SHA-512 integrity；校验信息见 `bulma-provenance.json`。

升级时同时更新文件名、HTML 引用、服务端静态文件白名单和校验记录，再验证页面及交互。

# Geist

本地提供 Vercel 官方 Geist 可变字体（100–900），界面仅使用 400/500/600。文件 `Geist-variable.woff2`，约 70 KB，来自 `vercel/geist-font` 的 `fonts/Geist/webfonts/Geist[wght].woff2`。

下载后校验官方 Git blob 与 SHA-256，记录见 `geist-provenance.json`；完整 SIL Open Font License 见 `GEIST-LICENSE`。字体通过本机静态文件白名单提供，页面不访问外部字体服务；中文由系统字体补充。
