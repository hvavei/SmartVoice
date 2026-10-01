NAME = 'SmartVoice'
VERSION = '3.1.0'
REPOSITORY = 'https://github.com/hvavei/SmartVoice'
FEEDBACK_URL = REPOSITORY + '/issues'
RELEASES_URL = REPOSITORY + '/releases'
UPDATE_API = 'https://api.github.com/repos/hvavei/SmartVoice/releases/latest'

RELEASE_NOTES = '''SmartVoice 3.1.0
• 用户Logo，合成与播放分离，语速100%居中、固定表格列边界。
• 角色重置、下拉文字居左，双击文本打开大窗口，保留导入分段。
• 标准安装包＋G2PW/OCR按需下载/离线导入，同时提供完整版。
• 按音色能力校验参数；区分官方类型与 SSML 能力。
• 分段持久缓存，失败后再次合成自动复用；“合成/取消”一键停止并输出已完成片段。
• 项目保存/恢复，撤销重做、分行撤销、局部试听；编辑/试听集成到文本右键。
• 顶栏精简为 选项/组件/关于；输出目录按钮定位最新成品；MP3/WAV、响度均衡。
• 稳定性加固：配置坏值兜底、转发服务仅本机访问、片段缓存自动限量、深扫修复隐藏缺陷。
• DPAPI 凭据保护、脱敏诊断、GitHub 反馈与更新检测。
'''
