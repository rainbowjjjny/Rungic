# 上游组件改为补丁队列：迁移方案与KWin试点

2026-09-26。用户要求按行业最佳实践管理我们对上游组件的修改，不受现有“直接Vendor”约定限制，并先写方案、用KWin试点。本文是方案；试点结果写在文末。调研报告：`.work/research/patch-queue/tooling-report.md`（在`ubuntu:26.04`容器中实测git-buildpackage 0.9.42、autopkgtest 5.55、lintian 2.129、git-ubuntu 1.1，并克隆Kubuntu、Debian Qt/KDE、KDE neon的kwin打包仓库核对）。

## 为什么改

现状：`vendor/`保存15个上游组件的完整源码（789 MB，其中Mesa 414 MB、FFmpeg 112 MB），我们的修改直接写在源码里。问题：

- 看不出“我们改了什么”：修改和上游代码混在同一棵树，只能靠提交历史拼凑；一个提交常含几项功能（例如KWin的`e07d7448`含投屏光标、宿主文本输入、原始宿主输出）。
- 升级上游没有机械流程：新版本要重新导入，再手工重放修改。
- 修改与测试没有对应关系：升级后不知道哪些功能要测；vendor修改目前没有任何自动测试（Ubuntu对kwin、kscreen、plasma-mobile等都以`BUILD_TESTING=OFF`构建，也没有`debian/tests`）。

## 业界做法

| 领域 | 做法 |
|---|---|
| Debian/Ubuntu及衍生版 | 仓库只放`debian/`；上游以原始tarball固定；修改是`debian/patches`中的DEP-3补丁；编辑时由`gbp pq`把补丁展开为提交。Kubuntu（`~kubuntu-packagers/kubuntu-packaging/+git/kwin`）、Debian Qt/KDE团队（`salsa…/qt-kde-team/kde/kwin`）、KDE neon（`invent.kde.org/neon/kde/kwin`）都如此。 |
| Yocto/OpenEmbedded | 配方固定`SRC_URI`与校验和；每个补丁必须带`Upstream-Status`（Pending、Submitted、Backport、Denied、Inactive-Upstream、Inappropriate＋原因）。 |
| postmarketOS、Alpine | 一个仓库放全部配方，源码按版本与sha512下载；补丁为`git format-patch`序列；上游优先。 |
| Android、ChromeOS | 用manifest管理完整fork，每个提交按`UPSTREAM:`/`BACKPORT:`/`CHROMIUM:`分类；适用于大量深度修改的项目。 |

我们是Ubuntu衍生发行版加少量非Debian上游，最接近前两行：**上游只固定不入库、修改为带元数据的补丁队列、仓库只放配方与补丁、测试分层**。

## 目录与文件

```
packages/<源码包名>/
  recipe.json          上游固定信息（见下）
  debian/              完整打包目录：Ubuntu原有内容 + 我们的补丁、changelog、测试
    patches/series     Ubuntu原有补丁在前，我们的补丁在后
    patches/rungic/    我们的补丁（每条一项功能）
    tests/control      autopkgtest
```

- **Ubuntu包**（kwin、kscreen、plasma-mobile、plasma-settings、plasma-keyboard、xdg-desktop-portal-kde、plasma-camera、libcamera、qtmultimedia、wl-clipboard）：`recipe.json`记录源码包名与版本、`.dsc`/orig/`debian.tar` 的sha256、git-ubuntu的`import/`标签、snapshot.ubuntu.com时间点。取源码优先用`apt-get source --snapshot`（archive签名链覆盖到Sources哈希），退回`pull-lp-source`＋sha256核对（它只用Debian密钥环验签，Ubuntu上传者只给警告）。
- **非Debian上游**（Mesa分支、FFmpeg、Snapshot、typesafe-computer-use、arc-cua、LiteRT）：`recipe.json`记录git地址与提交或tarball与sha256；补丁为`git format-patch`序列，元数据同下；打包目录同样放`debian/`（我们本来就产出deb）。
- **我们自己的代码**（桥接服务、APK、语音Agent、诊断工具等）不是上游修改，仍是普通源码，补单元测试。
- **跨包共享的代码**（`android-display-client.h`被KWin与KScreen共用，目前靠符号链接；`androidgraphicsbuffer.h`、`codec-client`、`gpu-allocator-client`）改为从我们的源码构建一个`-dev`包，使用它的上游补丁只写`Build-Depends`与`#include`。补丁无法表达符号链接，同一代码也不应在多个补丁中各有一份。
- 源码缓存放`.work/sources/`，按sha256校验后使用。

