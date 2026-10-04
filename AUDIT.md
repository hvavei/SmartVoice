# SmartVoice 3.0.0 产品化验收

## SmartVoice 3.1.0 本轮交付
- 最终文本工具条使用五列等权Grid，最小窗口实测五个按钮同一行、等宽铺满；时间轴已合并到“当前角色”信息行，外部说明文字已移除。
- Logo 使用用户提供的图片源文件（用户给出的两个路径其一不存在，以实际存在者为准），重新生成无外框/无人工描边图标。
- Logo 源图已生成 `assets/smartvoice.ico/png`，嵌入主程序、窗口和安装包。
- Logo 源图更新（蓝紫人像+耳机+声波）：`prepare_branding.py` 重写为多尺寸专用管线，16/24/32 小尺寸锐化、全档与源图逐像素一致；新增安装向导侧栏图 `wizard-image.bmp`/`wizard-small.bmp` 并接入 Inno Setup。
- 底部按钮改为：导入文档、合成/取消、播放/停止、暂停/继续、重播、输出目录；删除重试和原暂停按钮。多人区改为角色重置/对话分行，移除全开/全关。
- 语速滑块将50～200%映射到物理0～100轨道，100%严格在中点；主列表代号列固定不允许拖拽，槽位人声显示居左且窄屏裁切不改变布局。
- 导入解析抽出为 `documents.py`；TXT/Markdown/PDF/SRT 导入时合并源文件固定宽度软换行（空行=段落边界，缩进/标记行/字幕序号原样保留），排版随文本框宽度重排，JSON/CSV/LRC/DOCX 保持原样；文本框双击打开独立编辑器，默认双击指令被阻止。
- 角色分配重写（2026-09-28）：叙述行绑定第一个启用槽位、引号内对话保留引号原样走默认人声（可用 `[角色名]` 显式指定），不再发明 `[对话]` 标签；`名字:` 前缀仅当命中槽位名或合法数字槽位（含时间防误伤）才识别为角色，`他说：`/`12:30`/URL 按普通文本处理。编辑器仅对已绑定角色着色，普通冒号行不再误报"未绑定"。新配置槽位0默认名与"角色重置"一致为"旁白"。
- 独立编辑窗口移除滚动条：Tk Text 自带鼠标滚轮与上下键滚动（光标移出视野自动跟随），编辑区占满整窗；主文本框改 `wrap=word`，软折行跟随窗口宽度。
- 死代码清理（2026-09-28）：删除生产零引用的 `concat_mp3`/`strip_id3v1`/`strip_id3v2`（音频拼接统一走 `prepare_playback_audio`）、`docutils.concat_audio_ffmpeg`/`normalize_audio_to_bytes`、`engine.VOICE_CACHE`、`synth_edge` 的 `out_path` 死参数与落盘分支、`extract_pdf_text` 死参 `max_pages`、未用导入（`engine.time`、`components.shutil`、`polyphone.APP_DIR`、`studio_gui.APP_DIR`、`test_product.zipfile`）及死文件 `preview_smartisan_theme.py`、`engines.json`；引擎归类三处重复收敛为 `storage.engine_kind`（`engine.kind_of` 委托），片段指纹校验提取为 `voice_tasks._is_segment_key`，`package_components.COMMON` 真正接线，魔法数600提为 `MAX_SYNTH_CHARS`；`snapshot()` 冗余嵌套 azure 判断、`load_json`/`azure_voice_metadata` 重复计算、`_signed_pct` 恒等默认参、`edge_text` 间接赋值等微清理；forward_server `Server` 头改为动态 `appmeta.VERSION`，`--server` azure 分支合并去重复判断，config.json 文案改为 settings.json，README 版本/安装包文件名/构建前置说明更新，构建时 `dist/_internal` 先清后拷消除陈旧文件漂移。保留项：`main.py` 安装检查中 pypdf/pypdfium2/edge_tts 导入是故意的可用性探测；on_refresh OpenAI/火山分支保持平铺可读。
- 全套测试累计98项通过（产品测试+历史回归；含本轮新增：合成/取消按钮语义、停止不作废序号、串行/并发部分输出收割、停止后部分成品导出、三菜单结构、编辑窗口右键去工具栏、描述色灰蓝+功能区黑字、区标题阴影对）。
- 交互与视觉收敛（2026-09-29）：
  ① 工具栏按钮改“合成/取消”：运行中再点 `on_stop` 不再 `_preempt` 作废序号，只取消任务令牌并置 `_stopping`；`_synth_jobs` 捕获取消后返回含 None 的部分结果（串行 break、并发收割在途完成段），各任务 work 过滤空段——有已完成段照常拼接导出（状态“已停止 · 已输出完成片段”），全空则 `_cancelled_done` 安全收尾不弹失败框；`_progress_callbacks` 增加令牌检查（序号或令牌失效即抛 SynthesisCancelled），`_bg_run`/`_finish_fail` 带类型识别取消异常不弹错误框；`b_single` 不再置灰。
  ② `theme.py`：`FG` 改纯黑 `#000000`、`FEEDBACK` 改灰蓝 `#5b6b85`；`Status.TLabel`、`editor_role`/`play_time` 等动态描述统一 FEEDBACK。
  ③ 新增 `_title_widget` 阴影标题（1px 灰底 + 主字）应用于朗读引擎/调节风格/转发服务/人声/文本/多人配音六处；`_refont_menus` 让缩放同步菜单字号（`_menu_font` = 功能区标题字号）。
  ④ 顶栏菜单重构为 项目选项（含重试未完成片段、清除试听缓存）/增强组件/关于我们 三组，删除“编辑”“任务”菜单。
  ⑤ 编辑操作全部集成到文本右键（通用 `_bind_text_context_menu(widget, editor)`，主窗口保留独立编辑入口，编辑窗口去掉工具栏按钮）。
  ⑥ 修复隐藏问题：`_reflow` 对 LabelFrame 子控件统一 grid 会顶掉 `labelwidget`（Tcl 在 grid 后清空该选项），现按 labelwidget 跳过重排——这是区标题阴影框上线后暴露的真实布局缺陷。
  ⑦ `Cancellation.cancelled` 属性新增；README/RELEASE_NOTES 文案随菜单与取消语义更新。
  ⑧ 阴影标题首版用 place 叠字 → 父容器请求尺寸不计 place 子控件、塌缩为1×1，全部功能区标题不可见（实测 Tk9 place 不参与 winfo_req 尺寸）；改为同格 grid 叠放（pady=1 露底部阴影、后建主字盖上层），测试增加映射+高度>8 断言防回归，150% 缩放复验正常。
  ⑨ 深度筛查与菜单精简（2026-09-30，取代④的菜单命名与两条任务入口）：
  - 菜单：三入口改 选项/组件/关于；删除“重试未完成片段”“清除试听缓存”（`retry_task` 随之删除——全仓零调用，复用语义由“失败后再次合成自动复用”承担）；`components` 缺组件提示改指“组件 → 管理组件”，README/RELEASE_NOTES 同步。
  - 工具栏“输出目录”改为指向合成文件：有成品时 `explorer /select,` 定位选中，无成品回退打开输出目录。
  - 导出设置贯穿修复（原可致关不掉窗口/配置停写）：新增 `cfg_int/cfg_bool` 入口钳制（rate/vol/pitch/zoom/留白/格式读入坏值回退）；新增 `_export_options_safe()` 永不抛异常的快照，供 `_save_cfg`、`_project_payload`、`_audition_voice` 使用，严格校验仅保留给导出设置“保存”与 `_begin_task`；导出设置对话框补 WM_DELETE_WINDOW——合法保留改动、非法恢复进入时可用值；`_apply_export_options` 对项目回填值钳位矫正；`open_project` 在动界面之前先把项目空输出目录回落默认，杜绝半应用状态。
  - `_save_cfg` 整体 try 兜底（控件不可读时跳过本次暂存）；`multidub` 随每次保存回写——settings.json 是全量覆盖写，原缺键会把迁移进来的多人配音槽位配置直接抹掉（test 断言由 assertNotIn 翻转为回写+9槽校验）。
  - `_bg_poll` 重构 try/finally：单条事件处理异常不再跳过 `_bg_kick`（原 TclError 直接 return 断链 → 进度冻结、done/fail 永久滞留）。
  - 子窗口与组件：`_build_ui` 重建时跳过 Toplevel（原会连带销毁编辑/导出/组件窗口）；组件窗口单例化防重复打开，`install_archive` 增加 cancelled 参数、解包循环可取消，关闭窗口在取消完成后自动退出；组件临时目录（unpack-/download-）超1小时残留自动清理。
  - 退出路径：`on_close` 在销毁前先 `_save_cfg()`（刚输入的Key/端口不再丢）、`_flush_cfg` 落盘；临时目录清理带3次重试（播放/写盘线程短暂占句柄不再抛 PermissionError 裸奔）。
  - 转发服务：GUI 绑定从 0.0.0.0 改 127.0.0.1（无鉴权合成接口不再暴露局域网，浏览器插件等本机调用不受影响，与 `run_server` 一致）；`_Handler.timeout=30`；POST 体 >1MB 直接 413、文本 >10万字 413；响应写异常（断连/超时）捕获不炸线程。
  - 片段缓存：`SegmentCache` 增加 2GB 上限（按 mtime 淘汰最旧、.audio/.json 成对删除，缺失片段下次合成自动重建）；meta 非 dict 防御；`load_project` 的 `namelist()` 提到循环外（原每段一次 O(n²)），缺 project.json 给明确中文报错（原裸 KeyError 天书弹窗）。
  - 死代码清理：`App.select`、`retry_task`、`_last_label`（只写）、`_rate_position_var`（只写）、`lab_port/ent_port` 属性名（控件保留）、`_dub_snapshot` 恒为0的 fallback 死分支与配套文案、`__import__('pathlib')`、根 bind 死 try；`_dub_all` 标注测试专用保留，`_dub_cfg_snapshot` 因 multidub 回写转正为生产代码。
  - 交互/一致性修复：角色默认名统一为 旁白/角色{i+1}（失焦补名与角色重置原为 角色{i}，与校验/同步不一致会致 `[角色X]` 对不上）；`_discard_pcm` 播放中先停 MCI 再删（原 Windows 下删除失败静默吞）；试听文件按名覆盖（原 unique_export 每次递增，cache\auditions 只增不减且有10000上限）；`_show_info_in_prog_zone` 提示定时器句柄化，连发不互相顶掉。
  - 其它加固：`engine.load_json` 损坏 JSON 落 .bak 备份再回退默认；ffmpeg 子进程 600 秒超时（原无超时可永久挂）；PDF 文本提取后尽早 `reader.close()` 释放整份字节；`importlib.util` 显式导入。
  - 评估后明确跳过（记录原因）：项目保存/打开移后台线程（异常路径回滚与测试改造风险大于收益，且半应用状态已由安全快照+入口校验兜底）；转发访问日志（本地工具、隐私面）；engine 四份重试骨架合并与线程局部 Session 进程级化（高风险，单列排期）；`engine.load_json` 收窄吞异常范围（DPAPI 解密失败会变启动崩溃）。
  - 测试：全套 105 项通过（新增：输出目录定位成品、坏导出设置不卡保存/关窗/对话框、项目空导出目录回落、_bg_poll 断链存活、段缓存容量淘汰、转发 body/文本上限与超时、multidub 回写、菜单断言更新；两个并行收割测试的 0.05s 时序假设加宽防机器负载抖动）；`--smoke-test` 13 项 PASS。
  ⑩ 数字签名（2026-09-30）：按 CSDN 免费签名方法的思路（自制根证书 → 本机信任 → 签名 → 分发根证书给其他电脑）自建签名链，工具全部换用微软现行方案：makecert → `New-SelfSignedCertificate`（前者已废弃），signcode 图形向导 → `signtool` 命令行，certmgr → `Import-Certificate`。`build_release.py` 新增：
  - `--self-signed`：查找/创建 `CN=SmartVoice (Self-Signed)`（CurrentUser\My，RSA2048/SHA256/5年，Subject 自带 Self-Signed 标识不冒充 CA 验证）→ 导入 CurrentUser\Root（免管理员，`signtool verify /pa` 本机通过）→ 导出 `release/SmartVoice-Signing-Root.cer` 供分发；`signature-status.txt` 区分 SELF-SIGNED / 正式证书 / UNSIGNED 三态并写明指纹。
  - `fetch_signtool`：从 nuget 钉版下载 Microsoft.Windows.SDK.BuildTools 10.0.28000.2705（21.3MB，SHA256=8bfdfb6c…29ed 校验），取包内 x64/signtool.exe（单文件无附带依赖）；签名请求但本机无 signtool 时自动下载。
  - 默认时间戳 `https://timestamp.digicert.com` → `http://timestamp.digicert.com`：新版 SDK signtool 直接拒绝 https RFC3161 URL（实测 Invalid Timestamp URL），http 为 RFC3161 标准做法，对所有签名路径生效。
  - 验证：双 EXE + 双安装包 `Successfully verified`，`Get-AuthenticodeSignature` = Valid / CN=SmartVoice (Self-Signed)，双包 `verify_installer` `"ok": true`，全套 105 项 + 冒烟 13 项通过。
  - 透明度取舍：文章 2.2.4 是“偷偷”给用户装根证书，本项目按 README 既有原则改为透明分发——README 写明信任步骤、局限（仅改善发布者显示，SmartScreen 联网信誉不解决、不冒充受信发行者）与正式证书无缝替换方式；自签不宣称等同购买证书。
  - 仍待用户本人：正式可信签名（Azure Trusted Signing / SignPath 开源计划 / OV/EV 厂商证书）需购买或身份验证，机器侧已无可挖证书（证书库、PFX、signtool 均为空，已实查）。
  ⑪ 四路深扫与隐藏缺陷修复（2026-09-30）：
  - 高危×2：`docutils._ocr_pages` 组件激活顺序——必须先 `create_ocr()` 再 `import numpy`（标准版 numpy 随 OCR 组件分发，原顺序必然 ModuleNotFoundError，PDF OCR 永远不可用）；`_synth_jobs` 缓存写失败判死整单任务——`save` 包 OSError 落诊断事件，已合成段照常返回。
  - `voice_tasks`：Cancellation.register 先注销再 close；Diagnostics.finish 日志保留最近200份；fingerprint 条件加 annotate 键（启用注音才失效旧缓存，未启用保持历史指纹不失效）；SegmentCache._prune 按文件分别记账并回收孤儿 .json/超时 tmp；save_project 流式写 `.tmp`+原子替换、1GB 上限、过滤非法指纹（自产项目必可回读）、返回缺失段数（保存处提示“合成后自动补齐”）；load_project 类型闸门前置——engine_settings/parameters/person/exports 类型全在动 UI 之前 raise，杜绝畸形项目半应用状态。
  - `storage`：atomic_bytes finally 不掩盖原始异常；read_json utf-8-sig；unlock_settings 补 TypeError；migrate_legacy 改为闸门最后写=提交标记（中途失败可安全重试）、清旧明文尽力而为不阻断、存在明文 Key 且无 remember_key 时强制置 true 防 Key 被销毁；unique_export 被占用换序号（PermissionError→continue 而非导出失败）。
  - `studio_gui`：局部试听段不进项目缓存清单（原污染项目保存）；`_fail` 仅无活动任务时清进度（校验弹窗不再撤销正在跑的进度条）；`_finish_done` 异常先收尾任务；配置保存失败在任务运行中追加提示、不覆盖任务状态；旧任务 done 丢弃给状态提示；播放失败同步清 `_last_pcm` 悬挂引用；输出目录/打开数据目录异常保护；文本右键菜单挂 widget 名下随控件销毁（原挂 root，独立编辑窗关闭后泄漏）；角色名读取三处统一 `[:12]` 截断与 `_valid_dub_cfg` 对齐；`toggle_server` 任务运行中守卫。
  - `product_ui`：`_confirm_discard` 整体 try（异常按“有修改”处理，窗口永远关得掉）；`_project_payload` 参数逐键容错（单个坏 IntVar 不卡保存/比对）；save_project 缺失段数提示；open_project 先关独立编辑窗再 `_build_ui`（PeerText 不再持有被销毁的 text peer）；导出设置对话框单例化；试听缓存清理 unlink 容错；replay/打开成品在文件缺失与播放失败时明确提示。
  - `engine`：load_json utf-8-sig（带 BOM 配置不再被当坏 JSON 落 .bak 回退）；Edge 错误标签带真实异常类型与摘要（原一律标“超时”掩盖 403/重置等真因）；azure_voice_metadata 写盘前后双清防旧元数据回填；`_read_response_bytes` 256MB 上限防异常响应无限吃内存。
  - `main`：`--server` 改为 person→人声ID 解析（原 `voice_id` 死键恒用默认人声）；端口非整数/被占用友好退出；冒烟补 server_close。
  - `forward_server`：`/voices` 调用失败返回 502 JSON（原异常直接断连无响应）。
  - `components`：临时目录清理移入锁内不与解包/替换临界区并发；activate 失败回滚 sys.path/DLL 句柄不留半套状态；激活成功回收同名旧版本组件目录（G2PW 升级残留约 700MB）。
  - `prepare_models`：下载/解包写 `.part` 后原子替换+必需文件断言（中断不再留下被 exists() 误认为完整的半套模型）。
  - `documents`：无 BOM UTF-16 端序启发（NUL 落位奇偶判定 LE/BE，≥5%占比且≥70%集中同侧才判）；U+2028/U+2029/\x85/\x0b/\x0c 行分隔符归一为 \n。
  - 构建与验收：编译器/signtool 解包完成打 `.ok` 标记（无标记视为残留整目录重来），signtool 单文件 .part+原子替换；dist `_internal` 改名-替换-失败回滚（任一中断 dist 都可用上一版恢复）；verify_installer finally 卸载改 check=False（不掩盖真正断言错误、半装无卸载器跳过），安装超时 180→300s（Full 解包在高负载下临界）；diagnose_azure 对 r.json 补结构/类型防护。
  - 评估后明确跳过（记录原因）：双击“合成/取消”为按钮设计语义；stopping 期吞错会掩盖真 bug 不改；normalize_region 路径过滤（火山 appid 误伤）；fix_endpoint 覆盖自定义 host（测试固化）；端点 query 指纹与 safe_url 隐私张力（保留现状+测试固化）；项目 sha 自签（本地工具威胁模型）；长路径需单独设计；Edge synth 内部超时行为未核实不动；退出卡死按有界重试处理。
  - 测试：全套 116 项（+11：OCR 激活顺序、缓存写失败不判死、预览段不进清单、_fail 保留活动进度、save_project 缺失段数与无 .tmp 残留、load_project 类型闸门、无 BOM UTF-16 与分隔符归一、load_json BOM、unique_export 占用换号、/voices 502、响应字节上限）；`--smoke-test` 13 项 PASS；双包 verify `"ok": true`；四产物签名 Valid。
  ⑫ 环境独立性验证与实证（2026-09-30）：
  - 依赖清单：新增 `requirements.txt`（含 10 个直接依赖 + pyinstaller==6.22.3 构建依赖，钉版）。
  - 干净 venv 构建：在全新隔离 Python 3.14 venv 下 `pip install -r requirements.txt` 成功；`prepare_models.py` 联网下载资产；`build_release.py --fetch-compiler --self-signed` 全量重编，双包 `verify_installer` `"ok": true`。
  - 零数据首启验证：重定向 `SMARTVOICE_DATA_DIR` 到空临时目录，源码与 dist 冒烟全过（13 PASS），GUI 顺利完成首次建档（写入 settings/credentials/cache）并显示 `SmartVoice 3.1.0`。
  - 模块解耦：`polyphone.py` 模块导入安全重构（缺组件时不崩溃，调用时 `_load_model` 报缺资产）；6 个测试增加 `components.available` 显式 patch；3 个真实推理测试增加 `@requires_models` 跳过守卫。
  - 脱敏：`AUDIT.md` 个人盘符路径全量脱敏，仓内仅保留 Windows 系统/标准提供程序路径（`C:/Windows/Fonts`、`Cert:\`）与 GitHub/127.0.0.1 标准地址；全仓无硬编码用户名或本机专属路径。
  - 诚实边界：独立性仅在 Windows 11 x64（本机 + venv + 隔离数据目录）通过自动化测试与安装验收；未在其他物理 PC/Windows 10 实机验证；自签证书在其他电脑需导入根证书；合成服务依然依赖网络连通性。
  ⑬ 配置输入框右键与 Key 保存修复（2026-10-04）：
  - 输入框右键：全仓仅 Text 有 `<Button-3>` 菜单，Key/Region/终结点/端口/角色名/导出设置共 17 个 Entry 原生无右键（鼠标无法粘贴 Key）——新增通用 `_bind_entry_context_menu(entry)`（剪切/复制/粘贴/删除/全选，菜单挂控件名下随销毁），4 处接线全覆盖。
  - Key 保存：实证保存链路正常——未记住时 Key 被 strip 系安全设计（默认不落盘），记住开启时 DPAPI vault 回写/重载正常；真正的缺口是输入无 trace（仅离散动作与关窗触发保存）——key/region/ep/port 加 `trace_add` 输入即暂存（`_save_cfg` 内已有 350ms 去抖），`_save_cfg` 内 region 回写加值比守卫防 trace 自递归；未记住且 Key 非空时状态栏一次性提示「仅本次有效」；README 同步说明。
  - 垃圾代码/框架核查：全仓无 TODO/FIXME/打印调试残留；无 eval/exec/shell=True（构建调 powershell 用列表形参）；CLI 全链路冒烟覆盖；配置读写全部走 LOCALAPPDATA（可 SMARTVOICE_DATA_DIR 重定向），安装包不带 config/缓存/成品（verify 断言）。
  - 个人信息：盘符/UNC/机器名/用户名全仓零命中（仅保留 Cert:\、C:/Windows/Fonts 系统路径与 GitHub/127.0.0.1）。
  - 测试：新增 3 项（输入框右键存在、输入即暂存+提示、记住/未记住载荷）；全套 133 项 OK，冒烟 13 PASS。
- 标准包 `SmartVoice-Setup-Standard.exe` 不含 G2PW/OCR重组件；完整版 `SmartVoice-Setup-Full.exe` 包含全部组件。独立 `g2pw-3.1.0-cp314-win-amd64.zip`、`ocr-3.1.0-cp314-win-amd64.zip` 以SHA256清单供标准版按需下载/本地导入。组件URL指向 GitHub Release `components-v3.1.0`，在该Release创建前下载会明确提示未上传。
- 标准/完整版安装验收均通过：应用启动、G2PW/FFmpeg/OCR/DPAPI/项目缓存；标准版额外安装两个组件后完整检查通过；升级和卸载保留用户数据。
- 当前 `release/signature-status.txt` 为 `SELF-SIGNED: CN=SmartVoice (Self-Signed) ...`，四个产物 sign+verify 全过（见⑩）；未使用任何第三方证书冒充受信发行者。

## ⑬ 稳定性与解析性能复核（2026-10-02）
- 复核筛查报告后纠正误报：Edge 已有进度回调；配置读取已自动解密；签名扩展属性实测为 ObjectId；内置人声表、filedialog、Tk 的 _tclCommands 和冒烟 Session 替身均有用途。保留既有菜单、取消语义和未注音缓存指纹兼容性。
- 导出：修正本轮中间修改导致的成功导出临时硬链接残留；新增写入/fsync 失败的临时文件清理。DPAPI 输出缓冲在异常路径也进入释放流程。
- 文本：对所有候选编码检查 NUL，拒绝误当文本的含零字节输入；英文逗号/分号/冒号后的软换行补空格，同时保留连字符和开括号拼接语义。PDF 页面树异常也关闭 reader；OCR 单页失败带页号明确报错，不静默丢页。
- 网络：OpenAI 即使没有进度回调也走有上限的流式读取并关闭响应；转发响应显式 Connection: close；重复取消不重复遍历已经取出的响应集合。CLI 端口在解析后校验范围，冒烟 HTTP 服务在异常路径也释放并恢复原始描述符。
- 组件：重新导入使用旧目录备份、替换、原子指针提交；失败回滚，已激活组件拒绝破坏性覆盖并提示重启。保留 OCR 包的公共依赖源码，安装验收改为 OCR 先独立装验，再装 G2PW。
- 编辑器：角色高亮按标签批量提交，每批最多256段；文稿/角色未变时移动光标复用结果；不采用超5万字关闭高亮的降级方案。定时回调防护销毁控件，进度条重绘缓存包含 Canvas 身份并支持控件重建；布局不再嵌套 update_idletasks。
- G2PW：分词映射改为单次正则遍历，消除反复复制剩余字符串；删除 _truncate 未使用的偏移列表计算。移除无入口的 clear_audition_cache 方法，不恢复已精简的菜单项。
- 构建：依赖目录拷贝失败时清理半份目录并恢复旧目录；验收覆盖含空格/中文路径与空用户数据目录。
- 回归：隔离 venv 中130项测试全部通过（97.272秒），源码及冻结EXE冒烟各13项PASS；新增 test_stability.py 覆盖异常清理、组件修复/回滚、OCR失败提示、大文本批量刷新及Canvas重建。
- 发布验收：隔离 venv 干净重编成功；Standard 初始回退、单独安装OCR、再安装G2PW逐步检查通过，Full 内置模型检查通过，双包均 `ok: true`，升级/卸载保留数据。双EXE和双安装包签名均Valid（自签）；空用户数据目录下GUI标题正确并能正常关闭。以上为当前Windows环境验证，不等同跨设备或在线服务端到端实测。

## ⑭ 两套主题与界面打磨（2026-10-04）
- 两套主题：暖白·初（按 AUDIT 描述还原最早黑字搭配——中性浅底 + 纯黑功能字 + 灰蓝描述；早年精确色值已不在仓库，属忠实重建）、雾蓝（浅冷蓝）；“选项 → 主题”一键切换，配置持久化，重启保持。墨夜深色已按评审删除（clam 对只读下拉框字段底写死浅钢蓝色，像素级取证确认样式映射无法覆盖；旧配置残留 theme=night 自动回落默认）。
- 机制：`theme.py` 改为调色板字典 + `apply()`（刷新模块属性并同步已 from-import 的业务模块命名空间）；切换=同步调色板→关闭已开子窗口（编辑器内容 peer 共享无丢失）→ `_build_ui()` 重建→保存配置；任务运行中拒绝切换。
- 打磨：8 处暖色硬编码收敛进调色板（按钮悬停、表头、进度轨道、开关、描边）；右键菜单/顶栏子菜单跟随主题；角色高亮与未绑定警示色按主题走；全套正文/描述对比度 ≥4.5（测试断言）。
- 已知限制：clam 对只读下拉框字段硬编码浅色渲染（状态映射压不住），深色下字段偏亮但文字可读，属 Tk 行为；顶栏菜单条保持系统样式。
- 测试：新增 2 项（调色板合法性+对比度、切换重建+落盘）；两套截屏实证可用；全套 135 项 OK，冒烟 13 PASS；既有暖白断言（FG=#000000）不受影响，默认主题仍为暖白·初。

## 本次交付范围
- 主窗口/程序/安装包品牌改为 SmartVoice 3.0.0；正式仓库目标 https://github.com/hvavei/SmartVoice 。
- 独立音色能力策略：VoiceType 只展示类别，StyleList/RolePlayList 控制可选值，SSML prosody/phoneme 按模型族策略判断。未知能力保守禁用非默认参数；多人任务逐音色校验，错误包含角色名，不静默降级/换声。
- `voice_tasks.py` 管理取消令牌、网络响应释放、参数指纹、落盘分段缓存及诊断。成功段即保存；再次合成/失败重试只请求无有效缓存的段，参数/账户/端点作用域变化不误命中。
- 网络取消停止提交后续请求，并尝试关闭响应和中断重试退避。尚未返回首包的阻塞连接仍受 requests 网络超时约束，不宣称瞬时强制中断。
- 单个前台合成任务互斥；“合成/取消”运行中再点即停止提交新片段、已合成片段照常拼接输出（按钮不再置灰）。退出确认未完成任务和未保存项目。
- 项目保存为 .smartvoice ZIP：原稿、角色、人声、参数、区域/去凭据端点、导出偏好、片段指纹/成功音频、导出记录。软件设置单独存储，不把Key/Token写进项目。
- 文本编辑使用Tk撤销栈，支持重做；独立大窗口通过Text peer共享正文/撤销栈。角色标记高亮、未绑定提示、字数/段数/当前角色提示；当前行和选中内容试听；分行不修改原有槽位绑定。
- 播放暂停/继续/重播、位置/总时长；MP3/WAV、自定义文件名/目录、留白0～5000ms、可选-18 LUFS响度均衡。整条均衡改变整体增益与动态范围，不承诺将每句话压成完全等响。
- 导出同目录写完整临时文件再原子发布，不覆盖已有同名作品；Windows无硬链接文件系统用不覆盖rename回退。
- `storage.py` 统一 %LOCALAPPDATA%/SmartVoice 设置、凭据、日志、缓存、项目和作品目录；DPAPI保护当前Windows用户的Key。程序安装在 Programs/SmartVoice。
- 旧配置在首次成功迁移后清除其明文Key，保留旧作品位置；不删除项目片段/用户成品（“清除试听缓存”入口已随 3.1.0 菜单精简移除，试听缓存改同名覆盖+参数失效自动管理）。
- 诊断入口复制白名单字段：版本、引擎/区域、人声ID、字数/段数、阶段耗时、HTTP状态、错误类型、请求ID；原文和音频需导出报告时分别主动选择，不自动上传。
- 反馈入口接GitHub Issues；更新检查接GitHub Releases/latest，仅发现新版后引导打开正式发布页，不自动执行下载程序。本次接口返回404，用户可见“尚无公开版本/仓库不可公开访问”。

## 验证
- 完整70项源码回归通过，随后新增文件系统回退、后台完整导出、端点端口/查询参数指纹及HTTP诊断测试分别通过（当前共73项）。
- 实际安装包验收通过：G2PW模型推理、FFmpeg导出/PCM、PDF/OCR推理、网络适配器导入、DPAPI、项目与缓存往返、升级/卸载保留用户数据。
- 最终重打包后再次验收通过，报告位于临时目录 installer-check-jew0n2ys；主窗口标题为 SmartVoice 3.0.0，响应正常，本机一次窗口出现测量约1.218秒。Authenticode实查状态为NotSigned。
- 本轮不把“取消响应”宣称为能终止Azure服务端计费任务；成功已返回的片段复用可避免重试时重复请求。

## 发布待办（明确未完成）
- 正式可信代码签名证书仍需本人购买或完成身份验证（Azure Trusted Signing、SignPath 开源计划或 OV/EV 厂商证书），本机无法代办。自签链已落地（--self-signed，见⑩），拿到正式证书后 `--pfx`/`--certificate-thumbprint` 直接替换，签名密码仅从环境变量读取。
- 代码和安装包尚未推送到GitHub，也没有创建Release；仓库上线时应发布新安装包作为Release资产，不能上传本地配置、缓存和用户音频（已新增.gitignore）。
- 系统性语音听感、各模型族SSML兼容性与跨机器网络表现仍需更多真实样本验收。

---

# 2026-09-19 响应速度与任务进度审查

## 独立安装包、启动与 Azure 音色来源复查
- 用户确认交付目标为独立安装文件。改用 PyInstaller onedir + Inno Setup：`release/AzureTTSStudio-Setup.exe` 是唯一安装入口；日常运行不重复解压大模型。
- 同机同样的窗口检测方法（500ms轮询）测得：原 onefile 12.966s，新版首次3.870s，再次1.670s。样本受磁盘缓存/杀毒/机器负载影响，不作为所有用户的速度承诺。
- 网络库延迟至首次网络操作加载；启动不导入 requests/numpy/onnxruntime/tokenizers/cv2。模型和 OCR 按需加载。
- 本轮实际请求 `https://eastasia.tts.speech.microsoft.com/cognitiveservices/voices/list`，HTTP 200；返回691项，VoiceType计数为644 Neural、47 NeuralHD。当地Azure区域缓存和旧通用缓存的691个ID全部在官方结果中，未发现额外ID。
- `zh-CN-Lan:MAI-Voice-2-Flash` 为官方 Neural/Preview，`zh-CN-Lan:MAI-Voice-2` 为官方 NeuralHD/Preview；不是因为没有Neural后缀就属于“非神经语音”。本轮没有发现外部注入音色的证据，但不能据此保证历史缓存从未被编辑。
- Azure列表刷新保留 VoiceType、Status、Locale、采样率、风格/角色能力及获取地址/时间；主界面状态栏可显示官方类型。来源数据位于该区域 `voices_metadata_<region>.json`。
- Azure列表请求关闭Response，修复可重试状态的退避；拒绝空/错误结构覆盖原缓存。取消跨区域使用无来源通用缓存的回退。Edge/OpenAI/火山仍独立读取自己的缓存。
- 修复安装包 OCR 动态裸导入导致 TextDetector 缺失的问题，明确加载检测/识别/分类模块，并将PDF位图转换为OCR可接受的BGR数组。
- `build_release.py` 从干净运行目录生成安装包，仅复制主程序、_internal与README，不打入开发者Key/配置、缓存或音频；编译器下载固定版本并校验SHA256。
- `verify_installer.py` 在专用临时目录实际完成安装、离线功能验收、覆盖升级及卸载。确认未携带个人配置，升级/卸载保留用户配置和output。
- 源码52项回归通过；安装版另完成真实 G2PW推理、FFmpeg导出、OCR空白图推理及网络适配器导入，结果全部通过。
- 安装包未进行代码签名；不同Windows机器兼容性和大规模用户场景仍需后续实际验收。本轮未承诺消除云端延迟或所有发音偏差。

