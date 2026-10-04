# Pixel 8 Pro：CP1A.260405.005 已知事实与操作入口

记录日期：2026-10-04。适用范围为本次采集的 husky / GE9DP / CP1A.260405.005。本文是维护知识档案，不授权刷写或清数据；其他设备实例或固件须重新核对。

## 固定输入与资料优先级

- [执行 spec](CP1A.260405.005.json)：schema v1，id 为 `google/husky/CP1A.260405.005`，本次 SHA-256 为 `e3d4993441829ae752a483f1b6542a130ab587386f311962c4c9293adb6ef21d`。模块信任证书尚待 Task 1.4 回填，spec 还未定稿；回填后同步核对新的 spec SHA 与后续产物绑定。
- [108 篇移植计划与记录](../../../../docs/108-husky-port.md)：原厂配置差距、固定源码来源、任务划分、设备操作约束及后续验收。
- [三段式 skill](../../../../.agents/skills/rungic-three-stage-image/SKILL.md) 与 [兼容契约](../../../../docs/75-image-build-separation.md)：CI1 底座、CI2 独立镜像、CI3 独立安装的职责与验收边界。
- 原包为 `husky-cp1a.260405.005-factory-c22affec.zip`，SHA-256 `c22affece233e05bbbbd61a9f919ebf329f9532a78c1882dbc5ee105ebfbc105`。原厂提取入口是 `tools/prepare_pixel_stock.py`，不用 Motorola XML/super 分片入口。

本任务使用两份只读证据副本；以下摘要便于与原始记录核对，文件不入库：

| 输入 | 来源与内容 | SHA-256 |
| --- | --- | --- |
| `/tmp/husky-verification.json` | 原厂包经 `prepare_pixel_stock.py` 实际提取的报告；对应主仓库 `.work/husky/stock/verification.json` | `2e33b3ef0601b0f1994909c3bbbc66285dc4a647fa938071703928f69f279992` |
| `/tmp/husky-baseline-state.txt` | 手机实际 getprop、uname、SELinux、模块数量与完整名单 | `830c9e2e36db963113f32854adaa0b0ba0061f9cd63c59a70084f31dc4638e91` |

## 身份与原厂运行基线

| 项目 | 实测值 |
| --- | --- |
| product / device / SKU | `husky` / `husky` / `GE9DP` |
| fingerprint | `google/husky/husky:16/CP1A.260405.005/15001963:user/release-keys` |
| bootloader | `ripcurrent-16.4-14540574` |
| Android API / 安全补丁 | `36` / `2026-04-05` |
| 活动槽 | `_b`（实例状态，执行设备命令前重新读取） |
| 解锁 / Verified Boot | `ro.boot.flash.locked=0` / `orange` |
| uname -r | `6.1.145-android14-11-gfa1d6308d1fe-ab14691759` |
| SELinux | `Enforcing` |
| 已加载模块 | **319** |

原厂模块名单以基线文件为准。`== modules` 段包含 319 行；按原顺序、LF 分行并带末尾换行的模块名单 SHA-256 为 `191797df0ccb0c7499c1f1fd1435eb53339aed2b67e2688db0ec2088febbfda9`。候选内核启动后须逐项比较名单并核对 dmesg；只比较数量不能证明模块全部恢复。

108 篇记录本次实例的序列号为 `3B271FDJG005G7`。序列号、ADB 端口和活动槽不是型号能力；每次连接先查归属，后续命令精确指定端口和序列号。本任务没有连接手机，也没有采集新的 dmesg。

## 分区布局与 AVB

Pixel 出厂包包含内层 `image-husky-cp1a.260405.005.zip`。提取报告给出十个分区镜像的摘要，全部逐项写入 spec：

- 启动与设备配置：`boot`、`init_boot`、`vendor_boot`、**`vendor_kernel_boot`**、`dtbo`。
- AVB 链：`vbmeta`、**`vbmeta_system`、`vbmeta_vendor`**；后两者为链式 vbmeta，不能漏记摘要或用其他设备的 AVB 文件替换。
- 模块分区：**`vendor_dlkm`、`system_dlkm`**。M1 只改 GKI/boot，`vendor_kernel_boot` 和两个 dlkm 分区保持原厂。

这份输入没有 Motorola 的 `flashfile.xml` 或 `super.img_sparsechunk.N`，不伪造 `super_sha256`、product 容量或分片清单；“没有 super 分片”不表示设备没有动态分区。本轮不刷 super、不改 vbmeta、不清数据。`android-info.txt` 是提取元数据，不是分区镜像。

AVB 公钥 SHA-1 为 `69da4e73583acf8741905c590537f6b73d8c69df`。计算输入是主仓库只读的 `.work/husky/stock/vbmeta.img`，先核对其 SHA-256 与提取报告一致，再用 `tools/ci/preflight.py` 的纯 Python 解析器计算。