## 补丁规范

每条补丁一项功能，`git format-patch`格式（DEP-3明确接受），头部字段：

| 字段 | 含义 |
|---|---|
| `Subject`/`Description` | 做什么、为什么（DEP-3必填） |
| `Author` | 作者（有它可省`Origin`） |
| `Origin` | `vendor`、`upstream, commit:<id>`、`backport, <url>` |
| `Forwarded` | 上游MR/问题链接；`not-needed`须说明原因；`no`表示尚未提交 |
| `Applied-Upstream` | 上游已合入的版本或提交，下次升级时删除该补丁 |
| `Last-Update` | YYYY-MM-DD |
| `X-Rungic-Status` | 取Yocto的取值：`Pending`、`Submitted [链接]`、`Backport [版本]`、`Inappropriate [原因，例如Android宿主专用]` |
| `X-Rungic-Tests` | 覆盖它的测试ID（见测试分层），没有时写`none`并说明，作为待补清单 |
| `X-Rungic-Docs` | 设计与实测文档，例如`docs/58` |

`gbp pq`的`Gbp-Pq: Topic rungic`使补丁导出到`debian/patches/rungic/`。lintian 2.129对`X-`字段不报错；检查脚本要求每条补丁都有上述字段。

## 工具与工作流

新增`tools/pq.py`（替代`tools/stage_vendor.py`与`tools/vendor_debian.py`的相应部分）：

| 命令 | 作用 |
|---|---|
| `fetch PKG` | 按`recipe.json`下载并校验上游到`.work/sources/` |
| `prepare PKG [--version V]` | 在`.work/pq/PKG`建git树：上游源码一个提交、叠加`packages/PKG/debian`一个提交，再`gbp pq import`，每条补丁一个提交 |
| `export PKG` | `gbp pq export`，把`debian/patches`与`series`写回`packages/PKG/debian` |
| `rebase PKG --to V` | 在新上游版本上重放补丁队列，列出冲突补丁 |
| `lint [PKG]` | DEP-3与`X-Rungic-*`字段、`series`一致性、lintian |
| `verify PKG` | 按配方从零准备源码树，与参考树比较（迁移期与`vendor/`比较） |
| `tests PKG` | 列出补丁与测试的对应矩阵；升级时列出冲突或被改写的补丁所需的测试 |

构建：`tools/build_on_device.py`改为把配方与缓存的源码推到手机，`dpkg-source -x`后`dpkg-buildpackage`（`3.0 (quilt)`在解包时应用补丁）；保留现有的完整构建与增量构建两种方式。

**编辑一个上游组件**：`pq.py prepare kwin` → 在`.work/pq/kwin`像普通代码一样修改并提交（新功能一个新提交，修正某功能就修改对应提交） → `pq.py export kwin` → `pq.py lint` → 构建、测试、提交`packages/kwin`。

**升级上游**：`pq.py rebase kwin --to 4:6.6.7-0ubuntu1` → 解决冲突（每个冲突对应一条补丁及其功能） → 删除`Applied-Upstream`的补丁 → `pq.py tests kwin --changed`列出必须运行的测试 → 构建、分层测试、发布验收。

## 测试分层

