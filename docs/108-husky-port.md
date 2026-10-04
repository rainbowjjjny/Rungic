# 108 Pixel 8 Pro（husky）移植计划与记录

> **执行方式：** 按任务逐项推进，每项有可验证的验收；步骤用 `- [ ]` 跟踪。Claude 负责编排、设备操作与 GCP 编译机，Codex（gpt-6.1-sol / high）负责仓库代码改动与交叉审查。

**总目标：** 在 Pixel 8 Pro（husky，Tensor G3 / Mali-G715）上，Rungic 从独立安装到 Plasma 桌面出现、可触控操作，达到 `rungic-three-stage-image` 的验收顺序终点（base ready → payload verified → install/mount → release ready → account-prepare → account form → desktop loading → Plasma）。图形先用 CPU 软件渲染，GPU 加速不在本目标内。

**架构：** 沿用 X70/G100 的做法：只重编 GKI（`boot` 分区），加 `kernel/targets/gki/lxc_defconfig` 的六项选项，用 KABI 预留位补丁保持原厂模块 ABI，恢复原厂 GKI 模块签名证书；`vendor_kernel_boot`、`vendor_dlkm`、`system_dlkm` 保持原厂不动。Magisk 在 `init_boot`，换 `boot` 不影响 root。图形侧新增一条“无 KGSL”的软件渲染路径。

**技术栈：** ACK android14-6.1 + Kleaf/Bazel（GCP x86_64 编译机），Magisk 31.0，LXC，Mesa llvmpipe，现有 `tools/ci/*`。

## 全局约束

- 设备：Pixel 8 Pro，serial `3B271FDJG005G7`，product/device `husky`，SKU `GE9DP`。
- 固件：`google/husky/husky:16/CP1A.260405.005/15001963:user/release-keys`，安全补丁 2026-04-05，bootloader `ripcurrent-16.4-14540574`，当前槽位 `_b`。
- 原厂内核：`6.1.145-android14-11-gfa1d6308d1fe-ab14691759`；4K 页；`CONFIG_MODULE_SIG_PROTECT=y`、`CONFIG_MODVERSIONS=y`、`CONFIG_MODULE_SIG_ALL=y`（sha1）；`CONFIG_LTO_NONE=y`、`CONFIG_CFI_CLANG=y`；编译器 `Android (10087095) clang 17.0.2 r487747c`。构建参数必须从这些实测值推出，不照搬 X70。
- 内核源码：ci.android.com 构建 14691759 `kernel_aarch64` 的 `manifest_14691759.xml`；`kernel/common` = `fa1d6308d1fe803c3fdebcd3ee6f7a1155fc3462`（分支 `android14-6.1-2025-09`）。
- 原厂包：`husky-cp1a.260405.005-factory-c22affec.zip`，SHA-256 `c22affece233e05bbbbd61a9f919ebf329f9532a78c1882dbc5ee105ebfbc105`（Google 官网公布值，已核对）。
- Root：Magisk 31.0（`Magisk-v31.0.apk` SHA-256 `2c8a488b9a5293e578e95ae4f07e3c57aba4feec4a52ca4dd852a2692d6dd4e8`），修补后的 `init_boot` 已刷入 `init_boot_b`。
- 编译机：GCP `rungic-kernel-build`（asia-northeast1-b，c3-standard-22，86 GB 内存，400 GB SSD，Ubuntu 24.04）。不用时停机。
- SELinux 保持 Enforcing；不改 vbmeta；不刷 `super`；不清数据。
- 写设备授权：用户 2026-10-04 授权 Claude 自行决定刷写（`boot`、`init_boot` 等可用原厂镜像回退的分区），不必逐次询问。仍须遵守：新镜像先 `fastboot boot` 临时启动验证、通过后才刷入；刷入前确认回退镜像在手；用户 2026-10-04 进一步说明：手机上没有用户数据，清数据的操作（擦 userdata/metadata、刷 super、改 vbmeta 标志）也授权 Claude 自行执行；执行前仍须确认有回退所需的原厂镜像（`.work/husky/stock/` 与出厂包），并在本文“记录”中写明做了什么。
- 回退：`fastboot flash boot_<slot> <stock boot.img>`；`fastboot flash init_boot_<slot> <stock init_boot.img>` 去掉 root。每次写入前重新读取当前槽位，不写死 `_b`。
- 文件位置（AGENTS.md:108）：工作输入与产物一律在 `.work/husky/`；`~/projects/pixel8-firmware/` 只作为用户自己的原始备份，工具不直接读它。
- 设备命令：root 命令用 stdin 喂给 `su`（`adb -s <serial> shell su < script.sh`），不用多层 `su -c` 引号；namespace 检查用 Magisk 的 `/data/adb/magisk/busybox`，不依赖 Android 自带工具。
- 编译机：只经 IAP 隧道访问（`gcloud compute ssh rungic-kernel-build --zone=asia-northeast1-b --tunnel-through-iap`；2026-10-04 公网 22 端口出现不可达）；同步前核对主机身份与出口网络（AGENTS.md:73-88）。

## 原厂内核配置差距（2026-10-04 从 `/proc/config.gz` 读取）

| 选项 | 原厂 | 处理 |
|---|---|---|
| SYSVIPC、POSIX_MQUEUE、IPC_NS、PID_NS、USER_NS、DEVTMPFS | 未开 | `lxc_defconfig` 打开；SYSVIPC 需 task_struct KABI 补丁 |
| UTS_NS、NET_NS、CGROUPS、MEMCG、CPUSETS、CGROUP_FREEZER、CGROUP_BPF、OVERLAY_FS、VETH、BRIDGE、NF_NAT、MASQUERADE、FUSE_FS、SECCOMP_FILTER | 已开 | 不动 |
| CGROUP_DEVICE、CGROUP_PIDS | 未开 | **不开**（`kernel/README.md:98`：会改变 3,252 个 CRC）；沿用 cgroup2 + BPF 设备过滤 |

## 里程碑

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M0 ✅ | 设备事实、spec、原厂基线 | spec 字段全部来自实测；基线模块数与 dmesg 记录在案 |
| M1 ✅ | LXC GKI 内核 | 0 个 CRC 差异；`fastboot boot` 开机后模块数等于基线、Enforcing、namespaces 可用；用户同意后刷入（2026-10-04 完成） |
| M2 | 无 KGSL 的软件渲染路径 | 容器在无 KGSL 时启动；KWin 用 QPainter；Mesa 带 llvmpipe |
| M3 | RungicOS rootfs、APK 与独立安装包 | 从零构建出 rootfs、APK、安装包，`standalone.py verify` 通过 |
| M4 ✅ | 实机安装与验收 | 到达 Plasma 桌面并可触控（2026-10-04） |

M2/M3 依赖 M1 的实测结果（例如 KWin 在 Mali 上能否用 QPainter/SHM 输出），所以先不写细节，避免按假设设计。已知的改动面（详见研究摘要）：

- `system/rungic-plasma:31-33,169` 与 `system/plasma.config:44-46` 强依赖 `/dev/kgsl-3d0`，需改为可选。
- `desktop/mesa-meson-options` 只编 freedreno/zink、`llvm=disabled`，需增加 llvmpipe 变体；`desktop/package-mesa.py` 会替换掉 Ubuntu 的 llvmpipe。
- `desktop/gpu-env`、`desktop/kwin`、`desktop/session:38-41` 写死 KGSL 变量。
- `android/host/src/android/gpu_allocator.rs:25-69` 使用高通 UBWC 修饰符与 gralloc 位。
- GPU 探针与契约：`shared/graphics/gpu-probe.c`、`desktop/gpu-ahb-probe.c`、`quality/contracts/gpu-device.json`、`release/acceptance.json:169`。
- `tools/ci/standalone.py:192-205` 只核对 `boot` 分区哈希，Pixel 需按 spec 声明的分区核对。
- 仓库没有发布版 rootfs（`gh release list` 为空），M3 需要自建 CI2 rootfs，建议另开 ARM64 编译机。

## 分工与协作

- **Claude**：本文件维护、任务派发与验收；所有 adb/fastboot 操作（手机接在 Mac 上）；GCP 编译机的操作。
- **Codex**：仓库内代码改动（spec、提取工具、内核补丁移植、`standalone.py` 适配）；审查 Claude 的提交。Claude 审查 Codex 的提交。
- 每个任务一个分支，审查通过后合入 `port/pixel8-husky`；推送到 `origin`（`rainbowjjjny/Rungic`），不向上游推送。
- 每个任务结束更新本文“记录”一节：做了什么、验证了什么、剩什么。

## M0：设备事实与 spec

### 任务 0.1：原厂基线采集（Claude，只读）

**产物：** `.work/husky/baseline/`（不入库），摘要写入本文“记录”。

- [x] 采集身份与状态：
  ```bash
  S=3B271FDJG005G7
  for p in ro.build.fingerprint ro.product.device ro.boot.hardware.sku ro.bootloader ro.boot.slot_suffix ro.boot.verifiedbootstate ro.boot.flash.locked ro.build.version.sdk; do echo "$p=$(adb -s $S shell getprop $p)"; done > .work/husky/baseline/props.txt
  adb -s $S shell "su -c 'uname -r; getenforce; lsmod | tail -n +2 | wc -l; cat /proc/modules | cut -d\" \" -f1 | sort'" > .work/husky/baseline/kernel.txt
  adb -s $S shell "su -c 'dmesg | grep -iE \"module|sig|taint\" | tail -200'" > .work/husky/baseline/dmesg-modules.txt
  ```
- [x] 验收：`kernel.txt` 里有已加载模块数 `N_stock`（之后 M1 对照用），且 `getenforce` 为 `Enforcing`。

### 任务 0.2：Pixel 原厂包提取（Codex）

`tools/prepare_g100_stock.py` 只认 Motorola XML 包（`flashfile.xml`、`super.img_sparsechunk.N`），不能用于 Pixel 出厂包。

**文件：**
- 新建：`tools/prepare_pixel_stock.py`：校验出厂 zip 的 SHA-256 → 取内层 `image-husky-*.zip` → 输出 `boot`、`init_boot`、`vendor_boot`、`vendor_kernel_boot`、`dtbo`、`vbmeta`、`vendor_dlkm`、`system_dlkm` 镜像和各自的 SHA-256 清单；从 `boot.img` 解出 kernel `Image`；从 `vendor_kernel_boot` ramdisk、`vendor_dlkm`、`system_dlkm` 解出全部 `.ko` 到 `modules/`。
- 新建：`tools/test_prepare_pixel_stock.py`（`covers: install.device-spec`）。
- 修改：`quality/features/install.yaml` 的 `install.device-spec.code` 加入以上两个文件。