## Azure 真实联网复查（外部播放器也吞字）
- 实际使用 eastasia，当前人声为 `zh-CN-Lan:MAI-Voice-2-Flash`；配置存在可用 Key，环境代理启用。诊断没有打印 Key/代理地址。
- DNS 约 5–8ms；首次对照中 TLS 约 224–512ms（后续一次为 1550ms）。未认证 HEAD 返回 404，只证明端点可达；真实 POST 均为 HTTP 200。
- 原 MAI 原生短句：首包约 0.8–1.4s，完整返回约 6–16.5s；同文自动注音增加约 2.3s 本地准备时间。普通 XiaoxiaoNeural 的两次对照首包约 0.19–0.25s，完整返回约 1.9–7s。小样本结果有抖动，不能作为严格模型基准。
- 直连未稳定优于环境代理，未修改用户代理或区域。
- 修复：自动 G2PW 注音只用于普通 zh-CN Neural，带模型后缀音色使用原生文本；默认参数省略无意义 prosody，SSML 添加标准 synthesis 命名空间，动态属性转义。
- 标注器结果必须与原稿一致；越界/非汉字/无效音节不注入，保留原字。防止把缺失字符误当作播放问题继续补静音。
- 输出从 16kHz/128kbps 调为 24kHz/96kbps：比旧传输码率减少25%，与本地24kHz输出一致。诊断初始 A/B 使用48kbps，后续使用96kbps，因此不能拿两批完整下载时间直接计算优化百分比。
- 修改后 MAI 原生路径 SSML 构建约0.0–0.2ms，真实请求8.9s/13.6s：消除了本地模型准备成本，但云端/传输等待仍明显，不能宣称“龟速已完全解决”。
- 5份 MAI 原始输出通过 Azure zh-CN 识别对照，文字内容完整（标点有差异）；识别不能代替人工听音，也未复现用户的具体漏字句。仍需用户提供出问题原文、人声及输出音频。
- 新增 `diagnose_azure.py`：默认只测网络，`--live --output <目录>` 执行少量付费短句，`--direct` 比较直连，`--voice` 比较人声，`--transcribe --output <目录>` 对诊断音频做识别。报告仅输出白名单字段。
- 49项回归测试及离线冒烟测试通过。真实样本/报告保存在系统临时诊断目录。