| 层 | 内容 | 何时运行 |
|---|---|---|
| L0 补丁检查 | 字段、series、lintian；`pq.py verify` | 每次提交 |
| L1 构建时测试 | 上游自带的相关测试（KWin：`xdgshellwindow`、`idle_inhibition`、`inputmethod`、`test_virtualkeyboard_dbus`、`screencasting`、`test_ftrace`等集成测试），加我们为补丁写的新测试（能提交上游的写成上游风格，随补丁一起提交） | 单独的测试构建（`BUILD_TESTING=ON`），补丁改动或升级时 |
| L2 autopkgtest | `debian/tests`：在装好包的系统中检查我们的功能（D-Bus接口、环境变量开关、服务行为）；经`autopkgtest-virt-null`或`virt-ssh`在手机容器中运行，GUI类标`needs-root`、`allow-stderr`、`skippable` | 每次发布候选 |
| L3 系统验收 | 现有`tools/rungic_acceptance.py`场景（相当于openQA层），每个场景标明覆盖的补丁；补上缺失场景（投屏、按住Home唤起语音助手、空闲抑制、配色联动等） | 每次部署（冒烟）与发布候选（完整） |

每条补丁的`X-Rungic-Tests`至少指向一层；指不到的列为待补项。

## KWin试点

| 步骤 | 内容 | 通过标准 |
|---|---|---|
| 1 配方 | `packages/kwin/recipe.json`固定`4:6.6.6-0ubuntu0.1`（`.dsc` sha256 `4fda1d85…`、orig `76314bb5…`、`debian.tar` `f8e51bab…`、git-ubuntu `import/4%6.6.6-0ubuntu0.1`） | `fetch`校验通过 |
| 2 补丁队列 | 按历史重建：`d1301f2e`中的5个历史补丁（android、idle、screencast、screencast-shm、display-settings，已实测能在基线上干净应用）加其后9个提交，`e07d7448`拆为3条；共享头文件改由`-dev`包提供；每条写全字段 | `verify`：准备出的树与现有`vendor/kwin`除`.pc`与符号链接外逐字节一致 |
| 3 工具 | `tools/pq.py`及其单元测试 | 单元测试通过；`prepare`→`export`往返不改变补丁内容 |
| 4 构建与部署 | 从配方在手机上构建，与现有`+moto19`的包比较文件列表；发布并完整验收 | 完整验收15项通过 |
| 5 测试 | 测试构建运行L1所列上游测试；为“最小尺寸大于最大尺寸”补丁写上游风格的集成测试（同时准备提交上游）；建立`debian/tests`与矩阵 | 测试结果记录；矩阵覆盖全部补丁，缺口列出 |
| 6 模拟升级 | 把队列rebase到较新的KWin（Plasma 6.6后续版本或KDE neon已有的6.7.5）并构建，不部署 | 记录每条补丁的状态（干净、冲突、已被上游合入）与耗时 |
| 7 收尾 | 删除`vendor/kwin`，`vendor/manifest.json`与文档同步；AGENTS.md中Vendor约定的修改由用户确认 | 构建与发布只用`packages/kwin` |

## 全面迁移

试点通过后依次迁移：kscreen（与KWin共享头文件）、plasma-mobile（修改最多，约1300行）、plasma-settings、plasma-keyboard、xdg-desktop-portal-kde、wl-clipboard、plasma-camera、libcamera、qtmultimedia，然后是非Debian上游（Mesa、FFmpeg、Snapshot、typesafe-computer-use、arc-cua、LiteRT）。全部完成后`vendor/`删除，仓库中的上游源码只剩补丁。

与Rungic改名（docs/70）的关系：改名的B阶段暂停，等补丁队列迁移完成后再做，这样vendor中的改名只需修改相应补丁，也能用新的测试层验证。2026-09-26用户决定先迁移改名涉及的6个组件（见下文“第二批”），其余组件之后按上面的顺序迁移。

## 风险与待定

