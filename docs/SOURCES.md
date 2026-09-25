# 来源、许可证与固定输入

本项目保留原版Git历史，按用户可见能力移植/适配GuDong，参考另外两项目的业务方法；不是四套后台叠加，也不是整仓覆盖。

| 来源 | 固定提交 | 用途 | 许可 |
|---|---|---|---|
| [zhinianboke/xianyu-auto-reply](https://github.com/zhinianboke/xianyu-auto-reply) | [fdc8eb039be771456ecfbbbe43fa26f57feb3947](https://github.com/zhinianboke/xianyu-auto-reply/commit/fdc8eb039be771456ecfbbbe43fa26f57feb3947) | 原版多服务底座、React、数据模型及历史 | [AGPL-3.0原文](../LICENSE) |
| [GuDong2003/xianyu-auto-reply-fix](https://github.com/GuDong2003/xianyu-auto-reply-fix) | [e8fe7ba4239614fe64d194cf3bed8a3aa16d5ab6](https://github.com/GuDong2003/xianyu-auto-reply-fix/commit/e8fe7ba4239614fe64d194cf3bed8a3aa16d5ab6) | 49组能力、旧配置语义、迁移结构和兼容测试输入 | [AGPL-3.0原文](../LICENSE) |
| [LENKIN233/xianyu-monitor-skill](https://github.com/LENKIN233/xianyu-monitor-skill) | [1c23e1b48a9b1771baba475d90ab4affadf2e817](https://github.com/LENKIN233/xianyu-monitor-skill/commit/1c23e1b48a9b1771baba475d90ab4affadf2e817) | 完整基线、分页/代次关联、重启去重与通知分离 | [MIT原文及署名](licenses/LENKIN233-MIT.txt) |
| [shaxiu/XianyuAutoAgent](https://github.com/shaxiu/XianyuAutoAgent) | [540bbc26cf02ee6348d997843942776a9be9460b](https://github.com/shaxiu/XianyuAutoAgent/commit/540bbc26cf02ee6348d997843942776a9be9460b) | 议价意图/次数和最近完整往来的参考，修正单字判断及旧窗口问题 | [GPL-3.0原文](licenses/shaxiu-GPL-3.0.txt) |

原版研究提交为 `86474fd70670f814dd1fe1ba53575394ffa93668`，实施时已更新到上表基线。主仓LICENSE和GuDong固定版许可证同字节；MIT署名 `Copyright (c) 2026 Xianyu Monitor Skill Contributors` 保留。各来源原有版权/许可声明继续有效，整合说明不重新许可来源代码。

## 许可证SHA256

- AGPL：`0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0`
- LENKIN MIT：`ac8af69dbb823ca3b7de7b36341ab09d9db00e1a61f06c310e2c3426d2dab7b2`
- shaxiu GPL：`c9e9490d4d9e45e2f88d4cea1ffca1fbbe2ce5681921db59d33dcae8b40c958a`

## 固定验收输入

- [规格](specs/XYMB-SPEC-001.md)：`d35f88a0ed37db35d709e5e6439f6af062f58461a0273c2ece4f1c47a64b6657`
- [49组研究矩阵](provenance/feature-coverage-matrix.json)：`22cc6973604004e5f82c0883ca511747837e0c9e8f7ac0e35c3512c0d60584b0`
- [19项旧差异处置](provenance/local-patch-dispositions.json)：`feb92cbdb8ffbd0e0b7975196f6c35c233171a5ee78f3f43874c4f6620d0da4b`

规格v1.0.1仅修正文档引用，业务范围保持v1.0；原v1.0 SHA256为`305e4abcb515fcefc98def5456ee897859ee8059a2b83bd2751e1d384d62eac5`，可从Git历史查阅。

这三份是冻结的范围和来源依据，不更新里面的历史状态冒充当前结果。当前实现与测试在 [覆盖索引](COVERAGE.md)，实际运行报告在仓库外的项目归档。19项旧差异中6项与GuDong固定版相同，不重复声称为本地新补丁。

`main`保持纯上游，增强只在 `xianyu-management-bot-fork`。上游同步须核对新提交和回归；切换分支不等于发布。具体接线见 [底座](implementation/baseline.md)、[议价](implementation/bargaining.md) 和 [监控](implementation/monitor.md)。