## 本轮定位与修改
- `polyphone.py`：保留 G2PW，上下文窗口和目标位置一起作为缓存键；相同窗口去重批量推理，避免将缓存错误简化为“某字固定读音”。缓存上限 1024 个窗口。
- `engine.py`：Azure 请求按句优先切为最多 600 字的块，同一角色连续短行先合并；保留文本与角色顺序。每个网络线程独立复用 Session。
- `studio_gui.py`：Azure 最多两路在途请求，按输入序号整理结果，不能用网络完成先后顺序拼接。停止后不提交后续段，旧回调不会更新新任务。
- 去掉蓝色往返块和定时伪进度。绿色任务条的工作单位为“各合成段 + 整理音频 + 保存音频”。已知响应长度时，当前段接收比例计入填充；无长度时在段实际完成后推进。两路并发分别累计，不互相覆盖。
- 重试保留绿色填充的历史最高值，文字显示重试和当前字节数；因此重试期间填充可以暂时不增长。填充不是云端内部推理百分比，也不代表剩余时间比例。
- 拼接、首尾缓冲和保存阶段不再清零；保存完成后才填满。Canvas 复用图元，队列轮询每轮限制 100 条/约 8ms，避免消息淹没 Tk 事件处理。
- 角色编辑等高频配置变更合并 350ms 后由单独线程原子写入；退出刷新最后一份待保存配置。
- 文档解析/PDF OCR 移到后台，导入期间用户修改原稿时不覆盖新内容。