- 迁移期`vendor/`与`packages/`并存，构建工具同时支持两种来源，直到对应组件迁完。（2026-09-27已全部迁完，`tools/stage_vendor.py`与`tools/vendor_debian.py`已删除，`build_on_device.py`只接受`packages/`。）
- `gbp pq export`首次会把Ubuntu原有补丁改写为`From/Date/Subject`格式（调研实测），接受这次一次性改动，或只导出`rungic/`主题下的补丁。
- 主机没有arm64的binfmt（qemu-user-static未安装），L1/L2目前只能在手机上运行；如需在主机上跑arm64容器，另行安装。
- 主机上的x86测试环境（`tools/pq/build.Dockerfile`，镜像`rungic-build-kwin:26.04`）可以编译并运行KWin的全部157个测试，但这只能验证与平台无关的代码；Android后端和实机行为仍靠手机上的L3验收。经验（2026-09-26）：
  - 每个源码状态用新的构建目录（或`git archive`导出）。复用目录并用`rsync -a`同步时，源文件保留旧的修改时间，CMake不会重新编译，结果会混入旧目标文件（曾因此误判`testFtrace`挂起）。
  - 在xvfb下串行运行（`xvfb-run ctest -j1`）。不带显示或并行运行时，有57个测试因环境原因失败，结果没有参考价值；xvfb串行运行时，基线和我们的补丁都是同样的24个失败，其余全部通过。
  - 比较的是失败集合，而不是通过数：与不含我们补丁的基线（上游＋Ubuntu补丁）在同一镜像、同一命令下对比，只看两边的差异。
- KWin的`BUILD_TESTING=ON`构建会明显变长，测试构建与发布构建分开。

## 试点进度

**1 配方（完成）**：`packages/kwin/recipe.json`。三个文件从Launchpad下载，sha256与记录一致；`.dsc`签名者（Ubuntu上传者）的公钥不在Debian密钥环中，完整性以sha256为准（与调研一致）。`dpkg-source -x`的结果与原`dffc717e:vendor/kwin`逐字节一致。工具链镜像`tools/pq/Dockerfile`固定到`ubuntu@sha256:da6fc2be…`（gbp 0.9.42、quilt 0.69、lintian 2.129、dpkg-source 1.23.7）。

**2 补丁队列（完成）**：`tools/pq_import_history.py`按`tools/pq-history/kwin.json`把vendor历史转成16条补丁（5个历史补丁、`d1301f2e`中的共享头文件、10个功能提交；只改changelog的提交跳过），接在Ubuntu的2条补丁之后。从orig tarball开始用quilt应用全部补丁，结果与`HEAD:vendor/kwin`逐字节一致（`debian/patches`除外，符号链接按其指向的内容比较）。`e07d7448`暂按原提交保留为一条，待拆分；共享头文件暂以补丁携带，待改为`-dev`包。

**3 工具（完成）**：`tools/pq.py`（fetch、source、prepare、export、lint、verify、tests）与单元测试`tools/test_pq.py`；`prepare`→`export`往返后补丁不变；`export`只写回我们的主题与series，Ubuntu原有补丁保持原样。`tools/build_on_device.py`对有配方的组件改用`pq.py source`。

**核对上游状态时的更正**：`xdg-min-above-max`原计划作为上游候选，但xdg-shell.xml（`set_max_size`/`set_min_size`）明确规定最大值小于最小值是`invalid_size`协议错误，上游KWin的行为符合协议；该补丁是为微信4.1的客户端缺陷放宽协议，改标`Inappropriate`，应向客户端报告。其余标为`Pending`的补丁（screencast-mobile-shell、ftrace-fd-markers、output-internal-to-scripts、virtualkeyboard-commit-text）在提交上游前同样逐条核对。

**5 测试（部分）**：为`xdg-min-above-max`按上游写法新增集成测试`testXdgShellWindow::testMinimumAboveMaximum`（最小700×400、最大360×800的窗口保持连接并丢弃冲突的最大宽度；再把最大高度设到最小以下，两个方向都丢弃；无协议错误），经`pq.py prepare`在对应提交上fixup后导出。`pq.py tests kwin --gaps`列出6条无测试覆盖的补丁：共享头文件（随使用它的补丁覆盖）、两条投屏补丁（需要电视接收端，人工项）、宿主滚动、空闲抑制、脚本中的`internal`属性。

