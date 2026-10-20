# 安装器读取与版本验证契约

安装器支持的运行方式是：**安装期间只有安装器写入目标配置和目录树**。读取可以自然更新 atime，不恢复原文件 atime。这个前提不是对不合作同 UID 写者的安全保证；最后检查与原子交换之间的既存竞态没有被修复。

## 安装前必须暂停其他写者

具体操作见 [安装文档](installation.md#required-safety-precondition-pause-other-project-writers)。在首次读取既有 `AGENTS.md` 或 Codex/Claude 配置前，关闭或暂停其他 Claude/Codex 会话、编辑器自动保存/格式化、同步工具、生成器和其他安装器/脚本；包括仍持有可写 fd 的任务。独占条件必须持续覆盖预览、确认、staging、发布、验证和失败恢复。暂停的是**其他写者**，不是安装器自己的事务写入。

无法保证这一点时，不自动覆盖既有文件：保留原件，拒绝或跳过替换，待其他写者停止后手工合并。若有恢复条目或保留的 journal，先检查并完成恢复，再安装；不要通过重跑绕过未解决恢复。

这个条件由操作者保证，代码的协作锁无法强制约束不合作写者。它不解决可写 proposal/manifest 的同 UID 授权问题，也不提供版本条件 CAS。

## 读取唯一获准的副作用

预检、比较和提案预览可能自然更新访问时间 **atime**，包括 N、空输入、EOF、无终端、无需更改或安全检查拒绝的情况。noatime 挂载可能不更新它；不要求 atime 一定变化。

安装器不恢复既有 inode 的 atime，不尝试设置 ctime，也不在普通读取后采样一个新 ctime 来重新授权原件。新 staging inode 按原件设置 mtime，但不重用原件 atime。允许读取副作用不授权改变业务内容或其他元数据。

`noatime` 约束的是**同一 inode 的读取副作用**，不是新建文件的初始访问时间。成功更新受管区后，活动路径指向新 staging inode；不能拿它的 atime 与旧 inode 作等值比较。回归必须分别验证：恢复备份仍是 pinned 原 inode、原文及受保护元数据完整，且原 inode 的读取符合挂载行为；新活动 inode 的内容及应保留元数据正确、不复用旧 atime；同版重试不替换活动 inode、不新增备份，并继续满足同 inode 的读取契约。原 inode 的 ctime 仍允许那次已验证 exchange 自身引起的变化，不能扩大为对其他写入的豁免。

## 仍然严格的版本检查

读取前后与确认前的检查排除 atime，但保留 device/inode、nlink、类型/mode、owner/group、size、内容 hash、mtime、**ctime**、全部可枚举 xattrs，以及 Darwin ACL 和 BSD flags。确认绑定父目录身份；读取/枚举错误、版本变化和 staging 不一致都拒绝。

两套 JSON 预检统一使用 helper。diff 显示前后验证原件与 proposed staging 的版本，CLI 应用验证展示记录；准备 staging 与 journal 后再次验证原件和 staging 的名字/版本。检查前可观察到的 ctime-only 变化仍必须拒绝，不能把下面的交换后例外扩展到这些检查。

展示记录保存在临时目录的可写 manifest，**不是同 UID 安全边界**。能共同重写提案、identity 与 manifest 的写者可能绕过展示绑定。在要求抵抗这种写者时，必须另有不可由该写者修改的受信会话授权状态；当前不宣称提供它。

## Darwin 元数据

ACL 使用 [Apple 原生 fd API 和二进制格式](https://github.com/apple-oss-distributions/Libc/blob/main/include/sys/acl.h)读取、复制和验证，包括 ACL-wide flags、ACE flags、权限、GUID 和顺序。仅 `ENOENT` 表示无 ACL；其他错误不是空 ACL。调用无长度参数的 `acl_copy_int_native` 前验证 header、条目数和完整长度。无法安全移除 staging 继承的 ACL 时拒绝。

BSD flags 只接受可安全 staging 的 `UF_NODUMP`、`UF_HIDDEN` 组合；immutable、append、nounlink、压缩、未知/system flags 不静默清零。缺失 Darwin ACL/flags API 同样拒绝。

全部 xattrs 通过 fd 读取，包括 binary、empty、quarantine 和 `com.apple.ResourceFork`。拒绝 `com.apple.decmpfs`，不把旧压缩元数据套到新 payload。失败或变化不能用“内容相同”覆盖；这里不是所有未来 macOS 私有元数据的全覆盖承诺。

## PR #31 的归因

比较合并前 `9318f16a11c581661913d17b7371a108dc0e5154` 与合并提交 `4c3981fbd89dc7101869bd46798997904c41aa6f`，并对照[合并前 CI](https://github.com/jack4leo/self-evolution-flywheel/actions/runs/39223007179)和[合并后 CI](https://github.com/jack4leo/self-evolution-flywheel/actions/runs/39250886412)：

- **strict-ctime 红灯：新加的门揭露既存的更强模型缺口。** 旧实现的交换后比较已经不比较 ctime；#31 给例外显式命名 `allow_exchange_ctime=True`，并新增确定性攻击测试。隔离实验保留原 atime、诊断 fd 使用 Linux `O_NOATIME` 后，合并前后都接受最后检查后的 bytes/mtime 恢复攻击。直接复用攻击中设置 `atime=mtime` 的夹具会使旧实现因 atime 不同拒绝，不能误称为旧版有 ctime 防护。
- **macOS noatime：#31 新增夹具错误。** 合并后日志明确为 `hdiutil: attach: unknown option "-mountOptions"`，尚未运行 noatime 验证。修复为正常 attach 私有 APFS 镜像后，用 [mount(8)](https://keith.github.io/xcode-man-pages/mount.8.html) 的 `-u -o noatime` 更新该卷；不重挂系统/用户卷。
- **Linux installer-tests：#31 引入生产回归。** 合并前 Linux job 通过；合并后 journal 失败清理及 rollback race 夹具失败。新增持久 ctime/nlink 检查，但发布的 link/unlink 和 rollback rename 自身改变 ctime，旧 journal 版本因此过期。这与非合作写者的既存 ctime 竞态不同。
- **合并前普通 macOS 失败**已经存在，源于读取快照/atime 边界；不能把工作流整体红色都归因于 #31。#31 合并后的普通 macOS xattr/交互/元数据 job 已通过；真实 noatime 仍须看本次修复的独立结果。

后续修复仅在新建、安装器拥有的 inode 发布窗口内验证 link/unlink 的已知 nlink/ctime 转换，核对 pinned fd、active 名字、完整身份与父目录，再把发布后的**精确 ctime**持久化到 journal。回滚前仍使用严格 `same_identity`；没有“安装器创建过就永久忽略 ctime”的回退。只有严格前置校验已通过后的那次 recovery rename 允许自身 ctime 转换，恢复条目始终保留、不删除。

若发布后观测或精确版本的 journal 写入失败，旧版本记录仍在，后续严格回滚保留不确定 active 数据供人工恢复；不采样一个新版本绕过失败。新增测试覆盖正常 copy/两个 provider、发布后 ctime-only 与 bytes/mtime 恢复改动拒绝、最终 journal 写入故障保留。目录创建 journal 故障夹具改为定位原语义事件，而非固定第六次写入；其原有文件/目录/journal 恢复断言全部保留。

## 非支持场景：最后检查之后的不合作写者

`tests/test-installer-ctime-gate.py` 保留原测试和 `assertFalse(applied)`，不使用 `expectedFailure`、`continue-on-error` 或降低断言。它让同 UID 子进程在最后检查与真实 exchange 之间改写再恢复 bytes/mtime，证明只剩 ctime 不同，但实现仍报告 `APPLIED`。**这是未修复的限制，不是通过项。**

再次 fstat/hash、保留 fd、随机名称、0700 目录或 advisory lock 都不能把“检查 + exchange”变成版本 CAS。[rename 手册](https://keith.github.io/xcode-man-pages/rename.2.html)没有预期 ctime/内容版本参数；不能声称交换后的 ctime 只可能来自安装器。

默认 CI 对支持模型阻断；上述原测试单列为显式诊断 dispatch，在 Linux/macOS 上直接运行且仍可使诊断工作流失败。主线绿灯只表示支持模型通过，不表示该攻击已修复。若将来扩大威胁模型，必须重新将这个不变的拒绝测试设为对应支持门。

## 怎样验证

- `bash tests/test-install.sh`：完整安装、预检、提案、重试和恢复回归。所有独立 Python 套件另逐一运行，保留任何失败。
- `python3 tests/test-installer-supported-ctime.py -v`：真正阻断的单写者成功交换、原件备份/journal 完整，以及最终检查前 ctime-only 变化拒绝；Linux/macOS 都运行。
- `python3 tests/test-installer-ctime-gate.py -v`：更强模型诊断，当前真实非零失败，不代表支持模型回归。CI 复现：`gh workflow run installer-tests.yml --ref <branch> -f run_unsupported_diagnostics=true`。
- 普通 macOS：yes/no/EOF/no-TTY、展示绑定、native xattrs、ACL/flags/ResourceFork、版本变化、重试和保守恢复。
- `bash tests/run-macos-noatime.sh`：只在隔离 runner 私有 APFS 卷执行；先验证挂载标志与真实 read 前后 atime/ctime，再运行契约套件。创建/挂载/权限/读行为不能验证时报告 **UNVERIFIED** 或实际探针失败并非零退出，不能算通过。卸载失败保留夹具目录。
- `python3 tests/test-macos-noatime-fixture.py -v`：只验证脚本控制流和失败传播，不创建卷，**不能代替真实 macOS/noatime 证据**。

所有实验仅使用隔离工作副本/fixture/托管 CI runner，不访问用户 Mac 或真实目标项目。Linux 上 Darwin skip 不算 macOS 通过。#31 已合并，修复提交在独立 follow-up PR，保持 OPEN、不自动合并。
