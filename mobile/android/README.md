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

输出 `build/context-relay.apk`、SHA-256 和 `build-report.json`，其中包含公开的签名证书 SHA-256。正常构建使用仓库外 `%LOCALAPPDATA%/ContextRelayAndroid/release-signing/signing.dpapi`；可通过 `--signing-dir` 指定另一处本机私有目录。签名材料和强随机口令一并由 Windows 当前用户 DPAPI 密封，目录及文件 DACL 只授权当前用户、SYSTEM 与管理员。构建时在私有临时目录解密 PKCS12，结束清理；清理失败会报错。此保护不能阻止同一登录用户下的恶意程序、管理员或进程内存读取。

首次使用新版构建脚本，必须先明确处理签名身份。已有安装包必须迁移其原密钥，不能另建密钥，否则覆盖安装会被 Android 拒绝。对于旧版本生成的测试密钥，可在本目录执行下面的**一次迁移**；示例中的旧口令是旧构建器公开使用的测试值，不能继续当正式口令使用：

```powershell
$env:CR_LEGACY_SIGN_PASSWORD = 'android'
try {
    python build.py --jdk C:\path\to\jdk-17 --migrate-keystore "$env:LOCALAPPDATA\ContextRelayAndroid\signing\local-debug.p12" --legacy-password-env CR_LEGACY_SIGN_PASSWORD --signing-only
} finally {
    Remove-Item Env:CR_LEGACY_SIGN_PASSWORD -ErrorAction SilentlyContinue
}
```

其他来源的密钥应在指定环境变量中提供其实际口令，不写入命令行、仓库或日志。迁移核对原证书 DER、新口令密钥和密封文件回读结果；全部通过并清理临时文件后，才移除指定旧 PKCS12。失败保留旧密钥并拒绝构建；旧文件移除失败时需重试同一显式迁移，不能绕过错误。迁移不会更换原证书的名称或签名身份，也无法撤回之前已经复制出去的旧密钥。签名身份不等于应用商店审核。

**仅在从未发布、没有需要兼容的已有安装时**，使用 `python build.py --jdk C:\path\to\jdk-17 --initialize-signing --signing-only` 创建新身份。之后按上面的常规构建命令编译，无需再次迁移或初始化。新版不再接受 `--keystore` 或默认创建弱口令测试密钥。

请妥善保护 Windows 用户资料与签名密封文件。DPAPI 文件不是可以直接跨电脑导入的备份，丢失原账户的解密能力可能失去后续签名能力；脚本不实现签名密钥导出或恢复服务。不要提交或发布密钥、口令、配对内容、设备数据和临时文件。密码通过官方支持的 [`keytool` 环境变量参数](https://docs.oracle.com/en/java/javase/17/docs/specs/man/keytool.html) 与 [`apksigner` 的 `env:` 参数](https://developer.android.com/tools/apksigner) 传递，不放在子进程命令行中。

构建会执行 `ProtocolCheck` 的主机 JVM 断言，并检查 APK 签名、包信息和权限。这些检查不等于 Android 运行时、Android 真机、跨网络或视觉验收。模拟器和真机验证分别在项目验收记录中报告。