**6 模拟升级（完成分析）**：KWin上游6.6系列止于6.6.6，下一个稳定版是6.7.5（KDE neon为resolute提供的版本）。tarball经KDE发布密钥环中Bhushan Shah的签名（`B3CB…928CAEFC`）验证。把18条补丁依次三方合并到6.7.5：4条干净（空闲抑制、两条录屏、脚本`internal`），其余冲突；逐条单独合并的结果相同。冲突集中在嵌套Wayland后端（上游6.7重构了`wayland_egl_backend`、`wayland_layer`、`wayland_output`等，`xdgshell.cpp`的尺寸改为`QSizeF`，`virtualkeyboard_dbus.cpp`、`drmdevice.cpp`也有改动）。Ubuntu的2条补丁同样冲突（其中logind回退可能已部分进入上游）。记录：`.work/research/patch-queue/pq-upgrade-report.txt`、`pq-upgrade-solo.txt`。结论：跟进一个上游大版本时，需要人工调整并重测的正是这些补丁对应的功能；降低长期成本最有效的办法是把Android宿主相关的改动集中到更少的位置（例如独立的后端或插件），并把通用修正提交上游。resolute仍是Plasma 6.6，本次不解决冲突、不部署6.7。

## 第二批：改名涉及的6个组件（2026-09-26）

用户决定只先迁移Rungic改名会改到的组件：plasma-mobile、plasma-settings、kscreen、FFmpeg、Snapshot、typesafe-computer-use（提交`baef71f4`）。每个组件都由`tools/pq_import_history.py`从vendor历史生成补丁队列，并与`c36596035f9e:vendor/<名称>`逐字节核对一致（空目录、`debian/`、`.pc`除外）；随后删除这6个vendor目录，以及已被`packages/kwin`取代的`vendor/kwin`。（2026-10-03：2026-09-30 的历史改写后，任何提交里都已没有 `vendor/`，这两个导入工具连同 `tools/pq-history/` 的计划和 `desktop/patches/` 中只供它们导入的旧补丁副本一并删除，需要时见 git 历史。）

| 组件 | 上游来源 | 补丁 | 说明 |
|---|---|---|---|
| plasma-mobile | Ubuntu 6.6.5-0ubuntu0.1 | 13条（2条回移） | 按功能拆分；两个修正提交用autosquash并入对应功能 |
| plasma-settings | Debian 25.12.0-1（resolute同步） | 2条 | orig是KDE签名的发布包，与vendor导入时的tag包内容相同 |
| kscreen | Ubuntu 4:6.6.5-0ubuntu0.1 | 1条 | 以前只把重建的`kcm_kscreen.so`换进Ubuntu二进制包，现在整个源码包构建；vendor中的`PROJECT_DEP_VERSION`改动就是Ubuntu自己的补丁 |
| FFmpeg | 8.1.2发布包（配方类型`upstream`） | 2条 | `libx264_sw`改名与编解码器注册分开 |
| Snapshot | 51.0发布包（`upstream`，含Cargo依赖） | 2条 | 编码器识别与Android相机时钟分开 |
| typesafe-computer-use | 固定提交`24eb292`（类型`git`，按tree哈希核对） | 1条（9行） | 我们的Linux适配器移到`plasma/cua/typesafe/`，由overlay放入 |

**工具的扩展**：
- **overlay**：配方中“源码树路径→仓库共享文件”。共享文件（录屏快捷设置、`android-display-client.h`、编解码客户端与FFmpeg适配、clicker的Linux适配器）在打补丁前放入源码树，补丁只引用、不修改，仓库里仍只有一份（与原来的符号链接等价）。整体替换上游文件时记录上游文件的sha256，上游一旦修改该文件就拒绝构建，提示复核。
- **非Debian上游**：`upstream`（发布包＋sha256）和`git`（固定提交，`git archive`生成确定的tar，按tree哈希核对）。这类组件只有`debian/patches`；编辑时为gbp生成最小的control与changelog，只在`.work`中。构建它们的项目包在`package.json`中写`upstream`，构建目录中的`$SRC/upstream/<名称>`是补丁后的源码，包的内容标识包括配方、补丁与overlay文件。
- **历史导入**：`distribution`步骤（发行版补丁已在vendor中应用的那一步，只记录不重复）；`--ref`（vendor目录删除后，指定删除前的提交复现导入）。
- `verify`：参照树中无法解析的符号链接算作差异（以前只看stdout，会误报一致）；只在一侧存在的空目录不算差异。

