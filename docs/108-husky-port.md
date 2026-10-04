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
- **写设备前必须得到用户在对话中的明确同意**；新内核先 `fastboot boot` 临时启动（SKILL 要求不假设它可用，先实测），通过后才刷入。
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
| M0 | 设备事实、spec、原厂基线 | spec 字段全部来自实测；基线模块数与 dmesg 记录在案 |
| M1 | LXC GKI 内核 | 0 个 CRC 差异；`fastboot boot` 开机后模块数等于基线、Enforcing、namespaces 可用；用户同意后刷入 |
| M2 | 无 KGSL 的软件渲染路径 | 单独计划，M1 完成后编写 |
| M3 | RungicOS rootfs、APK 与独立安装包 | 单独计划，M1 完成后编写 |
| M4 | 实机安装与验收 | 到达 Plasma 桌面并可触控 |

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

- [ ] 采集身份与状态：
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

- [ ] 从原厂 `Image` 提取 GKI 模块签名证书，记下 SHA-256，回填到 spec 的 `module_trust_certificate_sha256`。
- [ ] 用这张证书实际验证原厂 `system_dlkm` 模块的签名（`scripts/extract-module-sig.pl` 或 openssl 校验 PKCS#7），证明它们确实是用这张证书签的；同时确认 `restore_module_trust.py:39-53` 的前提成立（证书在原厂和候选 Image 中都只出现一次，长度相同，subject 相同）。前提不成立就停下，重新评估。
- [ ] `tools/ci/restore_module_trust.py <stock Image> <candidate Image> <stock cert> --certificate-sha256 <sha> --output .work/husky/Image.trusted --report .work/husky/trust-report.json`。
- [ ] 用 AOSP `mkbootimg` 重打包（header v4），保持原厂的 kernel 压缩方式、`os_version`/`os_patch_level`、cmdline；`kernel_size` 必然变化。原厂 boot 里的 GKI 认证签名区和 AVB footer 对新 kernel 已失效：去掉旧签名并说明；bootloader 已解锁（orange），不重签、不改 vbmeta。
- [ ] 验收：`trust-report.json` 显示只替换了证书字节；`unpack_bootimg` 比对新旧 boot：除 `kernel_size`、签名区和 footer 外字段一致，并记录每一处差异。

### 任务 1.5：实机临时启动验证（Claude；需用户同意）

- [ ] **对照组先行**：征得用户同意后，先用 `fastboot -s 3B271FDJG005G7 boot` 临时启动**原厂** `boot.img`，确认这个 bootloader 支持临时启动，且 `init_boot`（Magisk）和 vendor 镜像照常被用上（开机后 `su` 可用、模块全部加载）。
- [ ] 对照组通过后，再临时启动候选 `boot-lxc.img`。
- [ ] 恢复办法：任何一次卡住，都长按电源键 + 音量下键回到 fastboot，正常重启即回到已刷入的原厂 boot（临时启动不会写入分区）。若 `fastboot boot` 不被支持：记录到 knowledge，再和用户确认是否改为直接刷 `boot_<slot>`。
- [ ] 开机后检查脚本 `.work/husky/check-kernel.sh`，用 `adb -s 3B271FDJG005G7 shell su < .work/husky/check-kernel.sh` 执行。脚本内容：`uname -r`（必须带候选的版本串，证明确实是新内核）；`getenforce`；`cut -d' ' -f1 /proc/modules | sort`（与基线名单逐个比较，不只比数量）；`zcat /proc/config.gz` 中六项选项；用 busybox `unshare -U -p -i -m -f` 进入新 namespace，检查里面 `$$` 为 1、能写 `/proc/self/uid_map`；用 `ipcmk -Q`/`ipcrm` 验证 SysV IPC 可用；`mount -t devtmpfs` 到临时目录；完整 `dmesg` 存档，并搜索 `Unknown symbol`、`disagrees about version`、`module verification failed`、`Loading of unsigned module`、`protected`。
- [ ] 验收：模块名单与基线完全一致；Enforcing；六项 `=y`；namespace、uid_map、IPC、devtmpfs 都正常；dmesg 无上述错误；触屏、Wi-Fi、显示正常；Magisk `su` 可用。
- [ ] 通过后再次征得用户同意，重新读当前槽位，`fastboot flash boot_<slot>`，重启并复查一次。

## 记录