- [x] 写失败测试：用 `tmp_path` 造一个最小出厂 zip（内层 zip 含假 `boot.img` 等），断言：摘要不符时在解包前抛错；输出清单列出每个分区和它的 SHA-256；内层 zip 缺分区时拒绝。
- [x] 运行 `python -m pytest tools/test_prepare_pixel_stock.py -v`，确认失败。
- [x] 实现，重跑通过。
- [x] 输出 `verification.json`，字段对齐 `tools/ci/preflight.py:46-60` 实际读取的内容（identity、分区摘要）；Pixel 没有 `super` 分片，预检改为按 spec 声明的分区列表核对，不伪造 `super_sha256`。同时保留链式 vbmeta（`vbmeta_system`、`vbmeta_vendor`）的摘要。
- [x] 在真包上运行，输出到 `.work/husky/stock/`，并核对 `boot.img` SHA-256 = `f102b2951357e6536b15cad20e69e14064e1ba5cb9f84c97cc477d204e0361d7`、`init_boot.img` = `f18685aef7a50581d5261fe9940214b3a7db4d310d8891810cde487b219b57fe`。
- [~] `tools/run-tests.sh` 通过后提交。（实际：相关子集全部通过；完整 `run-tests.sh` 在 macOS 上原本就有 81 个失败、18 个收集错误，缺 `gi` 等 Linux 依赖，在未改动的代码上同样复现，见 2026-10-04 记录）

### 任务 0.3：husky spec 与 knowledge（Codex 写，Claude 用实测值核对）

**文件：**
- 新建：`profiles/devices/google/husky/CP1A.260405.005.json`（`schema_version: 1`，字段结构同 `profiles/devices/motorola/vantage_cn/W2WV36.55-75-15.json`）。
- 新建：`profiles/devices/google/husky/CP1A.260405.005-knowledge.md`。

- [x] `identity`：product/device `husky`、sku `GE9DP`、上文指纹、bootloader `ripcurrent-16.4-14540574`。
- [x] `stock`：0.2 输出的全部摘要；`avb_public_key_sha1` 用 avbtool 从原厂 `vbmeta.img` 读取。
- [x] `kernel`：manifest 来源 ci 14691759、common commit、`stock_release`、`page_size: 4096`、`module_trust_certificate_sha256`（1.4 得出后补上；这是唯一允许后补的字段，补上后 spec 才算定稿）。
- [x] `release_requirements.android_api: 36`；`purity` 留空（不做预装清理）。
- [x] Pixel 分区布局差异（`vendor_kernel_boot` 等）若现有字段放不下，按 `references/device-onboarding.md:34` 扩展 schema 并修改读取方，不伪造字段。
- [x] 验收：`tools/ci/preflight.py` 以只读方式对真机运行，全部通过（必要时为 Pixel 修正预检，测试同步更新）。

## M1：LXC GKI 内核

### 任务 1.1：固定源码并同步（Claude，编译机）

- [x] 把 `manifest_14691759.xml` 存为 `kernel/targets/gki/android14-6.1-manifest.xml`。
- [x] 编译机安装 `repo`、`git`、`python3`，执行 `repo init -u https://android.googlesource.com/kernel/manifest -m <上述文件>` 并 `repo sync -c -j16`。
- [x] 验收：`git -C common rev-parse HEAD` = `fa1d6308d1fe803c3fdebcd3ee6f7a1155fc3462`。

### 任务 1.2：原样重编基线（Claude，编译机）

在改任何东西之前，先证明构建流程能复现原厂 ABI。

- [x] 确认 manifest 中每个 project 都钉在具体 commit，clang 预编译版本为 r487747c。
- [x] `tools/bazel build --config=fast --lto=none //common:kernel_aarch64`（`--lto=none` 来自原厂 `CONFIG_LTO_NONE=y`；stamp 只影响版本串，单独核对，不当作 ABI 依据）。
- [x] 生成的 `.config` 与原厂 `/proc/config.gz` 逐项比较，差异必须为 0（版本串除外）。
- [x] 用 `tools/ci/module_abi.py <Module.symvers> .work/husky/stock/modules --output .work/husky/abi-baseline.json` 对照原厂模块。
- [x] 验收：0 个 CRC 不匹配；把每个模块的 `unresolved_by_gki` 存为基线（这些应是由其他 vendor 模块导出的符号）。若不为 0，先查明原因，再继续。
- [x] 用 `pahole` 导出原样构建的 `task_struct`、`nsproxy`、`ipc_namespace`、`pid_namespace`、`user_namespace`、`cred` 布局，作为 1.3 的对照。

### 任务 1.3：移植 KABI 补丁到 6.1（Codex 写补丁，Claude 编译验证）

**文件：**
- 新建：`packages/gki-android14-6.1/recipe.json`（格式同 `packages/gki-android15-6.6/recipe.json`）。
- 新建：`packages/gki-android14-6.1/debian/patches/rungic/0001-Preserve-task_struct-ABI-when-enabling-SYSVIPC.patch`，把 `sysvsem`/`sysvshm` 移进 `ANDROID_KABI_RESERVE` 槽（6.6 版用 6–8 槽，6.1 要按该 commit 的 `include/linux/sched.h` 实际空闲槽位来定）。
- 按需新建：`kernel/targets/gki/android14-lxc-symbols`。
- 修改：`quality/features/install.yaml` 的 `install.gki-kernel` 加入 6.1 相关文件。

- [x] 在 `fa1d6308` 的 `include/linux/sched.h` 中确认 KABI 槽 6–8 未被占用（分支上的观察不算数），并用 `static_assert` 校验 `sysv_sem`（ARM64 上 8 字节）放进 1 个槽、`sysv_shm`（16 字节）放进相邻 2 个槽，且对齐。
- [x] 用 `pahole` 对比原样构建与候选构建：上述结构体除 KABI 槽内部外，偏移和大小完全一致。
- [x] 先在原样源码里查明这些结构体有没有依赖 `CONFIG_SYSVIPC`、`IPC_NS`、`PID_NS`、`USER_NS`、`POSIX_MQUEUE` 的条件字段，有证据再补，不照搬 6.12 的 Rust 修复。
- [~] 补丁经 `tools/pq.py` 队列管理（AGENTS.md:115），用 `pq prepare/export` 生成。（实际：补丁在编译机同一 commit 上用 git 生成，按 DEP-3 写入并通过 `pq lint`；没有在 Mac 上 `pq prepare`，避免克隆整个 kernel/common）
- [x] 配置片段：在编译机工作区建 `rungic/BUILD.bazel`（`exports_files(["lxc_defconfig"])`），用 `--defconfig_fragment=//rungic:lxc_defconfig` 构建；先查 manifest 钉住的 Kleaf 版本是否支持这个参数，不支持再改用 pq 补丁修改 `gki_defconfig`。
- [x] **先修工具（阻断项）**：`tools/ci/module_abi.py` 原先只比较候选 symvers 里存在的名字，候选少导出的符号只会被记为 unresolved，退出码仍为 0。已加 `--baseline <1.2 的报告>`：模块清单（相对路径与 SHA-256）必须相同，每个模块的候选 unresolved 引用必须是基线的子集，否则非零退出并列出模块/符号；CRC 差异仍失败。报告保留 symvers SHA-256，`--config <.config>`、`--image <Image>` 分别追加可选摘要；不传 baseline 时保留旧行为与报告字段。测试在 `tools/ci/test_module_abi.py`（`covers: install.gki-kernel/E1`），先失败再实现，使用合成 ELF64 `.ko` 的 `__versions` 段。
- [x] `module_abi.py --baseline` 对照原厂全部模块。
- [x] 验收：0 个 CRC 不匹配，并记录可比较的引用数和未覆盖的范围（`install.gki-kernel` E1）。其他选项引起的差异逐个定位，不放宽检查。

### 任务 1.4：恢复模块签名信任并重打包 boot（Claude，编译机或 Mac）

- [x] 从原厂 `Image` 提取 GKI 模块签名证书，记下 SHA-256，回填到 spec 的 `module_trust_certificate_sha256`。
- [x] 用这张证书实际验证原厂 `system_dlkm` 模块的签名（`scripts/extract-module-sig.pl` 或 openssl 校验 PKCS#7），证明它们确实是用这张证书签的；同时确认 `restore_module_trust.py:39-53` 的前提成立（证书在原厂和候选 Image 中都只出现一次，长度相同，subject 相同）。前提不成立就停下，重新评估。
- [x] `tools/ci/restore_module_trust.py <stock Image> <candidate Image> <stock cert> --certificate-sha256 <sha> --output .work/husky/Image.trusted --report .work/husky/trust-report.json`。
- [x] 用 AOSP `mkbootimg` 重打包（header v4），保持原厂的 kernel 压缩方式、`os_version`/`os_patch_level`、cmdline；`kernel_size` 必然变化。原厂 boot 里的 GKI 认证签名区和 AVB footer 对新 kernel 已失效：去掉旧签名并说明；bootloader 已解锁（orange），不重签、不改 vbmeta。
- [x] 验收：`trust-report.json` 显示只替换了证书字节；`unpack_bootimg` 比对新旧 boot：除 `kernel_size`、签名区和 footer 外字段一致，并记录每一处差异。

### 任务 1.5：实机临时启动验证（Claude；需用户同意）

- [x] **对照组先行**：征得用户同意后，先用 `fastboot -s 3B271FDJG005G7 boot` 临时启动**原厂** `boot.img`，确认这个 bootloader 支持临时启动，且 `init_boot`（Magisk）和 vendor 镜像照常被用上（开机后 `su` 可用、模块全部加载）。
- [x] 对照组通过后，再临时启动候选 `boot-lxc.img`。
- [x] 恢复办法：任何一次卡住，都长按电源键 + 音量下键回到 fastboot，正常重启即回到已刷入的原厂 boot（临时启动不会写入分区）。若 `fastboot boot` 不被支持：记录到 knowledge，再和用户确认是否改为直接刷 `boot_<slot>`。
- [x] 开机后检查脚本 `.work/husky/check-kernel.sh`，用 `adb -s 3B271FDJG005G7 shell su < .work/husky/check-kernel.sh` 执行。脚本内容：`uname -r`（必须带候选的版本串，证明确实是新内核）；`getenforce`；`cut -d' ' -f1 /proc/modules | sort`（与基线名单逐个比较，不只比数量）；`zcat /proc/config.gz` 中六项选项；用 busybox `unshare -U -p -i -m -f` 进入新 namespace，检查里面 `$$` 为 1、能写 `/proc/self/uid_map`；用 `ipcmk -Q`/`ipcrm` 验证 SysV IPC 可用；`mount -t devtmpfs` 到临时目录；完整 `dmesg` 存档，并搜索 `Unknown symbol`、`disagrees about version`、`module verification failed`、`Loading of unsigned module`、`protected`。
- [x] 验收：模块名单与基线完全一致；Enforcing；六项 `=y`；namespace、uid_map、IPC、devtmpfs 都正常；dmesg 无上述错误；触屏、Wi-Fi、显示正常；Magisk `su` 可用。
- [x] 通过后再次征得用户同意，重新读当前槽位，`fastboot flash boot_<slot>`，重启并复查一次。