**构建核对**：版本号不变的组件在手机上从补丁队列重新构建，与发布仓库中现有的包比较`md5sums`：
- plasma-mobile `+moto2`：1553个文件中1551个逐字节相同；另外两个是panel与taskpanel两个applet的`.so`，导出符号相同，差别是新构建中`NavigationPanelComponent.qml`多了一个QML预编译（AOT）函数。qmlcachegen能预编译哪些函数取决于构建时已安装的QML类型信息，旧包是更早的系统状态下增量构建的；源码相同，行为等价。
- plasma-settings `+moto1`：82个文件全部逐字节相同。
- kscreen `+moto3`（整包构建）：与以前替换插件的`+moto2`相比，文件清单与依赖相同；6个二进制文件现在由我们从源码编译（以前除`kcm_kscreen.so`外都是Ubuntu编译的），另有changelog不同。
- 项目包（新版本0.196）：FFmpeg的9个文件去掉构建ID与debuglink后完全相同；Snapshot唯一的二进制文件，字符串差异全部是构建路径`src/vendor/snapshot`→`src/upstream/snapshot`（Rust把源码路径写进panic信息）；clicker中的`typesafe_computer_use`目录完全相同。比较时要用能识别aarch64的`llvm-objcopy`：主机的`objcopy`不认识aarch64，出错时输出为空，两边的哈希会“相同”。


## 第三批：其余组件（2026-09-27，docs/73第二阶段）

plasma-keyboard、xdg-desktop-portal-kde、wl-clipboard、arc-cua、LiteRT、libcamera、qt6-multimedia、plasma-camera、Mesa全部迁为补丁队列，并与迁移前的vendor树逐字节核对一致；`vendor/`只剩`manifest.json`（`native/plasma/`、`plasma/firefox-mobile/`两棵直接跟踪的外来树）与空的审计豁免。工具随之扩展：配方`build_arch_only`（只构建架构相关包，`dpkg-buildpackage -B`）、`pq.py source`允许只有`debian/patches`的上游、历史导入可声明参照树未含发行版补丁。Mesa用`tools/build_mesa.py`在手机上以meson构建并由`plasma/package-mesa.py`打包，版本取自`packages/mesa/debian/changelog`。详情与构建核对见docs/73。

## 构建机：Mac mini（2026-09-27）

用户决定ARM64打包改在Mac mini上进行（`macmini.wire.net`，Apple M4 10核、16 GB，OrbStack Docker，K8以SSH密钥登录）。`tools/build_on_device.py --host macmini`、`tools/build_mesa.py --host macmini`与`tools/rungic_package.py build --host macmini`在一个长期运行的Ubuntu 26.04 ARM64容器中构建（`tools/pq/arm64-host.Dockerfile`，镜像按Dockerfile与ddebs源文件的哈希打标签，构建树在Docker卷`rungic-build`里），产物同样收进本机的发布仓库；默认构建机是Mac mini（`$RUNGIC_BUILD_HOST`可改），手机路径保留作后备。镜像的Ubuntu源用USTC镜像（从Mac mini约6.7 MB/s，官方ports约0.5 MB/s）。

