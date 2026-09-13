
## 怎么确定当前系统CPU架构下载相应lucky核心包
复制以下指令到终端执行,根据显示下载文件名含有架构字符串的ipk包
```
cd /tmp ;if [ -f /usr/bin/curl ];then curl -sSO http://release.66666.host/luckyarch.sh;else wget -O http://release.66666.host/luckyarch.sh;fi;sh luckyarch.sh 
```


## 1.X升级2.X版本注意

第一种方法：先通过lucky后台上传tar.gz方式升级lucky
再安装

- luci-app-lucky 
- luci-i18n-lucky-zh-cn 

两个ipk包

第二种方法：

lucky后台备份配置下载保存后，将lucky相关IPK卸载干净
```
opkg remove lucky
opkg remove luci-i18n-lucky-zh-cn
opkg remove luci-app-lucky
```

再安装 
- lucky 
- luci-app-lucky 
- luci-i18n-lucky-zh-cn 

三个ipk包



本分支本人自用,仅供参考.
配置文件架构和https://github.com/sirpdboy/luci-app-lucky 版本可能存在冲突,

替换版本前请使用前备份下载lucky配置

然后执行执行
```
opkg remove lucky
opkg remove luci-i18n-lucky-zh-cn
opkg remove luci-app-lucky
```
卸载删除干净之前文件.




最新版本编译好的IPK包请在
https://url21.ctfile.com/d/44547821-55537427-a5525e?p=16601
下载





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
直接点即全量构建并发布 Release：

- 发布版本自动取 `lucky/Makefile` 的 `PKG_VERSION-rPKG_RELEASE`；推 `v*` tag 时以 tag 名为准
- 版本对不上 `lucky/Makefile` 时直接失败，不会发错版本
- Release 里每个架构一组，列出该架构的 apk/ipk 下载链接
- 只想构建部分架构时改 `BUILD_ARCHS`；想强制指定版本时改 `RELEASE_VERSION`

设备侧安装：

```sh
apk add --allow-untrusted ./lucky-2.27.2-r1.apk ./luci-app-lucky-2.27.2-r1.apk ./luci-i18n-lucky-zh-cn-2.27.2-r1.apk
```


## 使用方法
   
- 将luci-app-lucky添加至 LEDE/OpenWRT 源码的方法。



### 下载源码：

 ```Brach 
 
    进入lede/openwrt项目根目录下
    # 下载源码
	
    git clone  https://github.com/gdy666/luci-app-lucky.git package/lucky
	
 ``` 
### 配置菜单

 ```Brach
    make menuconfig
	# 找到 LuCI -> Applications, 选择 luci-app-lucky, 保存后退出。
 ``` 
 
### 编译

 ```Brach 
    # 编译lucky IPK包
    make package/lucky/lucky/compile V=s
    # 编译luci-app-lucky IPK包
    make package/lucky/luci-app-lucky/compile V=s
    
 ```


## 截图
![](./previews/001.png)
![](./previews/002.png)