## 验证
- 44 项回归测试通过，包括并发上限、乱序完成后的原文顺序、原稿分块保真、进度不倒退、无蓝色图元、停止隔离、配置后台保存、导入竞争和 Windows 原生播放。
- `python -B main.py --smoke-test` 通过（模拟云端响应及本地 HTTP）。
- 本机 552 字重复句段样例首次多音字处理：修改前约 11.202s，修改后独立测量约 9.418s；同文缓存命中约 0.000014s。该样例含重复上下文，不能据此推算任意文稿/网络端到端提速比例。

## 仍需真实工作流确认
- 首次加载 BERT 和新上下文推理仍有明显 CPU 成本；没有绕开正式文稿的发音消歧来制造速度提升。
- 云端内部进度不可由接收字节精确反推。未知总长度的响应不会伪造绿色连续百分比。
- 两路并发的实际收益取决于 Azure 延迟/限流、文本长度及本地 CPU；本轮没有使用付费凭据进行联网性能对比。
- 已经进入的网络请求不能保证瞬时中断；在下一个阶段/接收回调或网络超时后释放。每个请求有超时限制。
- 此记录是已验证的改进范围，并不宣称每个功能已经达到唯一或绝对最优实现。

---

# 2026-09-17 历史审查记录（以下为当时状态）

