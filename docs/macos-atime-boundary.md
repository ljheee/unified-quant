# macOS 确认写入为什么仍被阻断

> **历史记录，非当前读取契约。** 本文记录 PR #30 在不允许 atime 变化时的调查；维护者后来将它合并到 `main` 的 `9318f16a11c581661913d17b7371a108dc0e5154`。随后用户明确允许读取自然更新 atime，并要求不恢复它。新分支的实现、严格 ctime 安全门及尚未解决的交换边界见 [installer-atime-contract.md](installer-atime-contract.md)。下文的“当前”“本次”“不修改生产 helper”和“必须保留 atime”等均描述当时范围，不是新分支的现状或要求。

**PR #30 调查时不能宣称 Mac 安装已修复。** 原生 fd xattr 兼容层已通过真实 macOS 的属性读写测试，但确认写入仍被另一项基线问题阻断：读取原件会改变访问时间（atime）；恢复 atime 又会改变元数据变更时间（ctime）。不能把新的 ctime 当成本进程造成而直接接受，否则会失去对并发修改的证据。

[已有真实 macOS 对照](https://github.com/jack4leo/self-evolution-flywheel/actions/runs/39217885934/job/117945947718)显示，原 main `9d04f1b4939f53125f92b718ad8d908b744ffc80` 与 PR `ca1ba0637e8c61d449a11df3162fd50fa600784e` 都在同一快照边界拒绝。打印出的字段只有 `st_ctime_ns` 发生变化；Mac 安装、确认与恢复测试仍失败。这不是 Python xattr 回退失败，也不是跨平台验证通过。

## 为什么不能把 ctime 变化归因于自己

调用链是 `proposal_command` → `confirmation_snapshot` → `read_open_file`。后者以 fd 读取，再以 `os.utime(fd, ns=原时间)` 恢复 atime/mtime。`confirmation_snapshot` 比较读取前后的 inode、链接数、内容关联字段、mode/uid/gid 及 atime/mtime/ctime，随后再次读取并校验 xattrs；确认应用前还比较完整的 `current/latest` 与用户确认的快照。

[Darwin futimes 手册](https://keith.github.io/xcode-man-pages/utimes.2.html)明确说，设置访问/修改时间也会把 inode-change-time 设为当前时间。它没有“只在状态仍等于快照时修改”的比较交换接口，也没有返回修改者身份。再次 `fstat`、测量时间差或重试，只增加观察点，不能证明两个观察点之间没有别的写入者。

例如，不合作进程可以在最后一次检查后修改再恢复内容、ACL、flags 或资源 fork，随后安装器恢复时间。最终内容和被恢复的字段可能相同，而 ctime 是剩余的变化证据。若把它统一解释为“自己的 utime”，就扩大了原检查允许通过的状态。重复哈希不能证明 ACL/flags 完整，也不能证明历史上没有发生修改。

现有普通 `identity()` 在恢复 atime 后重采样，返回签名并不持久化 ctime；现有 `confirmation_identity_matches()` 也有专用于 rename 后比较的 ctime 例外。这里不修改、不推广这些已有边界，更不能将 rename 的例外挪到读取或确认前检查中。

## 逐项评估候选方案

| 候选 | 可以保证什么 | 为什么目前不能作为通用修复 |
| --- | --- | --- |
| `read` / `pread` / 复制 fd | 不重新解析原件路径，fd 固定原 inode | 不阻止访问时间更新，也不阻止已打开 fd 的写入者。fd-only 不是 no-atime。 |
| `F_NOCACHE` / `F_RDAHEAD` | 控制缓存或预读 | [fcntl 手册](https://keith.github.io/xcode-man-pages/fcntl.2.html)没有授予 no-atime 或独占修改保证；`F_SINGLE_WRITER` 也不是排斥其他写者的锁。 |
| 只读 `mmap(MAP_PRIVATE)` | 通过 fd 映射；对映射自身写入采用 COW | [Apple 内核源码](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_mman.c)明确在非 `MNT_NOATIME` 卷上为文件映射设置访问时间。`MAP_PRIVATE` 不是只读历史快照，并发截断还可能使映射访问失败。 |
| `fcopyfile` / `COPYFILE_CLONE` | 可以接受源和目标 fd | [copyfile 手册](https://keith.github.io/xcode-man-pages/copyfile.3.html)明确 fd 版本的 CLONE 会回退普通复制，CLONE_FORCE 不受支持，不能用它保证不读取原件。按路径重新打开原件也不符合本任务要求。 |
| `fclonefileat(srcfd, dirfd, leaf, flags)` | 固定源 fd，同文件系统原子创建 COW 克隆；之后读克隆可避免对原件直接读 | [clonefile 手册](https://keith.github.io/xcode-man-pages/clonefile.2.html)没有普遍保证源时间戳不变；API 返回成功码而非新 inode 的 fd。克隆到名称后再 open 之间可被同 UID 进程替换，`O_NOFOLLOW` 只拒绝符号链接，不能认证替换的普通文件。0700 目录不是同 UID 的安全边界。 |
| `flock` / `O_EXLOCK` / record lock | 协调遵守锁协议的进程 | 是协作锁，不能把任意持有的写 fd、ACL/flags/xattr 操作变成受保护事务。未证明的 lease 也不能当作全面独占。 |
| no-atime 挂载或文件系统快照 | 某些环境可能避免原件读取的 atime 更新或提供不可变副本 | 是额外的平台与管理前提，不是当前一般 macOS 文件的保证。本次没有改变任何挂载、请求特权，也不建议把修改用户卷配置当作修复。 |
| “恢复后接受新的 ctime”或忽略 ctime | 可能让当前用例看似通过 | 会吞掉无法归因的并发修改，明确禁止。设置/恢复 ctime、放大容忍时间窗口、自动重试直到接受也不能补上原子性。 |

fd 克隆值得进一步研究，但 **COW、原子创建、原子一致的完整元数据快照，以及返回可认证目标 fd，是不同保证**。`EXDEV`、`ENOTSUP`、读取失败、源状态变化或目标身份无法证明，都必须拒绝，不能改用普通原件读取作为降级路径。随机名称只能降低撞名概率，不能替代身份校验。

## 哪些元数据不能被省略

macOS 的 POSIX mode/owner、[ACL](https://github.com/apple-oss-distributions/Libc/blob/main/include/sys/acl.h)、BSD flags 和 xattrs 是不同层。完整 xattr 枚举不能证明 ACL/flags 完整；数据流哈希也不覆盖资源 fork。当前确认快照并未显式保存 ACL 和 `st_flags`，`apply_metadata` 也没有完整的 ACL/flags 复制协议，所以不能宣称已证明所有 macOS 元数据的安全复制，或用“其余字段没变”来豁免 ctime。

原生 xattr 后端包含 quarantine、二进制/空属性及可枚举的 `com.apple.ResourceFork`；压缩属性 `com.apple.decmpfs` 仍明确拒绝。这是 xattr 层的保证，不等于资源 fork 在所有操作中的 atime 行为、ACL、flags 或所有安全标签均已验证。任何读取失败或不完整枚举仍须拒绝。

克隆还有不同的 owner、setuid/setgid 与 ACL 继承规则；[Apple 的 VFS 实现](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/vfs/vfs_syscalls.c)也对部分 flags 做变换。不能直接把克隆的元数据当作原件基线，或直接用克隆替换原件而假定所有属性相同。

## 并发写入与恢复

持有 fd 固定的是 inode，不冻结内容或元数据。用户确认前后、交换前最后检查之后，甚至交换后验证之后，都可能出现持有旧写 fd 的进程。原子 `RENAME_SWAP` 只交换名称，不是“inode + 完整元数据仍等于快照才交换”的 CAS。

本次不改确认、交换、journal、恢复或清理实现。状态无法证明时继续拒绝；已发生交换则保留 active/recovery 两个 inode 与日志，报告不确定性。不能自动交换回去、重写原件或根据先前看到的身份 unlink，因为这可能覆盖或删除稍后的合法修改。现有保守恢复边界应保持，不能承诺排除所有晚写者。

## 隔离 macOS 实验

[11 项探针](https://github.com/jack4leo/self-evolution-flywheel/actions/runs/39219009889/job/117949629843)在提交 `4baeffa6cb4bb7bdeef7fdbcb8199234f95905e6`、GitHub 的 macOS 26.6.2（25G83）arm64 runner 上全部执行完成，无跳过。代码见 `tests/test-macos-atime-boundary.py`；任务日志逐项打印原始前后字段，结果如下。

| 实验 | 本次真实结果 |
| --- | --- |
| fd `read`、`pread`、`F_NOCACHE` + read、fd `fcopyfile(DATA)`、私有 mmap | 五种方式都正确读取内容，也都改变原件 atime；不能直接保留原 atime。 |
| read 后恢复原 atime/mtime | atime/mtime 回到原值，但 ctime 改变；原 helper 的确认快照仍明确拒绝。 |
| 原生 fd xattr 读取 | binary、empty、quarantine、ResourceFork 的值完整读取；本次未观察到源字段变化。这不替代内容读取或所有文件系统的保证。 |
| fd 克隆 + 仅从克隆读取 | 本次源字段不变，四类 xattrs 相同；原件持有的写 fd 随后写入，不改变克隆原内容。它说明该候选有用，不证明目标名字没有被替换。 |
| 克隆返回后、open 前替换目标 | 同 UID 测试角色替换为等长普通文件，匹配 dev/nlink/size/mode/uid/gid/atime/mtime；`O_NOFOLLOW` 仍打开伪造内容，源的全部记录字段未变。新目标 inode 没有可比对的原子返回基线。这是具体反例，不只是文档保证不足。 |
| ACL 修改 | 原生 fd ACL 读取成功，mode 不变，但 ctime 改变。仅比较 mode/owner/xattrs 会漏掉这类元数据。 |
| flags、资源 fork、同长写入后还原并恢复时间 | 内容恢复，记录的 flags 等字段回到原值，仅 ctime 留下变化证据。把 utime 后的新 ctime 一律接受，无法证明这些操作没有插入。 |

进一步的探针将 flags、普通 xattr、ResourceFork、数据与 ACL 分为五种独立的同 UID 子进程注入：子进程不参与安装器的锁，修改并恢复元数据，父进程再恢复时间并检查 ctime 证据。克隆目标替换也由自行打开 0700 目录的同 UID 子进程执行，而不是假设该目录可以隔离同 UID 写者。ACL 另有“写入后”和“只读后”两组基线，明确把 ctime 变化归因于 ACL 写入，不归因于 `acl_get_fd_np` 读取。最新运行结果应查看 [PR #30 检查](https://github.com/jack4leo/self-evolution-flywheel/pull/30/checks)；探针任务绿色不代表原有事务测试绿色。

探针仅操作其自己创建的临时文件；它们证明的是候选机制的行为与反例，不是生产安装器端到端通过。Linux 上跳过这些 Darwin 专用探针不算 macOS 验证。测试清理只针对隔离 fixture，不是新增的生产恢复方案。

## 继续修复的门槛

在当前约束下，没有已证明可直接部署的通用读取/恢复方案。本次不修改生产 helper，不删除 ctime、不重新解释自己的 ctime、不减少 metadata 校验，也不以失败测试的绿色诊断任务冒充修复。

后续候选至少需要：原件 fd-only 且可证明的 atime 保留机制；如使用克隆，必须建立目标 fd 与刚创建克隆的不可替换绑定；完整处理 ACL、flags、所有 xattrs 与资源 fork；保留所有现有确认前检查与保守恢复；注入并发源修改、目标替换、部分枚举与恢复失败；最后通过真实 macOS 安装/重试/恢复回归和独立安全审查。若需要合作写者或独占管理环境，应作为明确的产品前提另行获得同意，不能暗中缩小威胁模型。
