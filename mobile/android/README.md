# Context Relay Android companion

原生 Java 首版，无 Google 服务或额外应用框架依赖。最低 Android 8.0 / API 26，编译及目标 API 35。手机只选择电脑已有任务；创建、导入、目录和权限设置仍在电脑管理器完成。

在电脑开启手机连接，粘贴完整 `contextrelay://pair#…`，核对电脑地址和证书 SHA-256 后明确配对。也可由手机已有的扫码工具打开该链接；应用本身不请求相机权限。电脑和手机需要可互通的局域网或已配置私有网络。只接受 HTTPS，以明确配对的叶证书 SHA-256 和有效期确认电脑身份，同时只允许配对地址中的主机，拒绝重定向或任意主机回退。本地证书不依赖公共 CA 或 IP 的 SAN，不能把这种固定证书身份校验称为公共 CA 域名认证；电脑换证书后需要重新配对。

任务支持列表与详情、刷新、按任务保留输入草稿、明确发送、暂停、只读核对恢复、一次审批或回答，以及按需生成/采用简报和阶段审核。语音键盘输入仍是草稿，不自动发送。AI 审核、自动化验收和人工认可分别显示；接受命令的回执不等于模型工作已经完成。

每次操作先将请求编号和完整内容密封保存，再发送到电脑。超时、退出后恢复或查询返回 404 时只查原编号，不自动重发。若电脑仍返回未知结果，先在电脑核对，再明确点击“已在电脑核对，解除手机等待”：旧编号及回执会进入本机密封历史，解除等待不证明原操作成功，也不重发它。后续新动作仍受电脑当前状态及 ETag 检查约束。卸载、清除应用数据或丢失 Keystore 密钥会失去本机记录；须先在电脑核对在途操作。

连接凭据、草稿和回执使用 Android Keystore 的 AES-GCM 密封，保存在应用私有的禁止备份目录；应用禁用云端与设备迁移备份及界面截屏。唯一声明权限为 `INTERNET`。此设计不能防御已被完全控制的手机或电脑。

## Offline Windows build

需要现有 Python 3.10+、JDK 17+、Android Platform 35 和 Build Tools 35；脚本不会下载、安装或修改已有 SDK。构建不需要 Gradle。使用自己的工具路径：

```powershell
python build.py --jdk C:\path\to\jdk-17 --android-jar C:\Android\Sdk\platforms\android-35\android.jar --build-tools C:\Android\Sdk\build-tools\35.0.0
```

已有同结构缓存时也可传 `--sdk-cache PATH`；只读取其 `platform/**/android.jar` 和 `build-tools/**/aapt2.exe`。构建在临时目录采用相对资源路径，避免部分 Windows Android 工具无法读取中文绝对路径的问题。

输出 `build/context-relay.apk`、SHA-256 和 `build-report.json`。脚本在仓库外 `%LOCALAPPDATA%/ContextRelayAndroid/signing/local-debug.p12` 创建独立的本地测试签名身份；它使用公开的测试口令，不是应用商店或正式发行身份。也可用 `--keystore` 指定仓库外的同格式测试密钥。升级 APK 必须使用相同签名；请勿提交或发布密钥、配对内容、设备数据和构建临时文件。

构建会执行 `ProtocolCheck` 的主机 JVM 断言，并检查 APK 签名、包信息和权限。这些检查不等于 Android 运行时、Android 真机、跨网络或视觉验收。模拟器和真机验证分别在项目验收记录中报告。