## M2：无 KGSL 的软件渲染路径

依据（2026-10-04 代码梳理）：KWin 的 Android 后端在打不开渲染设备时只记警告，退回 QPainter；QPainter 输出走 `/dev/shm` 共享内存缓冲，宿主端用 Android 自己的 EGL/GLES 合成 `RenderItem::Shm`，在 Mali 上可用；触摸输入链路与 GPU 无关。真正挡路的是容器启动对 `/dev/kgsl-3d0` 的硬依赖和写死的 KGSL 环境变量。手机实测：无 `/dev/kgsl*`；`/dev/dri/card0`、`renderD128` 属于显示控制器 `exynos-drm`，没有 3D；GPU 是 Mali-G715（`/dev/mali0`，kbase），Mesa 无可用驱动；屏幕 1008×2244、360 dpi；9 核、12 GB 内存。

Codex 负责 2.1–2.4 的代码与测试；Claude 审查、合并并在 M4 实机验证。

### 任务 2.1：GPU 设备节点改为可选（容器能启动）
- 修改 `system/rungic-plasma:31-39,169-179`：KGSL 与 `/dev/dma_heap/system` 的 `device_rule` 只在节点存在时添加；节点缺失只打日志，不退出。
- 修改 `system/plasma.config:44-46`：这两个绑定挂载加 `optional`。
- 测试：改 `tools/ci/test_container_control.py:139-145`，现在它断言“缺节点就失败”。改为：有 KGSL 时规则齐全；无 KGSL 时容器照常启动，且不添加 KGSL 规则。测试要说明为什么：Mali 等非高通手机必须能启动桌面。

### 任务 2.2：GPU 环境变量按设备选择
- 修改 `desktop/gpu-env`：只有 `[ -c /dev/kgsl-3d0 ]` 时才导出 KGSL/GL 相关变量；否则保持 `desktop/session:15-16` 的软件默认（`KWIN_COMPOSE=Q`、`QT_QUICK_BACKEND=software`），并确保 `MESA_LOADER_DRIVER_OVERRIDE` 未设置，让 Mesa 选 llvmpipe。
- `desktop/kwin:17` 的 `FD_KGSL_DMABUF_UBWC` 同样只在 KGSL 存在时设置。
- 测试：更新 `tools/tests/test_gpu_env.py`，两种设备各一组断言；同步 `quality/features/apps.yaml:131`。

### 任务 2.3：Mesa 增加 CPU 渲染驱动
- `desktop/mesa-meson-options`：`gallium-drivers` 加 `softpipe,llvmpipe`，`-Dllvm=enabled`。
- `desktop/package-mesa.py:17-24`：依赖加入对应的 libLLVM 运行库包。
- `packages/mesa/debian/changelog` 升版本。
- 原因：Qt Quick 的软件场景图曾让应用抽屉空白（`docs/73:133`），Qt Quick 客户端需要通过 llvmpipe 得到 GL。保持同一个 rootfs 同时支持 Adreno 和 Mali。
- 验收：在 ARM64 构建容器里编出的包含 `swrast`/`llvmpipe`，`EGL_PLATFORM=surfaceless eglinfo` 或 `glxinfo -B`（llvmpipe）可列出 llvmpipe。

### 任务 2.4：应用端默认渲染尺寸与物理尺寸
- `android/app/src/.../MainActivity.java:451`：没有 GPU 路径时，默认短边取 720，减轻 CPU 渲染负担。
- `MainActivity.java:690`：物理尺寸改用 `DisplayMetrics.xdpi/ydpi` 计算，不再写死 151×68 mm。
- 测试：沿用 `android/app/tests` 的 Java 单测模式，覆盖两种默认值。

实现选择与调研（2026-10-04）：`MainActivity` 读取 `/dev/kgsl-3d0` 是否存在，将布尔能力交给纯 Java `DisplayGeometry`；节点与现有 Linux KGSL 路径一致，不按型号/品牌猜测，也不把 `startGpuAllocator` 成功当作有 KGSL（AHardwareBuffer 服务也能在 Mali 启动）。无 KGSL 默认 `min(720, 原生短边)`，有 KGSL 保留原生值，`render_short_edge` 已保存偏好仍优先。此处只判断节点存在，不探测实际 GPU 渲染是否成功；应用 SELinux 上下文能否看到节点、husky CPU 渲染体验仍由 M4 实机核验。