解析依据为 AOSP 的 [AvbVBMetaImageHeader](https://android.googlesource.com/platform/external/avb/+/refs/heads/main/libavb/avb_vbmeta_image.h)（MIT，核对的源码 blob `f9cbac447d0941429d813ca0aadef4401e7ec8d7`）。读取 256 字节大端 header 的 authentication/auxiliary 长度及 public key offset/size，在 auxiliary block 内截取原始公钥 blob 后做 SHA-1；本轮计算完全离线。复用标准格式、仅提取摘要，无需安装 avbtool；它不验证签名、回退索引或整条 AVB 链，不能当作完整 AVB 验收。

## 内核、root 与待补字段

固定源码来自 CI 构建 **14691759** 的 `manifest_14691759.xml`，仓库文件为 `kernel/targets/gki/android14-6.1-manifest.xml`，common commit 为 `fa1d6308d1fe803c3fdebcd3ee6f7a1155fc3462`，页大小 **4096**。这里固定 manifest 文件与构建号，不复制 X70 的 manifest commit 或 6.12 内核参数。

`kernel.module_trust_certificate_sha256` 当前为 **null**，由 **Task 1.4** 从原厂 Image 提取并验证模块签名后回填。AVB 公钥与 GKI 模块信任证书用途不同，不能用前者摘要代填。当前 spec 只固定事实，不代表 LXC GKI 已编译或验收。

108 篇已记录 **Magisk 31.0 在 `init_boot_b`**，保持 boot 与 init_boot 的职责分离；候选 boot 不替换已有 root 入口。运行中的 Magisk 31.0 不执行可能返回 SQL NULL 的查询，已知问题见 [39 篇](../../../../docs/39-magisk-daemon-crash.md)。

## 编译机、代理与空间门槛

108 篇指定的 GCP 编译 VM 为 `rungic-kernel-build`，zone `asia-northeast1-b`，Ubuntu 24.04 / x86_64。**SSH 只经 IAP 隧道**：

```sh
gcloud compute ssh rungic-kernel-build --zone=asia-northeast1-b --tunnel-through-iap
```

2026-10-04 公网 22 端口不可达，不循环尝试公网 SSH；上机后重新核验主机身份、架构、路由和代理。本任务没有访问 VM，也不把其历史配置当作本次现场核验。

`release_requirements.arch/rootfs_arch` 均为 `arm64`，API 36。30% 电量、120 GiB 初始可用空间和 60 GiB 保留空间沿用 X70 的保守门槛，是预检策略，不是本轮实测占用。`deployment.phone_http_proxy` 见下文“网络代理”一节。`purity` 为空，本任务不做预装清理。

## 预检契约与验收边界

spec 的 `stock.format=pixel-factory` 选择 Pixel 提取报告契约；未声明 format 的旧 spec 保持 Motorola 路径。Pixel 分支核对报告 schema、archive name/SHA、device，以及 fingerprint 中的 build ID；报告必须明确 `flashed=false`、`device_tested=false`。

每个 `stock.<partition>_sha256` 声明一个必查镜像；预检同时比较报告摘要、实际文件摘要与报告字节数。已填写 AVB key 时再核对实际 vbmeta 公钥摘要。Pixel 报告不需要 manifest fingerprint 或 super 字段，预检输出绑定真实 verification SHA，不生成虚假的 stock manifest SHA。手机端沿用只读的身份、槽位、内核、Enforcing、电量检查及宿主空间门槛。

离线测试在 `tools/ci/test_device_preflight.py`，包含 Motorola 回归、不同代号的 Pixel 替身、所有声明分区的损坏/缺失，以及 AVB offset/size、截断和越界输入。主仓库原厂文件的十个分区摘要、字节数与公钥摘要已通过只读离线核验。**真机完整 preflight、候选 GKI、模块签名信任和 Plasma 首装仍待相应任务验收**；提供的基线副本不替代当前设备、电量或连接状态。

## 网络代理

- `deployment.phone_http_proxy` 暂为空字符串（不用代理）。X70 spec 和 AGENTS.md:85 里的 `http://192.168.5.45:6152` 是原作者 Mac mini 上的 Surge 代理（AGENTS.md:80、84），不是本机的网络，不能沿用。M3 打独立安装包前要和用户确认：手机所在网络是否需要代理才能访问 Ubuntu/apt 源，需要的话填用户自己的代理地址，并发新的 spec。

## 原厂模块提取（2026-10-04 实测）

- 原厂 `.ko` 共 **331** 个：`vendor_kernel_boot` ramdisk 210 个（LZ4 legacy 压缩的 cpio），`vendor_dlkm` 62 个，`system_dlkm` 59 个。两个 dlkm 都是 ext4（e2fsprogs 的 `debugfs -R "rdump / dir"` 可读）。
- **`vendor_dlkm/lib/modules/16k-mode/` 下有 5 个同名模块**，是为 16K 页内核（内层包里的 `kernel_16k`，构建 14810637、commit `164ae0b804dd`）编的，不是本机 4K 内核用的。提取时必须排除 `16k-mode`，并保留目录结构，不能把所有 `.ko` 平铺到一个目录：平铺后 16K 版会覆盖同名 4K 版，`bcmdhd4398.ko` 和 `cs40l26-i2c.ko` 会显示出 66 个假的 CRC 不匹配。判断依据：手机上 `/vendor_dlkm/lib/modules/bcmdhd4398.ko` 的 SHA-256 为 `6e29e0170ebbd80b811264eeabcb84c6003f25ed4b704ec48911ef3080b95cae`，与排除 16K 后提取的文件一致，其 CRC 与原样构建一致。
- 原厂 `boot.img` 的 kernel 是 LZ4 legacy 压缩（`lz4 -l`），解压后 `Image` 35,564,032 字节。重打包时保持同样压缩方式。
- `vbmeta.img` 把 `boot`、`init_boot`、`vbmeta_vendor` 作为链式分区，公钥 SHA-1 都是 `69da4e73…`（`vbmeta_system` 为 `d0495212…`）。改过的 boot 校验必然失败；已解锁（orange）时允许，这也是 Magisk 修补后的 init_boot 能启动的原因。