- 2026-10-04：**M0 完成。** 0.1 原厂基线（319 个已加载模块、Enforcing、dmesg 无模块错误）；0.2 `prepare_pixel_stock.py`；0.3 husky spec 与 Pixel 预检，真机只读预检全部通过；AVB 公钥 SHA-1 经 AOSP avbtool 独立核对一致。
- 2026-10-04：**1.1、1.2 完成。** 编译机同步固定 manifest，`common` 为 `fa1d6308`，clang `r487747c` 与原厂一致。`BUILD_NUMBER=14691759 tools/bazel build --config=fast --config=stamp --lto=none //common:kernel_aarch64` 用时 218 秒。`.config` 与原厂只差 `CONFIG_FRAME_WARN`（2048 对 0，与 ABI 无关）。`module_abi.py` 对原厂 331 个模块：20,027 个引用、0 个 CRC 不匹配、1,886 个由其他 vendor 模块提供的引用记为基线（`.work/husky/abi-baseline.json`）。首次对比出现 66 个不匹配，查明是提取时 16K 版模块覆盖了同名 4K 版，详见 knowledge。未解决：版本串缺 `-gfa1d6308d1fe-ab14691759` 后缀（`--config=stamp` 未生效），与 ABI 无关，留待 1.4 处理。
- 2026-10-04：**1.3 完成。** 在 `fa1d6308` 上核实 `task_struct` KABI 槽 1、2 已用，3–8 空闲；`sysv_sem` 8 字节放进槽 6，`sysv_shm` 16 字节放进槽 7–8（`packages/gki-android14-6.1/`）。`--defconfig_fragment=//rungic:lxc_defconfig` 在本 Kleaf 可用，配置变化只有六项及其自动子项（`SYSVIPC_COMPAT`、`SYSVIPC_SYSCTL`、`POSIX_MQUEUE_SYSCTL`）。`module_abi.py --baseline`：331 个模块清单一致，18,141 个匹配，0 个 CRC 不匹配，未解析 1,886 个与基线相同，退出码 0（`.work/husky/abi-lxc.json`，Image SHA-256 `0f391744…`）。`pahole`：`task_struct` 仍为 4,800 字节，只有偏移 3640–3663 的槽 6–8 变成 union，其余 215 个成员偏移不变；`nsproxy`、`ipc_namespace`、`pid_namespace`、`user_namespace`、`cred` 逐行相同。
- 2026-10-04：Codex 在 `task/husky-spec` 完成 Task 0.3 的仓库改动，按本次要求保留未提交状态。新增 [husky spec](../profiles/devices/google/husky/CP1A.260405.005.json) 与 [知识档案](../profiles/devices/google/husky/CP1A.260405.005-knowledge.md)，身份、uname 与 319 个模块基线取自真实只读采集副本；十个分区摘要与 archive name/SHA 取自真实 Pixel 提取报告。先写失败测试，再实现按 `stock.format=pixel-factory` 选择的预检路径，保留旧 Motorola 路径；从主仓库只读原厂 `vbmeta.img` 用纯 Python 解析并计算 AVB 公钥 SHA-1 `69da4e73583acf8741905c590537f6b73d8c69df`，解析器有 offset/size、截断与越界测试。主仓库原厂文件的全部声明分区、字节数与公钥摘要通过离线核验；指定 venv 的 `python -m pytest -q tools/ci/test_device_preflight.py tools/test_feature_inventory.py tools/tests/test_dev_guide.py` 为 31 passed、110 subtests passed，`python tools/pq.py lint` 退出 0。质量归属/文档分类与生成总览已更新。模块信任证书摘要保留 null，等待 Task 1.4；本轮没有访问手机或 VM，完整真机 preflight 仍待 Claude 验收，不标记本任务实机验收通过。
- 2026-10-04：Codex 完成 Task 1.3 的 ABI 工具阻断项修复。离线验证：指定 venv 的 `python -m pytest -q tools/ci/test_module_abi.py` 为 18 passed、17 subtests passed，`python tools/pq.py lint` 退出 0。覆盖缺失导出、模块新增/删除/改名/摘要变化、相同基线、未解析引用减少、CRC 差异、不传 baseline 的兼容行为和可选内核产物摘要；空清单及损坏基线也拒绝。仅完成工具与合成输入验证，1.2 原厂等价报告、候选内核编译及原厂模块实测仍由后续任务验收。
- 2026-10-04：Codex（gpt-6.1-sol / high）审查本计划，提出 11 条意见（1 个阻断：`module_abi.py` 漏判缺失导出）。核实后全部采纳：LTO 实测为 `LTO_NONE`，与 `--lto=none` 一致；其余修订见 M0/M1 各任务。
- 2026-10-04：编译机 `repo init --standalone-manifest` 后同步 ACK 源码；内核 manifest 仓库没有 `android14-6.1-2025-09` 分支，只能用固定的 manifest 文件。

- 2026-10-04：bootloader 已解锁，Magisk 31.0 root 完成（`uid=0 context=u:r:magisk:s0`，Enforcing）；原厂包与 Magisk 均已校验；编译机已创建；Codex 已配置为 gpt-6.1-sol / high；读取原厂内核配置，得出上面的差距表。
