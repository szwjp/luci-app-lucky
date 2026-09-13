
## 怎么确定当前系统CPU架构下载相应lucky核心包
复制以下指令到终端执行,根据显示下载文件名含有架构字符串的ipk包
```
cd /tmp ;if [ -f /usr/bin/curl ];then curl -sSO http://release.66666.host/luckyarch.sh;else wget -O http://release.66666.host/luckyarch.sh;fi;sh luckyarch.sh 
```

## 轻量打包（无需 OpenWrt 源码树 / SDK）

OpenWrt 25.12 起包管理器换成 apk，插件需要同时提供 `.apk`（apk-tools v3）和 `.ipk`（opkg）。
`scripts/build-pkg.sh` 直接铺好文件树后调用 `apk mkpkg` / `ipkg-build`，一次产出两种格式：

```sh
# 依赖：apk-tools >= 3.0 的 apk（含 mkpkg）、ipkg-build、fakeroot、curl/wget
scripts/build-pkg.sh --arch arm64 --pkg both --out dist
# 只出 apk：  --pkg apk
# 指定架构：  --arch mipsle_softfloat --prefix mipsel-
```

产物（`dist/`）：

- `lucky-2.27.2-r1.apk` / `lucky_2.27.2_1_aarch64_generic.ipk`（核心包，架构相关）
- `luci-app-lucky-2.27.2-r1.apk` / `luci-app-lucky_2.27.2_1_all.ipk`（壳包，`noarch`）
- `luci-i18n-lucky-zh-cn-2.27.2-r1.apk` / `.ipk`（翻译包，每个 `po/<lang>/lucky.po` 一个）

翻译目录由 `scripts/po2lmo.py` 编译成 `.lmo`（OpenWrt `po2lmo` 的无依赖 Python 实现，
不需要 C 工具链或 Lua 头文件），安装到 `/usr/lib/lua/luci/i18n/lucky.<locale>.lmo`。
`po/zh_Hans` 自动映射为 LuCI 的 `zh-cn`。

校验产物结构（解析 apk v3 的 ADB 容器，不需要安装、不依赖设备）：

```sh
scripts/apk-verify.py --arch noarch --require-file /usr/bin/lucky dist/lucky-*.apk
scripts/apk-verify.py --arch noarch \
  --require-file /usr/lib/lua/luci/i18n/lucky.zh-cn.lmo dist/luci-i18n-lucky-*.apk
```

CI 见 `.github/workflows/build.yml`（多架构构建 + 校验 + 发布）。`Run workflow` 无需任何输入，
直接点即全量构建并发布：

- **每个架构一个独立 Release**，Release 列表里显示的就是架构名（`x86_64`、`arm64`、
  `mipsle_softfloat` …），tag 为 `<arch>-<version>`
- 每次运行会重建各架构的 release，所以列表里每个架构始终保持一条最新
- 发布版本自动取 `lucky/Makefile` 的 `PKG_VERSION-rPKG_RELEASE`；推 `v*` tag 时以 tag 名为准
- 版本对不上 `lucky/Makefile` 时直接失败，不会发错版本
- 只想构建部分架构时改 `BUILD_ARCHS`；想强制指定版本时改 `RELEASE_VERSION`

设备侧安装（文件名带架构前缀，按需替换 `arm64`）：

```sh
apk add --allow-untrusted ./arm64-lucky-2.27.2-r1.apk ./arm64-luci-app-lucky-2.27.2-r1.apk ./arm64-luci-i18n-lucky-zh-cn-2.27.2-r1.apk
```

## 截图
![](./previews/001.png)
![](./previews/002.png)