## 本轮修复
- Edge 删除同音/近音字替换和首尾省略号，保持请求原文。
- Azure zh-CN 使用 SAPI 数字声调（如 `yin 2 hang 2`，轻声 `5`）。
- 删除任意 2～4 字切块注音和重复替换生成标签的逻辑；最长词优先单次匹配。
- 移除歧义词“朝阳”的固定读音。叠词边界不强制覆盖；不再每个标点追加固定停顿。
- OpenAI 请求接入界面模型字段；配置保存不再将模型名/AppID按区域名清洗。
- 切换引擎保存独立凭证与地址，避免把上一引擎的凭据带给下一引擎。
- CLI 转发仅对 Azure 纠正区域/地址，其他引擎保持自己的地址和默认音色。
- 拒绝 OpenAI HTML 成功页、火山非 JSON 响应被当音频保存。
- 后台进度通过队列回主线程；丢弃旧序号的结果和错误。
- 去掉模拟进度动画：已知长度显示接收比例，未知长度只显示已接收字节；多人显示完成段数。
- 退出取消 Tk 定时器并释放转发端口；修复 POST JSON 非对象造成异常。
- 删除弃用的自动角色分析空方法；修复英文成对引号，分行结果可重复处理。
- 未识别的冒号前缀保留原文，避免吞掉时间、URL等内容。