核对（与手机上的完整构建逐文件比较，ELF去掉build-id与debuglink）：plasma-settings主包83个文件、调试包3个文件完全一致；libcamera的gstreamer1.0-libcamera、libcamera-dev一致；两边的构建依赖版本（`.buildinfo`）除运行时Mesa外相同。xdg-desktop-portal-kde的主程序多出/缺少两个QML预编译符号（`QQmlPrivate::AOTCompiledContext::getValueLookup`等），编译任务数改为与手机相同的4个后依旧：qmlcachegen按构建环境里**已安装**的QML模块解析类型，手机容器装着整个桌面，能预编译的绑定更多；Mac mini的容器只有声明的构建依赖。两者运行时都正确（未预编译的部分由解释器执行），Mac mini的结果只取决于声明的构建依赖，更可复现。libcamera的13个包中，`libcamera-ipa`的6个`*.so.sign`与`libcamera.so`中256字节的一段不同：IPA模块用每次构建随机生成的密钥签名、公钥嵌入库中，任何两次构建都会不同；其余文件一致。手机上的KWin `+rungic3`是增量构建，不能作比较。KWin含LTO的完整构建在Mac mini上约6分钟，手机上一小时以上。

传输问题：`docker exec cat`经ssh传大文件时曾以成功状态提前结束（13.7 MB的包只收到12.9 MB），现在上传与下载都核对大小与SHA-256，不一致重试。

手机与Mac mini在同一局域网（192.168.5.60与192.168.5.45，也可经wire.net的10.77.0.x互通），大文件由手机直接传给构建机，不经本机与VPN：手机容器的专用密钥`/root/.ssh/id_ed25519_buildhost`在Mac的`authorized_keys`中受限为`restrict`、只接受手机的两个地址、强制命令为构建容器里的`tools/pq/rungic-transfer`（`put DIR`解包到、`get FILE`读取`/root/rungic-build`下的路径，拒绝绝对路径与`..`）。实测6.8 MB/s（经本机转发约1.2 MB/s），其他命令与路径被拒绝。崩溃符号化已改用此路径。2026-10-03起手机依次尝试wire.net（`10.77.0.20`）与局域网地址（`MacMini.PHONE_HOSTS`），实测两条都约12 MB/s；开发部署的包也由手机直接取（docs/97）。

### 构建端选择补充（2026-10-04）

`tools/build_on_device.py --host local-docker` 在当前机器的原生 ARM64 Docker 中复用上述镜像、缓存卷和传输校验；`tools/build_mesa.py` 与 `tools/rungic_package.py build` 也接受该目标。可用 `RUNGIC_BUILD_HOST=local-docker` 统一选择。编译并行度按 Docker VM 的 CPU 和内存限制，而非宿主硬件：最多 4 个任务，每 2 GiB 一个任务，显式 `--jobs` 可降低上限。x86 Docker daemon 会在构建前被拒绝，不自动改走 QEMU。

远程 Mac 路径仍以原作者 `choukevin@macmini.wire.net` 为默认地址，用 `RUNGIC_BUILD_SSH=builder@host` 配置自己的地址；`build_on_device.py --host macmini --ssh-host builder@host` 可临时覆盖。两条 Docker 路径都读取所在 Mac 的 `scutil --proxy`，给镜像构建和容器命令显式传入代理。本补充只验证离线工具行为，本机镜像创建与构建验收见 [husky 3.1 记录](108-husky-port.md#任务-31本机-arm64-docker-与可配置远程构建端)。

## Android宿主与配置源码收尾（2026-09-30）

原先直接维护的`native/plasma`与`plasma/firefox-mobile`也已进入配方流程，见[73篇收尾记录](73-reduce-upstream-changes.md#remaining-source-trees-migrated-2026-09-30)。git类型配方可用`subdir`选取固定提交中的子树，此时`tree`为子树哈希；可用`exclude`显式排除随上游入库的非构建内容，升级后排除项不存在会报错。所有实际源码仍只在`.work`展开。

Android宿主、Smithay、Winit分别维护配方，由`tools/prepare_android_host.py`组装供交叉编译；自有模块作为overlay。宿主上的项目包也支持`upstream`输入，与设备构建共用`stage_sources`，Firefox移动配置已用真实DEB验证。没有恢复直接跟踪外来源码的例外。
