SmartVoice 3.1.0
===============
项目：https://github.com/hvavei/SmartVoice
反馈：https://github.com/hvavei/SmartVoice/issues
更新：关于 → 检查更新（GitHub Releases）；没有公开版本时会明确提示。

安装：release\SmartVoice-Setup-Standard.exe（标准版）或 SmartVoice-Setup-Full.exe（含OCR/多音字完整版），均无需 Python。
程序默认安装到 %LOCALAPPDATA%\Programs\SmartVoice。
开发目录入口：dist\SmartVoice.exe，必须保留 _internal 依赖目录。
数字签名状态见 release\signature-status.txt；无正式证书时可 --self-signed 本机自签。
卸载：开始菜单 → SmartVoice → 卸载 SmartVoice，或安装目录中的 unins000.exe。
如果电脑上是旧版 AzureTTSStudio 免安装副本，请运行 Remove-Legacy-SmartVoice.bat；它只移除旧程序，不删除用户数据。

用户数据位于 %LOCALAPPDATA%\SmartVoice，可从“关于 → 打开用户数据目录”进入：
settings.json：软件设置、引擎配置、凭据引用和导出偏好。
credentials.json：当前 Windows 用户 DPAPI 保护的凭据；不是明文 Key。
cache\segments：按原文、人声、模型、参数指纹保存的完成片段；总量约2GB，超出自动清理最旧片段。
cache\auditions：试听文件缓存（同名覆盖不堆积）；参数变化后自动失效，无须手动清理。
logs：脱敏任务诊断；不含原文、Key/Token、服务端错误正文。
projects：默认项目保存位置（.smartvoice 项目包）。
exports：默认成品输出位置；可在导出设置中更改。
升级和卸载保留以上用户数据。旧 AzureTTSStudio 配置会在首次启动时迁移，
成功加密后移除旧配置中的明文凭证；旧音频不移动，导出偏好指向其原目录。

项目/编辑：
“选项 → 保存/打开”保留原稿、角色、人声、参数、已完成片段和导出记录。
项目不含账户凭证。换电脑需重新填写该电脑的凭证。
主编辑区支持 Ctrl+Z/Ctrl+Y；文本右键菜单含剪切/复制/粘贴/试听，
“独立大窗口编辑”共享原稿和撤销栈，其编辑操作也全部集成到右键菜单。
对话分行只做排版，不重置角色或音色；Ctrl+Z 可撤销。
角色标签用颜色标识，未绑定标签有提示；可试听当前行或选中文字。

合成与取消：
启动前逐一检查实际人声支持的风格/角色/参数。VoiceType 与 SSML 能力分开处理。
Azure 风格和角色来自官方元数据；未知模型族的高级参数保守置灰，可复位为默认。
成功片段落盘后才记为完成；失败后再次“合成”自动复用已落盘片段。
同文同参数再次合成也会复用；更换人声/参数/账户作用域自动变更指纹。
工具栏“合成/取消”一键发起或取消：取消立即停止提交后续片段，
已合成的片段照常拼接输出并可播放；同时尝试关闭已返回的响应，
连接首包等待仍受网络超时约束。
一个前台合成任务互斥运行；关闭时询问未完成任务和未保存项目。

播放/导出：
底部提供暂停/继续、重播和播放时间；“选项”菜单可打开刚生成的音频，
工具栏“输出目录”会在资源管理器中直接定位最新合成文件。
导出设置支持名称、目录、MP3/WAV、0～5000ms 首尾留白；直接关窗自动放弃非法修改。
成品以完整临时文件原子发布；同名自动增加编号，不覆盖已有作品。
可选响度均衡目标 -18 LUFS / -1.5 dBTP，会调整整体增益和动态范围，默认关闭。
本机播放使用同内容 PCM WAV；单击人声或缩放界面不打断播放。

转发服务：
仅监听本机 127.0.0.1（供浏览器插件等本机调用），支持 /voices 与 /forward；
请求体上限1MB、单次文本上限10万字、连接读写30秒超时；合成/人声列表失败返回结构化错误JSON（502/400）。

诊断：
关于 → 复制诊断信息：版本、引擎/区域、人声ID、字数/段数、阶段时间、HTTP状态/错误类型、请求ID。
关于 → 导出问题报告：默认脱敏；逐项主动确认后才附加原文或音频，不自动上传。

构建与签名：
首次构建前先运行 python prepare_models.py（下载G2PW推理资产）和 python prepare_branding.py（生成assets图标，需Pillow）。
python build_release.py --compiler "路径\ISCC.exe"
本机没有代码签名证书时加 --self-signed：自动创建 CN=SmartVoice (Self-Signed) 证书（当前用户、5年）、
装入本机受信任根证书、下载并钉版校验微软 SDK signtool（http RFC3161 时间戳），
对两个 EXE 和两个安装包签名并逐一 verify 通过；状态写入 release\signature-status.txt。
自签只在已信任它的电脑上显示发布者；其他电脑需先双击 release\SmartVoice-Signing-Root.cer，
安装到“受信任的根证书颁发机构（当前用户）”，SmartScreen 联网信誉提示不会因此消失，
自签不冒充受信发行者。正式发布用购买的证书：--certificate-thumbprint "证书指纹"
或 --pfx "路径\certificate.pfx"（密码通过 SMARTVOICE_SIGN_PASSWORD 环境变量提供）。
测试：python -B -m unittest test_regressions test_product -v