## 验证范围
- `python -m unittest test_regressions`：7项回归测试，含GUI凭证隔离、过期任务、SSML结构、轻声、叠词、格式化和错误响应。
- `python main.py --smoke-test`：14项离线/本地HTTP冒烟检查。
- Edge真实短句请求收到18000字节音频。未做人工听音评测，不能据此保证所有多音字正确。
- Azure/OpenAI/火山仅离线协议验证；本轮未使用真实付费凭据合成。
- Azure SAPI格式依据： https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-ssml-phonetic-sets#zh-cn

## 保留和待改进
- Tkinter是当前运行框架，未发现另一套弃用GUI框架；不为清理而更换技术栈。
- 火山v1地址未取得失效证据，保留现有适配器；不能称为已验证的豆包新模型通道。
- pypinyin用于字典音调格式转换，不是端到端语义消歧模型；不承诺100%正确。
- PDF仍为简易提取，无法可靠处理字体映射/扫描件，需后续换成熟解析器。
- MP3仍为字节拼接，尚未进行统一解码/重编码，不能保证所有播放器和混合采样率文件兼容。
- Azure流式读取过程的异常重试和响应释放还需完善。
- OpenAI本地无Key服务、自定义人声列表以及各引擎能力开关仍有适配空间。
- 当前占位提示仍插入Text控件、由读取函数排除，并非真正零字符覆盖层。

以上是本轮已覆盖范围和已知剩余项，不代表消除了全部潜在问题。