物理尺寸复用 Android 的公开指标而非引入机型表：先用当前 `Display.Mode` 自然方向像素和 `DisplayMetrics.xdpi/ydpi` 计算毫米值，宿主输出再按 `Display.getRotation()` 的 90°/270° 交换轴；显示信息中的毫米值与其自然方向 `physicalWidth/physicalHeight` 保持一致。核对了 AOSP `android16-release` 的 [DisplayContent.computeScreenConfiguration](https://raw.githubusercontent.com/aosp-mirror/platform_frameworks_base/android16-release/services/core/java/com/android/server/wm/DisplayContent.java)、[DisplayInfo.getMetricsWithSize](https://raw.githubusercontent.com/aosp-mirror/platform_frameworks_base/android16-release/core/java/android/view/DisplayInfo.java)（Apache-2.0）：旋转只交换像素宽高，物理 DPI 仍为自然轴，不能直接拿旋转后的 metrics 像素与未交换的 DPI 相除。无效 DPI 返回 Wayland 未知尺寸 0 mm；真实毫米值的准确性仍取决于 OEM 提供的 DPI，离线测试不证明面板测量精度。

### 任务 2.5：验收项按设备能力分级
- `release/acceptance.json` 中的 `contract.gpu-device`、`recording.quicksetting`、`perf.compositor`、`contract.wifi-display` 依赖 KGSL 或高通 WFD。在没有 KGSL 的设备上标为“不适用”并说明原因，不能删掉检查，也不能把它们当成通过。

## M3：构建链（rootfs、APK、独立安装包）

依据：仓库没有“从零构建”的 rootfs 流程。现有 `build_fingerprinted_rootfs.py` 依赖一棵不在仓库里的二进制基础系统树、APT 包池、Alpine LXC 运行环境和 Termux 组件，GitHub 上也没有发布（10 个 fork 都没有 release）。社区 K40S 套件（`yayoinoyume/Rungic` 分支 `dev/munch-cgroup-fix` 的 `munch-build-kit/rootfs-scripts`）证明可以用 `mmdebstrap` 从零搭 Ubuntu 26.04（resolute）arm64；这也是 `docs/75:38` 提出但未实现的方向。

构建机：用户的 Mac Studio（M1 Ultra 20 核、128 GB、Docker 原生 `linux/aarch64`），ARM64 包和 rootfs 原生编译，不用 QEMU。GCP x86 机器只在需要 linux-x86_64 工具时使用（如 NDK），其余时间停机。

| 任务 | 内容 | 产物 | 负责 |
|---|---|---|---|
| 3.1 | ARM64 构建容器：用 `tools/pq/arm64-host.Dockerfile` 在本机 Docker 建镜像；`tools/build_on_device.py` 的构建主机改为可配置，增加本机 Docker 目标（现写死原作者的 Mac mini） | 本机可用的打包环境 | Codex 改代码，Claude 实测 |
| 3.2 | 编 `release/packages.json` 里 15 个重建包，以及 `rungic_package.py` 管理的 20 个项目包 | 本地 APT 仓库 | Claude 执行，失败交 Codex 排查 |
| 3.3 | `bootstrap_rootfs.py` + 原生 ARM64 Docker 入口：核验本地 APT 池/发布清单 → mmdebstrap 安装元包闭包与运行清单 → 准备锁定账户模板 → 核验/保存完整 deb 包锁 → 交给 image/host builder；具体命令见下节 | root 树、完整 deb 池、包锁、bootstrap 报告；容器执行后另产镜像报告 | Codex 工具/离线测试已实现，Claude 待执行容器验收 |
| 3.4 | Alpine LXC 运行环境、静态 `rungic_lxc_enter`/`rungic_plasma_enter`、cast JAR，做 host seed | host seed 与报告 | Codex + Claude |
| 3.5 | Termux 依赖（`termux.apk`、带 PulseAudio 的 prefix）和 `rungic-sparse-write`，全部从官方来源下载并固定哈希 | deps | Claude |
| 3.6 | APK：Rust 宿主（`aarch64-linux-android`）、`libxkbcommon`（可参考社区脚本）、`build-apk.sh`；改掉写死的 NDK 路径和代理 | `rungic.apk` | Codex 改脚本，Claude 构建 |
| 3.7 | 为 husky 生成 `kernel-report.json`（`kernel_banner`、`boot_sha256`、`boot_bytes`），用工具生成，不手写 | 内核报告 | Codex |
| 3.8 | `standalone.py pack` + `verify` | 独立安装包 | Claude |

M3 的每个任务开始前，先把它展开成带验收命令的步骤，写进本文件。

### 任务 3.1：本机 ARM64 Docker 与可配置远程构建端

- [x] 先写失败测试：husky 需要不依赖原作者 Mac mini 的构建入口；覆盖环境/CLI 主机选择、远程地址覆盖、本机二进制传输、原生 ARM64、代理传入、镜像/卷复用及并行度限制；保留原作者的默认地址、10 个任务与手机路径。
- [x] 增加 `--host local-docker`（也可 `RUNGIC_BUILD_HOST=local-docker`），复用 `tools/pq/arm64-host.Dockerfile` 与 `rungic-build` 卷；远程地址用 `--ssh-host USER@HOST` 或 `RUNGIC_BUILD_SSH=USER@HOST`。本机按 Docker VM 的 CPU 与内存取 `min(NCPU, max(1, RAM // 2 GiB))`（Claude 审查时去掉了固定上限 4：Docker 已调到 20 核 64 GB），显式 `--jobs` 只能降低本机上限；Mesa、项目包及 SDK 包均受约束。
- [x] 离线验收命令：`/Users/litaotan/projects/Rungic/.work/venv/bin/python -m pytest -q tools/test_mesa_packaging.py tools/test_build_on_device.py tools/test_feature_inventory.py tools/tests/test_dev_guide.py`，以及同一 Python 的 `tools/pq.py lint`；结果与本机限制见记录。
- [ ] Claude 在可访问 Docker socket 的会话里核验 `hostname`、`uname -m`、`route -n get default`、`scutil --proxy` 与 `docker info --format '{{.Architecture}} {{.NCPU}} {{.MemTotal}}'`；实际准备镜像：`PYTHONPATH=tools python3 -c 'import build_on_device as b; h=b.use("local-docker"); h.ensure(); print(h.image(), h.jobs)'`；随后 `docker exec rungic-build sh -c 'uname -m; . /etc/os-release; echo "$VERSION_CODENAME"; llvm-config-21 --version'`，确认 aarch64、resolute 与 LLVM 21。此步骤会建镜像，本次代码任务不执行。
- [ ] 长构建由 3.2 执行：`python3 tools/build_mesa.py --host local-docker`；包锁与产物另行记录。2.3 的实际 llvmpipe 渲染验收仍需构建后以软件 GL 探针核对，不能用离线配置测试代替。
### 任务 3.3：从零生成 CI2 root 树

- [x] 先写离线失败测试，再实现 `tools/ci/bootstrap_rootfs.py`。输入逐项列在模块 docstring：3.2 的**已实体化** flat APT 仓库（`Packages`、`Release`、rebuilt/project/metapackage `.deb`；`.remote` 记录不够）、对应发布 JSON、`release/packages.json`、运行/排除包清单、精确 Mozilla Firefox `.deb` 版本、已认证 Mozilla 公钥、固定 epoch、Ubuntu mirror、可选完整包锁。Ubuntu 使用 resolute、updates、security 和四个组件；默认 TUNA ports。没有使用旧二进制 root 基线、QEMU 或手机查询。
- [x] mmdebstrap setup hook 在第一次解析依赖前写优先级 1001 的版本 pin、临时排除包负 pin 与既有 dpkg 应用过滤；安装 `rungic-release=版本`、运行清单与 `firefox=版本`，关闭 Recommends，排除项若属于硬依赖则失败。源码依据为 [mmdebstrap 1.5.7 手册](https://manpages.debian.org/trixie/mmdebstrap/mmdebstrap.1.en.html) 的 setup/customize、file-mirror-automount、essential/unlink 与 SOURCE_DATE_EPOCH 接口（MIT）；采用成熟 APT 解析与真实 chroot，不自行做依赖解析。社区 kit 的两个 raw 脚本本轮抓取失败；只保留既有调用经验，不声称核实其账户/配置实现。Ubuntu 包版权由安装后的 `/usr/share/doc/*/copyright` 保存。
- [x] 包外准备仅按已有源码契约：`rungic:1000:1000`、`/home/rungic` 与 Ubuntu skel、root/模板锁定口令（host seed / fresh-account）、`zh_CN.UTF-8`（`desktop/session`）、早期日志目录（`system/init`）、machine-id/SSH key/DNS 清理（image builder）。账户完成标记、共享目录、音频 cookie 与 SELinux 上下文交给 CI3；不添加密码免认证 sudo、linger 或个人桌面设置。其余配置由 release 的项目包提供，SSH socket 自动启用必须通过检查。
- [x] 结果为 Linux volume 中的 `/<run>/root`，同时输出 `packages.lock.tsv`、带每个 `.deb` SHA256 的 `packages.lock.json`、可重新索引/使用的完整 `debs/` 池、`bootstrap-report.json`。安装核验包含 `apt-get check`、非空 `dpkg --audit` 拒绝、项目 clicker venv `pip check`、SSH socket、嵌入 manifest、账户协议 2、模板/属主、排除项。锁/报告也写入 root 的 `/usr/share/rungic/build/`。失败目录保留用于诊断，再跑须选新 run 名，不能覆盖或自动接着半棵树安装。
- [x] 离线验收：`/Users/litaotan/projects/Rungic/.work/venv/bin/python -m pytest -q tools/ci/test_bootstrap_rootfs.py tools/ci/test_rootfs_isolation.py tools/test_feature_inventory.py tools/tests/test_dev_guide.py`；同一 Python 跑 `tools/pq.py lint`；功能归属登记在 `install.rungicos-image`，总览由 `tools/feature_inventory.py render --write` 生成。
- [ ] Claude 在有 Docker socket 的会话先核验本机身份、路由、系统代理、Docker `linux/aarch64`。wrapper 每次读取系统代理，loopback 改为 `host.docker.internal` 并传给 Docker build/run；容器也输出身份、架构、路由和代理。输出用独立 Linux named volume `rungic-rootfs`，仓库/源码/manifest/key 只读挂载，不在 Mac 共享目录上创建客体系统树。Dockerfile 单独提供小型 bootstrap/打包环境，固定 base digest 与现有 ARM64 host 一致，不安装庞大的开发主机包集。

执行命令（先把 `RUNGIC_OS_RELEASE`、`RUNGIC_FIREFOX_VERSION` 设为 3.2 **实际产物**的版本；Mozilla key 默认复用 Git 中 `system/config/etc/apt/keyrings/packages.mozilla.org.asc`，由 config 包安装，工具核验已记录指纹 `35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3` 和安装后字节，不另下载密钥）：

```sh
python3 tools/ci/bootstrap_rootfs_docker.py \
  --repo .work/apt/repo \
  --release ".work/apt/releases/${RUNGIC_OS_RELEASE}.json" \
  --firefox-version "$RUNGIC_FIREFOX_VERSION" \
  --source-date-epoch "$(git show -s --format=%ct HEAD)" \
  --output-name "husky-${RUNGIC_OS_RELEASE}"
```

加 `--print-only` 只检查输入并打印展开后的**完整 Docker build/run 命令**，不接触 Docker；实际执行则先验证 Docker server 架构。默认 mirror 为 `http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports`，可用 `--mirror` 改为保留的 Ubuntu snapshot。bootstrap 完成后，同一 volume 的树直接交给 image builder（特权容器已是 Linux root，此处使用 `--inside`，不在其中嵌套启动 Podman）：

```sh
docker run --rm --platform linux/arm64 --privileged \
  --mount "type=bind,src=$PWD,dst=/src,readonly" \
  --mount "type=bind,src=$PWD/.work/apt/releases/${RUNGIC_OS_RELEASE}.json,dst=/inputs/release.json,readonly" \
  --mount type=volume,src=rungic-rootfs,dst=/output \
  rungic-rootfs-bootstrap:26.04 \
  python3 /src/tools/ci/build_rootfs_image.py --inside \
  --root "/output/husky-${RUNGIC_OS_RELEASE}/root" \
  --release /inputs/release.json \
  --output "/output/husky-${RUNGIC_OS_RELEASE}/image/rootfs.img" \
  --size-gib 16 --firefox-version "$RUNGIC_FIREFOX_VERSION"
```

- [ ] 核对 bootstrap 报告、`image/rootfs-report.json`、包锁和 `e2fsck`，3.4 的 `build_host_seed.py --inside --rootfs-tree /output/husky-<release>/root …` 消费同一棵树；不通过 Mac tar 解包再包装以免改变属主/属性。16 GiB 为本次候选参数，仍需 M4 实机核对容量与首装。
- [ ] 按锁重建验收：在同一 Linux volume 中，用上一 run 的 `debs/`（已经有 `Packages` / `Release`）作为 `--repo`，上一 run 的 `packages.lock.json` 作为 `--package-lock`，同一 manifest/key/epoch/mirror 与新 `--output` 直接调用 `bootstrap_rootfs.py`；按上面的 Docker mounts 加载 `/output` 即可，无需把大包池复制到 Mac。比较两个 `packages.lock.json`（应相同）与 bootstrap 报告里的包锁摘要；改变一个 deb 或锁中的版本应拒绝。滚动 archive 首次解析不承诺闭包不变，重放需要保留 deb 池与兼容的 Ubuntu archive/snapshot；epoch/包字节与配置可锁定，维护脚本和 ext4 逐字节复现尚未验收。

```sh
docker run --rm --platform linux/arm64 --privileged \
  --mount "type=bind,src=$PWD,dst=/src,readonly" \
  --mount "type=bind,src=$PWD/.work/apt/releases/${RUNGIC_OS_RELEASE}.json,dst=/inputs/release.json,readonly" \
  --mount type=volume,src=rungic-rootfs,dst=/output \
  rungic-rootfs-bootstrap:26.04 \
  python3 /src/tools/ci/bootstrap_rootfs.py \
  --repo "/output/husky-${RUNGIC_OS_RELEASE}/debs" \
  --release /inputs/release.json \
  --package-lock "/output/husky-${RUNGIC_OS_RELEASE}/packages.lock.json" \
  --firefox-version "$RUNGIC_FIREFOX_VERSION" \
  --source-date-epoch "$(git show -s --format=%ct HEAD)" \
  --output "/output/husky-${RUNGIC_OS_RELEASE}-replay"
docker run --rm --mount type=volume,src=rungic-rootfs,dst=/output,readonly \
  rungic-rootfs-bootstrap:26.04 cmp \
  "/output/husky-${RUNGIC_OS_RELEASE}/packages.lock.json" \
  "/output/husky-${RUNGIC_OS_RELEASE}-replay/packages.lock.json"
```

重放容器的联网同样需带本轮系统代理对应的 `--env http_proxy=… --env https_proxy=…`（wrapper 已打印具体值），或者使用可直接访问的 archive snapshot；不能默认沿用历史代理或直连。

**明确未决问题：** Git 没有旧 baseline 树，不能恢复其中未记录的手工默认或额外应用。源码可推出的配置已实现，其余不猜测。本 release 的干净依赖闭包能否通过 clicker `pip check`、新账户准备、Plasma/llvmpipe 与既有硬件路径，需要真实容器及 CI3/M4 验收；容器 hook、权限/xattr、完整 package lock、image 与 host seed 的真实输出也尚待执行。离线替身测试不替代这些结果。

### 任务 3.7：从 boot 生成内核报告

- 先写 `tools/ci/test_kernel_report.py` 的合成 v4 boot 测试并运行，确认失败；覆盖 gzip、LZ4 legacy、未压缩 Image、全文件摘要、错误头/截断/缺版本串及可选 Image 不匹配。
- 实现 `tools/ci/kernel_report.py BOOT --output REPORT [--image Image]`；只从 header 声明的 kernel 区域解压提取 `Linux version`，不信 cmdline、ramdisk 或 AVB 尾部的字符串。复用 Python gzip 与上游 `lz4` CLI（LZ4 legacy 输入需安装 lz4），避免自行维护解码器。可选 Image 必须逐字节等于 boot 解压内容，不作为替代来源。
- 验收：指定 venv 执行 `python -m pytest -q tools/ci/test_kernel_report.py tools/ci/test_standalone.py tools/test_run_tests.py tools/test_feature_inventory.py tools/tests/test_dev_guide.py`、`python tools/pq.py lint`；Java 尺寸测试按 `tools/run-tests.sh` 的 javac/java 入口运行，并执行 `sh -n tools/run-tests.sh`。
- 真实输入只读核验：`python tools/ci/kernel_report.py /Users/litaotan/projects/Rungic/.work/husky/boot-lxc.img --output .work/husky/kernel-report.json`。报告放本工作树 `.work/`，供后续 3.8 组包使用；生成报告不代表 ABI、签名、实机首装或 Plasma 验收通过。

格式核验来源：[AOSP boot_img_hdr_v4](https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/main/include/bootimg/bootimg.h)（BSD-3-Clause，源码 blob `67ae349583e66424e5af0a3b7a386b42b6c958e0`；1584 字节头、4096 字节页、kernel/ramdisk/signature 对齐布局）与 [LZ4 v1.10.0 legacy 格式](https://github.com/lz4/lz4/blob/v1.10.0/doc/lz4_Frame_format.md#legacy-frame)。本机 `lz4 --version` 为 1.10.0；直接调用上游 CLI（[GPL-2.0-or-later](https://github.com/lz4/lz4/blob/v1.10.0/programs/COPYING)）解码而不复制库源码，省去维护 LZ4 匹配/块边界解码器的成本。报告只提供 `standalone.py` 实际读取的三个必需字段及 schema/压缩方式/解压 Image 摘要，不伪造 ABI 或模块信任结果。只支持 v4；其他 boot 版本及未支持的压缩格式显式拒绝。

## M4：实机安装与验收

- `standalone.py install` 到 husky（不清数据）；之后按 SKILL 的验收顺序逐项核对：base ready → payload verified → install/mount → release ready → account-prepare → account form → desktop loading → Plasma。
- 终点：Plasma 桌面出现，触摸可操作，`tools/rungic_acceptance.py` smoke 级通过，并截图存档。
- 已知会降级的功能（QPainter 下）：截屏/录屏、部分 KWin QML 特效。记录下来，不算作失败。

### 任务 3.8 准备：Mac 实际构建配方与 build plan（2026-10-04）

- [x] 新增 `tools/ci/husky_build_plan.py`：生成四份 `build_artifact` 配方，逐个执行并验证，在全部记录通过 `standalone.collect_build_manifest` 后才发布计划。`components` 每项为 `{recipe, directory}`，`bindings` 严格为 `rootfs.img.gz`、`host-seed.tar.gz`、`rungic.apk`、`rungic-sparse-write`。
- [x] 配方沿用本机已用命令：NDK 28.2 的 Android API 31 静态稀疏写入器；stable ARM64 Rust 的 `build-native-core.sh` 后接 Homebrew Bash 的 `build-apk.sh`（OCR none、开发签名）；Linux Docker 的 mmdebstrap → `build_rootfs_image.py --inside`；`build_enter.sh` 和投屏构建脚本后接 `build_host_seed.py --inside`。`RUNGIC_ENTER_OUT` 新增可选输出目录，其余 JNI/Cargo/APK/cast 输出用已有覆盖变量，全部新产物在执行器的 pending 工作目录生成，成功后发布内容寻址记录。没有导入今天的成品作为新的源码构建命中。
- [x] 输入策略写在模块 docstring：源码/固定上游配方/补丁按内容、模式、链接和 xattr；NDK、Rust、JDK、SDK 工具按安装内容；libxkbcommon 1.13.1 的已编译 `.so` 以生成配方时的 SHA256 固定，另记录 Meson cross file，属于二进制输入，不补造其历史编译证明；Alpine 3.22.6/LXC 6.0.4-r0 runtime 固定为 `1401a42399462cef33db09b7ce47542c35aaa747faeb185bd0309ca3a541bba4`；本地 APT 仓库同样作为二进制输入。Docker bootstrap 与 pq 工具镜像记录现场 inspect 的完整 `sha256:` image ID，rootfs/host 用该 ID 运行，pq 标签在 native 准备前后核对。
- [x] rootfs 消费 volume 中今天保留的 `/output/husky-20261004.1/debs` 和完整包锁，按 3.3 的重放入口重新 bootstrap；不是缺失的旧作者 binary baseline。Linux 内按 `ownership=True` 核对 deb 池与锁的输入身份，构建前后都核验。host 消费这次 image builder 新生成的 `image/root-tree`（已有排除与身份清理步骤），将树的 Linux 属主/内容身份和位置作为 rootfs 记录输出，再通过带 `build.json` 的依赖绑定；host 使用前后及计划发布前重新核验树。完整树与 runtime 始终在 Linux volume 中处理，仅配方命令把新压缩镜像、seed 和报告导出到 Mac 记录目录。失败工作目录保留，不覆盖原来的 `husky-20261004.1`，不清理其他任务。
- [ ] Claude 在有 Docker socket 的会话运行下列命令。工具现场核验主机、路由、系统代理与 Docker ARM64；loopback 代理转为 `host.docker.internal`，传入 Docker。现有 `rungic-rootfs-bootstrap:26.04` 与 `rungic-pq:26.04` 必须在手，3.3 的完整 deb 池/锁必须保留；按锁重放仍需要可访问的兼容 archive。不会重建 Docker 工具镜像、访问 adb 或自动安装手机。

在**本工作树**运行，二进制输入读主仓库的 `.work/`，源码与未提交修改读本工作树：

```sh
cd /Users/litaotan/projects/Rungic-wt-m3-plan
/Users/litaotan/projects/Rungic/.work/venv/bin/python tools/ci/husky_build_plan.py \
  --release 20261004.1 \
  --firefox-version '156.0.1~build1' \
  --source-date-epoch "$(git show -s --format=%ct HEAD)" \
  --input-root /Users/litaotan/projects/Rungic \
  --output .work/husky/build-plan-20261004.1
```

该 epoch 是本次显式构建参数，复用时保持相同；若要保持第一次 bootstrap 的 epoch，以其 `bootstrap-report.json` 的 `source_date_epoch` 为准替换。合入主仓库后在主仓库执行同一命令时，去掉 `--input-root` 即可。输出目录必须不存在；重试用新的 `--output` 名，计划目录位置不参与组件输入键，同输入可复用已验证的内容寻址记录。volume 中的关联树须保留；缺失或内容/属主变化会拒绝发布计划，即使 Mac 文件缓存尚在。

完成后工具打印四个实际记录目录中的产物路径。`standalone.py pack` 使用打印出的 **新产物及同目录报告/包锁**，传 `--build-plan .work/husky/build-plan-20261004.1/build-plan.json`；`--deps` 中的 `rungic-sparse-write` 必须来自新 sparse 记录，Termux 输入、spec、kernel report 和 package release 仍按 3.8 的既有核验准备。不要把今天旧成品路径与新计划混用。pack/verify 与 M4 实机验收仍待 Claude 执行；本工具不代表已完成组包、安装或桌面验收。

## M5：GPU 加速（2026-10-04 用户要求两条路线并行）

**现状（实测）：** 软件路径下，plasmashell 滑动时占 387–467% CPU；Qt 场景图 `render≈1 ms`，但 `swap≈55 ms`（llvmpipe 在 swap 时光栅化 720×1602 并拷进 shm），帧间隔中位 60 ms（≈16 fps）。降到 60 Hz、关壁纸模糊与动画、`LP_NUM_THREADS=4` 都无改善（4 线程更慢：swap 70 ms）。渲染短边 540 时 swap 中位 20 ms、帧间隔中位 26 ms（≈38 fps）。目前手机临时设为 540，待用户决定默认值。

**路线 A：Linux 直接驱动 Mali（panthor + Mesa panfrost/panvk）**
- 先做可行性研究，不写设备：Mali-G715 的架构代号与 Mesa/panthor 支持状态；panthor 能否回移到 android14-6.1 并作为模块加载；CSF 固件；Pixel 的 GPU 电源域、时钟与 DVFS；**与 Android 自己使用的 kbase 驱动能否共存**（Android 的 SurfaceFlinger/HWUI 依赖 kbase，若必须二选一则此路线会破坏 Android 图形）。
- 产出：带证据的可行性结论、工作量估计、若可行则给出原型步骤。

**路线 B：把 GL 调用转发给 Android GPU（virglrenderer vtest，社区 Termux 方案已验证）**
- Android 侧运行用 Android EGL/GLES 构建的 virglrenderer（vtest 服务），socket 经 `/mnt/android-wayland` 进入容器；容器内 Mesa 用 virgl（`GALLIUM_DRIVER=virpipe` / vtest）渲染，结果仍以 shm 交给 KWin。
- 先手工原型并用同一套 `QSG_RENDER_TIMING` 方法测量（swap、帧间隔），与 llvmpipe 540/720 对比；有明显收益再正式集成（Mesa 打开 virgl、宿主或 App 启动 vtest 服务、gpu-env 按能力选择、验收）。

**协调规则：**
- 手机同一时间只由路线 B 写操作（安装、重启会话、推文件）；路线 A 只读。写设备前确认 Rungic 桌面仍可恢复：每个实验结束恢复原状（`systemctl --user unset-environment …`、重启 plasmashell）。
- 不刷内核、不清数据，除非 Claude 明确批准；不改用户的 Android 设置（键盘、Play 保护等）。
- **禁止从容器（或容器 PID 命名空间内的任何进程）打开 `/dev/mali0`，也不得给容器加它的设备规则或挂载。** 路线 A 反汇编本机 `mali_kbase.ko`（r54p2）确认：`gpu_dvfs_kctx_init()` 用全局 tgid 在调用者 PID ns 中 `find_get_pid`，`get_pid_task` 结果未判空即解引用；本机 `panic_on_oops=1`，首次从容器创建 kbase 上下文极可能 oops 并重启。修复前只能在 Android init PID ns 中访问 GPU。
- 产物进 `.work/husky/m5-*`；代码改动在各自分支，Claude 审查后合并；Codex 做设计评审与代码审查。

## 记录

- 2026-10-05：**B3 状态契约确认与最终补测：** 用户确认 `files/tmp/vtest.state`（容器内 `/mnt/android-wayland/vtest.state`）由 App supervisor（B2）写单行 running/failed，可缺失；只接受准确的 `running` 或 `running\n`。先补失败测试，发现 shell 命令替换会吞多余换行及 NUL，已改为 `cmp` 字节比较（config 包显式依赖 diffutils），读取失败、空文件、其他行、额外空行/空白和 NUL 均拒绝。增加 7 个状态用例后，指定主仓库 venv 的最终集合（GPU、Mesa 打包、功能清单、dev guide）为 **52 passed、15 subtests passed（20.43 秒）**；pq lint、六个 shell 的 sh -n、git diff --check 再验退出 0，总览已重生成。仅状态格式已确认；真实 systemd/手机恢复、App 失效通知与配套 Mesa 修复仍待集成，全部改动未提交。

- 2026-10-05：**B3 最终离线核验：** 指定主仓库 venv 的 `python -m pytest -q tools/tests/test_gpu_env.py tools/test_mesa_packaging.py tools/test_feature_inventory.py tools/tests/test_dev_guide.py` 为 **45 passed、15 subtests passed（17.97 秒）**；同一 Python 的 `tools/pq.py lint`、六个改动 shell 文件的 `sh -n`（probe、refresh、gpu-env、session、kwin、config build.sh）、`git diff --check` 均退出 0。`feature_inventory.py render --write` 已执行，`check --strict` 为 161 项功能、0 errors、44 项既有 device-only 警告，欠账不增。下条扩大登录环境测试的 Mac 既有失败仍保留，未改与 B3 无关的测试。全部 16 个文件保持未提交；没有 git commit、构建正式包、部署或实机操作。

- 2026-10-05：Codex 在 `task/m5-b3-session` 完成 **M5 路线 B / B3 的 Linux 能力选择与崩溃恢复代码（未提交、未部署）**。先读本文 M5/记录及主仓库 `.work/husky/m5-B/report.md`、`design.md`（本工作树没有该目录），新增失败测试，确认旧实现残留 virpipe、未同步用户管理器且缺探测/恢复文件后实现。`desktop/virgl-probe` 要求 `/mnt/android-wayland/vtest.state` 为单行 `running`、`vtest.sock` 为 Unix socket，再以 `EGL_PLATFORM=surfaceless GALLIUM_DRIVER=virpipe VTEST_SOCKET_NAME=… eglinfo -B` 核验成功退出且 renderer 为 virgl；超时 2 秒、再给 TERM 1 秒后强杀，不留 probe core。`gpu-env` 无 KGSL 才探测，失败保持原 llvmpipe/OpenGL、清除 GALLIUM_DRIVER/VTEST_SOCKET_NAME；Qualcomm 保留 freedreno，KWin 启动器清除 virpipe、husky 继续 QPainter/SHM。session 在 import 前清除管理器旧变量，登录 profile 不能重新覆盖这两项。新增 `plasmashell-gpu-refresh` / `plasmashell-gpu.conf`，每次 ExecStartPre 重选 GPU 并严格更新用户管理器；[Plasma/6.6 上游单元](https://raw.githubusercontent.com/KDE/plasma-workspace/Plasma/6.6/shell/plasma-plasmashell.service.in) 为 on-failure、3 次/60 秒，本 drop-in 取消此单元启动限流、重试间隔 5 秒，保留 Type=dbus、bus name、原 ExecStart/会话生命周期；依据 [systemd v259 的逐进程环境合成规则](https://raw.githubusercontent.com/systemd/systemd/v259/man/systemd.exec.xml) 让 ExecStart 获得刷新后的环境。由 `rungic-plasma-config` 安装三个新文件并显式依赖 mesa-utils/coreutils/diffutils；临时 DESTDIR 执行真实 recipe 验证内容和权限。新文件与 E6 已在 apps.gpu / desktop.session 认领，总览重生成；30/31 篇同步了接口与验收边界。测试覆盖成功、failed/缺状态、缺 socket/普通文件、非 virgl、非零退出、真实超时（忽略 TERM）、缺 helper、KGSL 不探测、KWin 不继承 virpipe、profile 不绕过探测，以及管理器 running→failed→running→failed 重选。扩大测试集合为 **51 passed、15 subtests passed、1 failed**：唯一失败为原有 `test_login_environment.py::test_output_evaluates_in_the_session_shell`，macOS 默认账户 shell 不读取该测试创建的 .profile，HEAD 原版 helper 同样复现；B3 新 profile 测试通过。最终集合与 shell/pq 检查结果见核验补记；随后用户确认状态格式，严格解析与新增测试见最新补记。现场 Mac Studio / arm64，系统代理为空、路由查询被沙箱拒绝；Unix bind 被沙箱拒绝，离线测试在此环境用目录替代 socket 存在性谓词，另保留生产 `-S` 对普通文件的反向测试，开放环境会直接使用真实 socket。没有访问手机、没有更改设备规则/挂载，容器绝不新增 `/dev/mali0`。**待集成/实机：** 状态格式已由用户确认：Android supervisor（B2）写单行 running/failed、允许缺失；合入配套 virgl/事务锁/断连修复后核验真实 EGL 输出与退出码、用户 manager 的 ExecStartPre→ExecStart 环境、杀服务/子进程后的 shell 画面与资源释放。启动前探测无法救回仍存活但挂起的 shell，App 失效通知尚需 supervisor 对接；持久非 GPU 错误也会每 5 秒重试，须从 journal 定位。回退是通过开发覆盖 reset 或回到旧正式包，移除本 drop-in 后恢复上游策略；本轮未执行部署或回退。

- 2026-10-05：**路线 A 研究完成**（`.work/husky/m5-A/report.md`）。G715 = Valhall arch 11.8；panthor 自 Linux 6.18、Mesa panfrost/panvk 自 main 2026-08-27（MR 41803，未发布）才支持。**A1 panthor 不可行**：与 Android 的 kbase 不能共存（解绑 kbase 会毁掉 SurfaceFlinger/HWUI），另需回移 drm_exec/drm_gpuvm/drm_sched 到 6.1、无上游 zuma 平台支持。**A2 容器内 Mesa 直接用 kbase UAPI**（funnymdzz/mesa 的 PanVK kbase 后端，同 UK 1.38，已在 Pixel 7 跑 Vulkan）可行但工作量大：原型 2–3 周、产品化 6–10 人周；需移植到 v11、补 Wayland 呈现，并先修复本机 kbase 的 PID 命名空间空指针崩溃（Claude 核对了反汇编证据）。Codex 独立评审（`.work/husky/m5-codex-consult.txt`）建议先把路线 B（virgl）产品化，A2 并行研究。
- 2026-10-04：**M4 达成：Plasma 桌面在 husky 上显示并可触控。** 实机安装中修复：`standalone.py install` 未推送 schema 2 的 `build-manifest.json`（`d2b1b65`）；App 写死 `/product/bin/su`，Pixel 上 Magisk 的 su 在 `/system_ext/bin`（`0104de5`）；容器 `/dev/shm` 标签未生效：Ubuntu libmount 在看不到 selinuxfs 时静默删除 `context=`，且 Pixel 策略拒绝 `appdomain_tmpfs` 的 filesystem associate，改为 `android-shm-mount` 直接 mount(2) 并校验、`rootfs.sepolicy.rule` 加规则（`145261c`）；无 KGSL 时 Qt Quick 软件场景图让 Dock 与应用抽屉空白，改走 llvmpipe（`734cb33`）；首装时桌面会话未进大核 cgroup，首帧后再请求 boost（`e1e2e8d`）；`display.geometry` 接受 App 选择的渲染尺寸（`d3f74ab`）；`input.text` 适配中文会话（`ed9ac6b`）。发布 `20261004.2` 经 `rungic_release.py deploy --acceptance none` 正式部署（带验收的部署因 `input.text` 回滚，见下），随后 smoke 19 项中 18 项通过；`input.text` 失败原因为 Gboard 拼音模式吞掉注入的拉丁字母，手动用输入法提交“cal”后文字确实进入 plasmashell 搜索框。用户在手机上完成账户创建（用户名 linux）并登录 Codex。
- 2026-10-04：**3.3–3.6 产物在本机全部做出。** rootfs：`bootstrap_rootfs_docker.py` 从零构建，报告中的 apt-get check、dpkg --audit、pip check、ssh.socket、新账户、home 布局、预装排除全部通过；`build_rootfs_image.py --inside` 打成 16 GiB ext4，1,491 个包，压缩后 1.75 GB（`afa92dc0…`），e2fsck 0。host seed：Alpine 3.22.6 minirootfs（SHA-256 与官方 release 元数据一致）+ `apk add lxc`（6.0.4-r0，与 `docs/17` 相同），静态 `rungic-lxc-enter`/`rungic-plasma-enter`（NDK 28.2 darwin），cast JAR，335 MB（`836d7259…`）。APK：libxkbcommon 1.13.1 取自 Ubuntu 源码，用 NDK 交叉编译；Rust 宿主 2 分 32 秒编完；`build-apk.sh` 需用 Homebrew bash 5（系统 bash 3.2 没有 `mapfile`），`RUNGIC_APK_OCR=none`；修了两个脚本缺陷：NDK 路径只认 linux-x86_64、llvm-ar 回落到原作者路径（`56cf74e`），以及 APK 缺 `libc++_shared.so`（`d63923d`，即 `docs/79` 的启动崩溃）。`rungic-sparse-write` 用 NDK 静态编译。内核报告由 `kernel_report.py` 生成，`image_sha256` 与 1.4 的 `Image.trusted` 一致。Termux APK v0.118.3 与官方 sha256sums 一致。未完成：`termux-prefix.tar.gz`（手机未解锁，Termux 无法初始化）；`standalone.py pack` 要求 build_artifact 构建记录（`--build-plan`），仓库没有现成 recipe，交 Codex 编写。
- 2026-10-04：Codex 在 `task/m3-build-plan` 完成 **3.8 构建计划准备与离线验证**，全部保留未提交。已完整阅读 `build_artifact.py`、两份 `build_fingerprinted_*`、docs/94、108 和相关脚本，使用项目三段式 skill 的构建隔离约定；先写测试，缺模块时确认失败，再实现实际 Mac 配方、执行器调用与四项绑定。扩展测试实际调用 `build_artifact.inputs/execute/verify` 和 `standalone.collect_build_manifest`，用生成配方配合假二进制/工具记录核验计划形状、root→host 依赖、输入变更拒绝、位置无关缓存键和新产物位于内容寻址目录；Docker/SDK 操作使用替身。进一步发现本机 Python 不提供 `os.listxattr/getxattr`，原执行器在 Mac 上无法哈希文件，已用 Darwin libc 的 no-follow 接口补齐，真实 xattr 改动使指纹变化，悬空链接仍可读取。实际宿主 C 编译测试又发现隔离环境丢掉 TMPDIR 后 clang 在沙箱 `/tmp` 创建临时文件失败，现将临时目录放在 pending 记录内部；宿主 C 编译与第二次复用通过。这两处修正保持 Linux xattr 路径及原输入/产物验证规则。新文件认领在 `install.rungicos-image`，新增 E7 与测试 covers，更新 `delivery.build-fingerprint/E2` 并重生成总览。指定主仓库 venv 的新测试、`tools/ci/test_build_manifest.py`、`tools/tests/test_build_artifact.py`、`tools/test_feature_inventory.py`、`tools/tests/test_dev_guide.py` 为 **50 passed、15 subtests passed**；`tools/pq.py lint`、`sh -n tools/build_enter.sh`、`git diff --check` 通过。现场为 `LitaodeMac-Studio.local` / arm64，系统代理为空，路由查询被沙箱拒绝。按本轮限制未访问 Docker/adb、未生成真正的 husky payload、未 git commit；容器重放/新产物、pack/verify 和 M4 均仍待验收。Claude 的精确运行命令和输入/缓存保留要求见上节。

- 2026-10-04：**3.2 完成。** 本机 Docker 编出全部 19 个 rebuilt 包（含 Mesa），每个 40–165 秒，KWin 165 秒，全部成功并收进 `.work/apt/repo`；19 个项目包全部构建，版本 `0.727`/`0.728`。踩到的问题：(1) `build_mesa.py`、`pq source` 需要本机先有 `rungic-pq:26.04` 镜像（`docker build -t rungic-pq:26.04 -f tools/pq/Dockerfile tools/pq`）；(2) host 类项目包在本机执行 `build.sh`，依赖 GNU `install -D` 和 `dpkg-deb`，macOS 需 `brew install coreutils gnu-sed findutils gnu-tar dpkg`，并把这些 gnubin 目录放到 PATH 最前；(3) 2.3 打开 `-Dllvm` 后 `rungic-flatpak-gl` 在 Freedesktop SDK 镜像里找不到 llvm-config，已在其 `build.sh` 中单独覆盖为 `-Dllvm=disabled` 并补测试（`dc9a361`）。`rungic_release.py build` 默认会到原作者的手机读取 coupled 包版本；改用 `--coupled-json`，取编译镜像中 Ubuntu 当前版本（`plasma-workspace 4:6.6.6-0ubuntu0.1`、`libplasma7`/`libplasmaquick7 6.6.6-0ubuntu0.1`；rebuilt 包对它们只有 `>=` 约束），生成发布 `20261004.1`（72 个精确依赖）。Firefox `156.0.1~build1` arm64（`docs/40` 验证过的版本）来自 Mozilla 官方 APT：用仓库中的公钥（指纹 `35BAA0B3…`）验证 `InRelease` 签名（GOODSIG/VALIDSIG），再核对 `Packages` 和 deb 的 SHA-256（`c5cc4755…`），之后 `rungic_release.py import` 进包池。
- 2026-10-04：**2.3 实机构建验收通过。** 本机 Docker（`--host local-docker`）编出 Mesa 1,669 个目标并打包。首次安装失败：`mesa-libgallium` 带了 `gbm/dri_gbm.so`，而 Ubuntu resolute 当前的 `libgbm1`（26.0.8-1ubuntu0.3）也拥有它，dpkg 拒绝覆盖。改为与 Ubuntu 一致放进 `libgbm1`（先写失败测试），升到 `+rungic5`。干净容器中 5 个包全部安装成功；`EGL_PLATFORM=surfaceless eglinfo -B` 显示渲染器 `llvmpipe (LLVM 21.1.8, 128 bits)`，OpenGL 4.6 Core；`kgsl_dri.so`、`zink_dri.so` 仍在。另：本机需先建 `rungic-pq:26.04`（`docker build -t rungic-pq:26.04 -f tools/pq/Dockerfile tools/pq`），`build_mesa.py` 不会自动构建它。
- 2026-10-04：Codex 在 `task/m3-rootfs-bootstrap` 实现 **3.3 工具与离线验证**，按要求全部保留未提交。先写测试，缺少 bootstrap 模块时确认收集失败；随后加入原生 ARM64 Linux 的 mmdebstrap 从零 root 树入口、独立固定 base Dockerfile 与薄 wrapper。输入先核验实体化 APT pool、索引/deb 摘要、release/packages.json 三类选择、精确发布/Firefox 版本及排除项；setup hook 先写 1001 pin，customize 核验嵌入 manifest、协议 2、锁定 UID/GID1000 模板、home、安装闭包、SSH socket 与 pip。保留每个 deb 并生成可用于重放的完整 APT 池、版本/架构/内容锁和报告；拒绝覆盖及重放中的集合/字节变化。包外配置仅复用源码证明的账户、locale、日志与身份清理；Mozilla 公钥由现有 config 包提供，手机代理仍由 CI3 firstboot 写入。新文件已登记 `install.rungicos-image`，总览重生成。指定 venv 的新测试、`test_rootfs_isolation.py`、`test_feature_inventory.py`、`test_dev_guide.py` 合计 **57 passed、20 subtests passed**；`tools/pq.py lint` 与 `git diff --check` 退出 0。现场为 `LitaodeMac-Studio.local` / arm64，系统代理为空，路由查询被沙箱拒绝；按本次限制没有访问 Docker、生成 rootfs/image、安装软件或访问手机。未记录的旧 baseline 默认无法从 Git 还原，干净闭包 pip/账户/Plasma 与 hook、文件属性、image/host 输出仍待容器及 CI3/M4 验收；详情、完整输入及运行/重放/打包命令见 3.3 和模块 docstring。

- 2026-10-04：Codex 在 `task/m2-container` 完成 Tasks 2.1、2.2、2.5 的仓库改动，全部保留未提交。先读 M2/M3、全局约束及 30/31、40、61 篇，并核对现有 KWin Android 后端补丁、[LXC 可选挂载文档](https://linuxcontainers.org/lxc/manpages/man5/lxc.container.conf.5.html)与 [Mesa 驱动覆盖文档](https://docs.mesa3d.org/envvars.html)，复用现有 QPainter/SHM 路径；未引入新的上游代码或依赖。按 TDD 先确认新增测试失败，再实现：KGSL 与 DMA heap 独立按字符设备存在性生成规则，缺失只记日志，挂载可选；覆盖 husky「有 DMA heap、无 KGSL」、两项独立缺失、非字符节点及 Qualcomm 当前设备号/错误保留。无 KGSL 时保留 `KWIN_COMPOSE=Q`、`QT_QUICK_BACKEND=software`，清除旧 Adreno 环境和用户管理器残留；UBWC 仅供 KGSL，Qualcomm 的 GL/Turnip/Flatpak 设置保持原行为。四项指定验收从 Android root 只读判断 KGSL，缺失时写 `status="not applicable"`、`passed=null`、原因与 `not_applicable_ids`，不运行相应检查、不贡献指标或通过数；设备状态读不懂仍失败，Qualcomm 容器漏挂设备仍属契约失败。功能清单、生成总览及 30/31 篇已同步；没有新增文件。指定 venv 的 `python -m pytest -q tools/ci/test_container_control.py tools/tests/test_gpu_env.py tools/tests/test_acceptance_scenarios.py tools/tests/test_acceptance_restore.py tools/test_feature_inventory.py tools/tests/test_dev_guide.py` 为 **52 passed、19 subtests passed（20.06 秒）**；`python tools/pq.py lint`、四个修改脚本逐一 `sh -n`、`git diff --check` 均退出 0，清单严格检查为 161 项功能、0 errors，44 项既有 device-only 欠账未增加。本轮没有构建、部署或访问手机；Mesa llvmpipe 包由 Task 2.3 完成，真实桌面/触摸仍待 M4 验收。
- 2026-10-04：Codex 在 `task/m2-mesa-builder` 完成 **2.3、3.1 的代码与离线验证**，全部保留未提交。先写失败测试，再加入 softpipe/llvmpipe、启用 LLVM，保留 freedreno/zink、KGSL 与 Turnip；Mesa 版本升至 `26.3.0~devel20260824+rungic4`。复用原固定 lfdevs 源码（许可证仍由 `packages/mesa/recipe.json` 与上游 `docs/license.rst` 记录），CPU 驱动依据 [Mesa 官方 llvmpipe 文档](https://docs.mesa3d.org/drivers/llvmpipe.html)；运行依赖 `libllvm21 (>= 1:21.1.0)` 来自 [Ubuntu resolute mesa-libgallium 元数据](https://packages.ubuntu.com/fr/resolute/mesa-libgallium)，对应 [源码包的 llvm-21-dev](https://packages.ubuntu.com/source/resolute/mesa)，不是猜测包名。构建器加入 `RUNGIC_BUILD_SSH` / `--ssh-host` 和原生 `local-docker`，复用原 Dockerfile/卷及系统代理逻辑；按 Docker VM CPU、每 2 GiB 一个任务、最多 4 个任务限制并行度，项目包和 SDK 包同样受限，原作者默认 Mac mini 与手机路径保留。另修复已有 Mesa 构建目录未重新配置的问题，确保旧 `llvm=disabled` 缓存也接收新选项。新测试登记于 `apps.gpu` / `delivery.build-hosts`，功能总览已生成；测试说明 husky 的 CPU GL 需求并覆盖 Qualcomm/Adreno 路径。指定 venv 的要求测试集合为 **42 passed、45 subtests passed、1 failed**：唯一失败为原有真实 Ninja 增量子测试，本机缺 `ninja`，在 `HEAD` 原版中同样复现；macOS make 3.81 的秒级时间戳测试抖动已改为明确设置旧产物时间。排除此旧测试并加入 `tools/test_system_test.py` 为 **44 passed、44 subtests passed、1 deselected**；扩展 `tools/test_rungic_package.py` 为 4 passed、4 failed，四个 GNU install/stat、ELF 链接与沙箱优先级相关失败在 `HEAD` 原版中相同复现。`tools/pq.py lint`、`sh -n tools/pq/rungic-transfer`、生成的 Mesa preinst 语法检查及 `git diff --check` 均退出 0。现场主机为 `LitaodeMac-Studio.local` / arm64，`scutil --proxy` 为空；路由与 Docker socket 查询被沙箱拒绝。因此本轮没有创建镜像、运行长构建、访问手机或验证实际 llvmpipe 输出，镜像与渲染验收仍待 Claude 执行；日志与原版对照放 `.work/husky/task-m2-mesa-builder/`。

- 2026-10-04：Codex 在 `task/m2-app-kernelreport` 完成 Task 2.4 与 3.7 的代码及离线验证，全部保持未提交。先写失败测试（Java 缺 helper、Python 缺报告模块、统一入口漏跑新 Java 测试均确认失败），再实现：无 KGSL 默认短边 720、有 KGSL 保留原生值，保存偏好优先；物理尺寸用真实显示模式像素与 xdpi/ydpi 换算，按显示旋转交换宿主毫米轴，未知 DPI 为 0 mm；新增 v4 boot 报告工具，支持 gzip/LZ4 legacy/raw Image，可选 `--image` 必须与解压内核逐字节一致。新文件已在功能清单认领并更新生成总览。指定主仓库 venv 的相关 pytest 加 `tools/test_feature_inventory.py`、`tools/tests/test_dev_guide.py` 为 **50 passed、31 subtests passed**；`pq.py lint`、`sh -n tools/run-tests.sh`、`git diff --check` 均退出 0；DisplayGeometry、FirstBootState、ControlException 三项 Java 测试通过，应用全部 Java 源码连同生成资源用 Android API 36 编译通过（仅现有 Java 8/deprecated API 警告）。只读解析主仓库 `.work/husky/boot-lxc.img`，报告保存在本工作树 `.work/husky/kernel-report.json`：`kernel_release=6.1.145-android14-11`、LZ4 legacy、`boot_bytes=67108864`、`boot_sha256=54053ca108d72d76cb627debef749e360c7e567ef70feebdf255cc5e731d2b3f`，解压 Image 为 35,699,200 字节、SHA-256 `70aa523bc4b54e4a84850c2bf0294d7a71cbb227320564c273331a707c089330`，与既有 boot 候选记录一致。本轮未访问手机，KGSL 节点在应用上下文的可见性、OEM DPI 与实际面板尺寸及 Plasma 实机体验仍待 M4；未执行完整 Linux 系统测试或 3.8 pack/verify，不以离线结果代替这些验收。
- 2026-10-04：**1.5 完成，M1 完成。** 经用户同意：先 `fastboot boot` 原厂 `boot.img` 作对照组，约 26 秒开机，su 可用，319 个模块与基线名单相同，证明本机支持临时启动。再临时启动候选 `boot-lxc.img`，约 28 秒开机：`uname -r` 为 `6.1.145-android14-11`；Enforcing；六项配置 `=y`；`/proc/sysvipc` 有 msg/sem/shm；mqueue 与 devtmpfs（311 个节点）可挂载；busybox `unshare -r -p -i -m -f --mount-proc` 内 PID 为 1、uid_map 为 `0 0 1`、IPC 可用；319 个模块名单与基线完全相同；dmesg 无模块符号或签名错误（匹配到的 4 行 `panic` 字样都是正常配置日志）；Wi-Fi 已连接，屏幕开启，触摸设备 `fts` 在；用户手动确认触屏、显示、网络正常。用户再次同意后 `fastboot flash boot_b`，重启后复查同样全部通过；从 `/dev/block/by-name/boot_b` 读回的 SHA-256 为 `54053ca1…`，与候选镜像一致。回退：`fastboot flash boot_b .work/husky/stock/boot.img`。
- 2026-10-04：**1.4 完成。** 原厂 Image 中只有一张 1,357 字节、subject 为 `CN = Build time autogenerated kernel key` 的证书（SHA-256 `91f55959…`），候选 Image 中同样只有一张等长、同 subject 的证书。用原厂证书对 `system_dlkm` 59 个模块做 CMS 验签全部通过；反向对照：用候选证书验签前 10 个全部失败，证明必须换证书。`restore_module_trust.py` 只改了证书范围内的 1,095 字节，范围外字节不变（`.work/husky/trust-report.json`）。重打包：kernel 用 `lz4 -l -12 --favor-decSpeed`，`mkbootimg --header_version 4`、无 ramdisk、cmdline 为空；原厂 boot 签名区大小为 0，无需处理。AVB 采用与本机已能启动的 Magisk init_boot 相同的做法：保留原厂 vbmeta 签名块（公钥 `69da4e73…`、rollback index 1775347200），只按新长度重写 footer；签名校验必然失败，依赖已解锁（orange）放行。`unpack_bootimg` 比对：与原厂相比只有 `kernel_size` 不同；boot 中的 kernel 解压后与 `Image.trusted` 逐字节一致。候选 `boot-lxc.img` SHA-256 `54053ca108d72d76cb627debef749e360c7e567ef70feebdf255cc5e731d2b3f`。已知差异：版本串为 `6.1.145-android14-11`，没有 `-gfa1d6308d1fe-ab14691759` 后缀（`--config=stamp` 没有生效）。模块装载在 MODVERSIONS 下不比较版本号部分的 vermagic，但仍需在 1.5 中实测确认。
- 2026-10-04：1.5 检查脚本 `.work/husky/check-kernel.sh` 在原厂内核上先跑一遍作反向对照：六项配置均无；`/proc/sysvipc` 不存在；mqueue、devtmpfs 挂载报 `No such device`；`unshare -r -p -i -m` 报 `Invalid argument`；319 个模块，dmesg 无模块错误。
- 2026-10-04：**M0 完成。** 0.1 原厂基线（319 个已加载模块、Enforcing、dmesg 无模块错误）；0.2 `prepare_pixel_stock.py`；0.3 husky spec 与 Pixel 预检，真机只读预检全部通过；AVB 公钥 SHA-1 经 AOSP avbtool 独立核对一致。
- 2026-10-04：**1.1、1.2 完成。** 编译机同步固定 manifest，`common` 为 `fa1d6308`，clang `r487747c` 与原厂一致。`BUILD_NUMBER=14691759 tools/bazel build --config=fast --config=stamp --lto=none //common:kernel_aarch64` 用时 218 秒。`.config` 与原厂只差 `CONFIG_FRAME_WARN`（2048 对 0，与 ABI 无关）。`module_abi.py` 对原厂 331 个模块：20,027 个引用、0 个 CRC 不匹配、1,886 个由其他 vendor 模块提供的引用记为基线（`.work/husky/abi-baseline.json`）。首次对比出现 66 个不匹配，查明是提取时 16K 版模块覆盖了同名 4K 版，详见 knowledge。未解决：版本串缺 `-gfa1d6308d1fe-ab14691759` 后缀（`--config=stamp` 未生效），与 ABI 无关，留待 1.4 处理。
- 2026-10-04：**1.3 完成。** 在 `fa1d6308` 上核实 `task_struct` KABI 槽 1、2 已用，3–8 空闲；`sysv_sem` 8 字节放进槽 6，`sysv_shm` 16 字节放进槽 7–8（`packages/gki-android14-6.1/`）。`--defconfig_fragment=//rungic:lxc_defconfig` 在本 Kleaf 可用，配置变化只有六项及其自动子项（`SYSVIPC_COMPAT`、`SYSVIPC_SYSCTL`、`POSIX_MQUEUE_SYSCTL`）。`module_abi.py --baseline`：331 个模块清单一致，18,141 个匹配，0 个 CRC 不匹配，未解析 1,886 个与基线相同，退出码 0（`.work/husky/abi-lxc.json`，Image SHA-256 `0f391744…`）。`pahole`：`task_struct` 仍为 4,800 字节，只有偏移 3640–3663 的槽 6–8 变成 union，其余 215 个成员偏移不变；`nsproxy`、`ipc_namespace`、`pid_namespace`、`user_namespace`、`cred` 逐行相同。
- 2026-10-04：Codex 在 `task/husky-spec` 完成 Task 0.3 的仓库改动，按本次要求保留未提交状态。新增 [husky spec](../profiles/devices/google/husky/CP1A.260405.005.json) 与 [知识档案](../profiles/devices/google/husky/CP1A.260405.005-knowledge.md)，身份、uname 与 319 个模块基线取自真实只读采集副本；十个分区摘要与 archive name/SHA 取自真实 Pixel 提取报告。先写失败测试，再实现按 `stock.format=pixel-factory` 选择的预检路径，保留旧 Motorola 路径；从主仓库只读原厂 `vbmeta.img` 用纯 Python 解析并计算 AVB 公钥 SHA-1 `69da4e73583acf8741905c590537f6b73d8c69df`，解析器有 offset/size、截断与越界测试。主仓库原厂文件的全部声明分区、字节数与公钥摘要通过离线核验；指定 venv 的 `python -m pytest -q tools/ci/test_device_preflight.py tools/test_feature_inventory.py tools/tests/test_dev_guide.py` 为 31 passed、110 subtests passed，`python tools/pq.py lint` 退出 0。质量归属/文档分类与生成总览已更新。模块信任证书摘要保留 null，等待 Task 1.4；本轮没有访问手机或 VM，完整真机 preflight 仍待 Claude 验收，不标记本任务实机验收通过。
- 2026-10-04：Codex 完成 Task 1.3 的 ABI 工具阻断项修复。离线验证：指定 venv 的 `python -m pytest -q tools/ci/test_module_abi.py` 为 18 passed、17 subtests passed，`python tools/pq.py lint` 退出 0。覆盖缺失导出、模块新增/删除/改名/摘要变化、相同基线、未解析引用减少、CRC 差异、不传 baseline 的兼容行为和可选内核产物摘要；空清单及损坏基线也拒绝。仅完成工具与合成输入验证，1.2 原厂等价报告、候选内核编译及原厂模块实测仍由后续任务验收。
- 2026-10-04：Codex（gpt-6.1-sol / high）审查本计划，提出 11 条意见（1 个阻断：`module_abi.py` 漏判缺失导出）。核实后全部采纳：LTO 实测为 `LTO_NONE`，与 `--lto=none` 一致；其余修订见 M0/M1 各任务。
- 2026-10-04：编译机 `repo init --standalone-manifest` 后同步 ACK 源码；内核 manifest 仓库没有 `android14-6.1-2025-09` 分支，只能用固定的 manifest 文件。

- 2026-10-04：bootloader 已解锁，Magisk 31.0 root 完成（`uid=0 context=u:r:magisk:s0`，Enforcing）；原厂包与 Magisk 均已校验；编译机已创建；Codex 已配置为 gpt-6.1-sol / high；读取原厂内核配置，得出上面的差距表。
