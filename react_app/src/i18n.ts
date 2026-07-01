export type UiLocale = 'zh-CN' | 'en-US';

const SUPPORTED_LOCALES = new Set(['zh-CN', 'en-US']);

const TEXT_TRANSLATIONS: Record<string, string> = {
  '本地工作台已经就绪。': 'Local workspace is ready.',
  '默认权限': 'Default permissions',
  '完全访问权限': 'Full access',
  '项目配置权限': 'Project config access',
  '已切换到项目配置权限。': 'Switched to project config access.',
  '纸墨白': 'Paper Ink White',
  '鸢尾紫': 'Iris Purple',
  '冷静蓝': 'Calm Blue',
  '轻量绿': 'Light Green',
  '搜索入口还没接线，先直接在会话里描述需求会更快。': 'Search is not wired yet. Describe what you need in chat for now.',
  '插件能力还在准备中，当前先走本地工具链。': 'Plugins are still being prepared. Using the local toolchain for now.',
  '自动化入口后面再补，现在先把主链路跑通。': 'Automation will be added later. The main flow comes first.',
  '这个入口我先没动，当前还是直接在会话里描述需求。': 'This entry is not wired yet. Describe the request in chat for now.',
  '这里现在支持上传文件和图片了。': 'Files and images can now be uploaded here.',
  '已创建新项目。': 'New project created.',
  '已创建新对话。': 'New chat created.',
  '当前对话已清空。': 'Current chat cleared.',
  '对话已删除。': 'Chat deleted.',
  '已复制到剪贴板。': 'Copied to clipboard.',
  '已经放回输入框，可以修改后重发。': 'Moved back to the composer. Edit it and send again.',
  '已经放回输入框，直接发送就会重新生成。': 'Moved back to the composer. Send it to regenerate.',
  '这条消息没有可重发的原始输入。': 'This message has no original input to resend.',
  '先输入内容，或者至少带一个附件。': 'Type something, or attach at least one file.',
  '上一条还在处理中。': 'The previous message is still running.',
  '侧边栏已折叠。': 'Sidebar collapsed.',
  '主题颜色已更新。': 'Theme color updated.',
  '已开启本地服务提示。': 'Local service hints enabled.',
  '已关闭本地服务提示。': 'Local service hints disabled.',
  '设置已保存。': 'Settings saved.',
  'OpenAI API Key 已清除。': 'OpenAI API key cleared.',
  '附件已加入输入区。': 'Attachment added to the composer.',
  '附件已移除。': 'Attachment removed.',

  '自动': 'Auto',
  '关闭': 'Off',
  '低': 'Low',
  '中': 'Medium',
  '高': 'High',
  '快速': 'Fast',
  '视觉': 'Vision',
  '联网': 'Web',
  '推理': 'Reasoning',
  '工具': 'Tools',
  '工具调用': 'Tool call',
  '获取当前时间': 'Get current time',
  '列出项目文件': 'List project files',
  '读取文件': 'Read file',
  '列出目录': 'List directory',
  '执行本地命令': 'Run local command',
  '写入 stdin': 'Write stdin',
  '查看图片': 'View image',
  '重置 JS REPL': 'Reset JS REPL',
  'Shell 命令': 'Shell command',
  'Exec 命令': 'Exec command',
  '当前时间': 'Current time',

  '会话': 'Chats',
  '搜索': 'Search',
  '插件': 'Plugins',
  '自动化': 'Automation',
  '设置': 'Settings',
  '文件': 'File',
  '编辑': 'Edit',
  '查看': 'View',
  '窗口': 'Window',
  '帮助': 'Help',
  '我的项目': 'My Projects',
  '我的对话': 'My Chats',
  '新项目': 'New Project',
  '新对话': 'New Chat',
  '还没有项目，点右侧加号先建一个。': 'No projects yet. Use the plus button on the right to create one.',
  '这里还没有对话，点右侧加号新建。': 'No chats yet. Use the plus button on the right to create one.',
  '这个项目里还没有对话': 'No chats in this project yet',
  '项目菜单': 'Project menu',
  '固定项目': 'Pin project',
  '在资源管理器中打开': 'Open in File Explorer',
  '重命名项目': 'Rename project',
  '归档对话': 'Archive chats',
  '移除': 'Remove',
  '新建项目对话': 'New project chat',
  '正在回复': 'Responding',
  '删除对话': 'Delete chat',
  '切换侧栏': 'Toggle sidebar',
  '最小化': 'Minimize',
  '最大化': 'Maximize',
  '窗口控制': 'Window controls',
  '上下文工作台': 'Context Workbench',
  '正在连接本地调试后端...': 'Connecting to the local debug backend...',
  '重试': 'Retry',
  '需要在桌面端选择本地文件夹。': 'Choose a local folder in the desktop app.',
  '没有拿到文件夹路径。': 'No folder path was returned.',
  '没找到这条对话。': 'This chat was not found.',
  '项目已移除。': 'Project removed.',
  '项目已置顶。': 'Project pinned.',
  '项目已重命名。': 'Project renamed.',
  '这个项目没有绑定本地文件夹。': 'This project is not linked to a local folder.',
  '需要在桌面端打开资源管理器。': 'Open File Explorer from the desktop app.',
  '已打开项目所在父文件夹。': 'Opened the project parent folder.',
  '这个项目还没有可归档的对话。': 'This project has no chats to archive.',
  '项目对话已归档。': 'Project chats archived.',
  '没找到当前会话。': 'Current session was not found.',
  '没找到这条消息。': 'This message was not found.',
  '这条消息已经删掉了。': 'This message has been deleted.',
  '已停止本次回复。': 'Stopped this response.',
  '流式响应意外中断': 'Streaming response ended unexpectedly',

  '选择模型': 'Select model',
  '选择聊天模型': 'Select chat model',
  '这里展示所有已启用供应商里已经添加好的模型，点击后会直接切到对应供应商。': 'Shows models already added under enabled providers. Selecting one switches to that provider.',
  '当前会话': 'Current chat',
  '关闭模型选择': 'Close model picker',
  '搜索模型或供应商...': 'Search models or providers...',
  '个模型': 'models',
  '没有找到可选模型': 'No available models found',
  '先去供应商里启用一个供应商，并把模型添加进去，这里才会出现真正能用的聊天模型。': 'Enable a provider and add models first. Usable chat models will appear here.',

  '直接描述你要处理的事，Enter 发送，Shift + Enter 换行。': 'Describe what you want to do. Enter sends, Shift + Enter adds a line.',
  '停止回复': 'Stop response',
  '发送消息': 'Send message',
  '正在思考...': 'Thinking...',
  '思考完成': 'Thinking complete',
  '正在生成思考内容...': 'Generating reasoning...',
  '图片': 'Image',
  '打开': 'Open',
  '仅附件消息': 'Attachment-only message',
  '这条消息只带了附件。': 'This message only has attachments.',
  '运行命令': 'Run command',
  '已运行命令': 'Command run',
  '调用了 1 个工具': 'Used 1 tool',
  '命令已执行，但没有输出。': 'Command ran with no output.',
  '成功': 'Succeeded',
  '失败': 'Failed',

  '上下文地图': 'Context Map',
  '切换右侧侧边栏': 'Toggle right sidebar',
  '这里会显示本轮真正进入上下文的消息。': 'Messages that actually enter context this round will appear here.',

  '建议': 'Suggestions',
  '手动': 'Manual',
  '可用': 'Available',
  '预览': 'Preview',
  '未选择供应商': 'No provider selected',
  '删除': 'Delete',
  '替换': 'Replace',
  '压缩': 'Compress',
  '混合': 'Mixed',
  '更新': 'Update',
  '用户': 'User',
  '展开节点详情': 'Expand node details',
  '把一个或多个节点展开成完整内容和可编辑条目视图，再决定要不要编辑。': 'Expand nodes into full content and editable items before deciding whether to edit.',
  '删除单个条目': 'Delete one item',
  '删除某个节点里的一个条目。': 'Delete one item inside a node.',
  '替换单个条目': 'Replace one item',
  '把某个节点里的一个条目替换成新的内容。': 'Replace one item inside a node with new content.',
  '压缩单个条目': 'Compress one item',
  '把某个条目压缩成更短的版本，同时保留原来的条目类型。': 'Compress one item while keeping its item type.',
  '压缩节点': 'Compress nodes',
  '把一个或多个节点压缩成新的摘要节点，作用在当前工作快照上。': 'Compress one or more nodes into summary nodes in the working snapshot.',
  '删除节点': 'Delete nodes',
  '从当前工作快照里删除一个或多个节点。': 'Delete one or more nodes from the working snapshot.',
  '红色阈值必须大于黄色阈值': 'The red threshold must be greater than the yellow threshold.',
  '已保存，后面的上下文对话会使用这个模型。': 'Saved. Later context chats will use this model.',
  '没有可用会话': 'No available session',
  'Token 概览': 'Token Overview',
  '这里是token 统计，你可以直观看到token情况，再决定要不要去手动页处理。': 'Token statistics are shown here so you can decide whether to handle context manually.',
  '总 Token 数': 'Total Tokens',
  '这里和左侧上下文地图使用同一个 token 计数器，统计当前地图里的节点内容。': 'Uses the same counter as the context map to count node content.',
  '工具调用 Token': 'Tool Call Tokens',
  '这里按同一套计数器统计工具展示内容和工具输出占用的 token。': 'Counts tool display content and tool output with the same counter.',
  '当前聚焦': 'Current Focus',
  '全部': 'All',
  '当前没有单独选中节点，所以手动页会基于整份上下文来处理。': 'No nodes are selected, so the manual page will use the full context.',
  '节点 Token 明细': 'Node Token Details',
  '这里只显示 minimap 里的红色节点。': 'Only red nodes from the minimap are shown here.',
  '正在统计 Token...': 'Counting tokens...',
  '这一步会把主聊天当前真正会发给模型的上下文一起算进去。': 'This includes the context that the main chat would actually send to the model.',
  '当前没有红色节点': 'No red nodes right now',
  '当前还没有可统计的节点': 'No nodes to count yet',
  '等主聊天里有实际上下文之后，这里就会列出每个节点的 Token 数。': 'Once the main chat has real context, each node token count will appear here.',
  '可以直接整理当前上下文': 'You can organize the current context directly',
  '支持删除或压缩单节点、多节点、文本和工具调用结果；不确定能做什么，就直接问模型有哪些可用功能。': 'You can delete or compress single nodes, multiple nodes, text, and tool outputs. Ask the model what it can do if unsure.',
  '主聊天这一轮还没结束，右侧上下文工作区会等它先停下来。': 'The main chat is still running. The context workspace will wait for it to finish.',
  '直接问当前上下文哪里太长，或者哪些内容该保留...': 'Ask what is too long in the current context, or what should be kept...',
  '先进入一个会话，再在这里聊天...': 'Open a chat first, then talk here...',
  '清空上下文模型对话': 'Clear context model chat',
  '当前没有可清空的对话记录': 'No context model chat to clear',
  '初始版本': 'Initial version',
  '当前版本': 'Current version',
  '当前所在版本': 'Current version',
  '这次更新了当前上下文。': 'This update changed the current context.',
  '处理中...': 'Processing...',
  '工作区设置': 'Workspace Settings',
  '手动页模型': 'Manual Page Model',
  '右侧手动页会固定走这个模型，用来做上下文分析和编辑。': 'The manual page on the right uses this model for context analysis and edits.',
  '保存中...': 'Saving...',
  '保存设置': 'Save Settings',
  '当前工作区供应商：': 'Current workspace provider: ',
  'Token 颜色阈值': 'Token Color Thresholds',
  '设置 minimap 的绿色、黄色、红色分段。': 'Set the green, yellow, and red minimap ranges.',
  '黄色阈值': 'Yellow threshold',
  '红色阈值': 'Red threshold',
  '保存阈值': 'Save Thresholds',
  '当前工具能力': 'Current Tool Capabilities',
  '这些工具只服务当前上下文，不会去跑主任务。现在已经能做节点级查看，以及 item 级压缩、替换和删除。': 'These tools only affect current context, not the main task. They can inspect nodes and compress, replace, or delete items.',
  '选择工作区模型': 'Select workspace model',
  '这里选的是右侧上下文工作区自己的模型和供应商，不会跟主聊天模型混在一起。': 'This selects the context workspace model and provider, separate from the main chat model.',
  '当前工作区': 'Current workspace',

  '助手': 'Assistant',
  '我': 'Me',
  '外观': 'Appearance',
  '供应商': 'Providers',
  '关于': 'About',
  '更富有感情': 'More empathetic',
  '会更主动接住情绪，适合陪你把想法慢慢理顺。': 'More emotionally responsive, good for slowly sorting out thoughts.',
  '均衡的助手': 'Balanced assistant',
  '像一个可靠搭档，情绪、效率和判断力比较平衡。': 'A reliable partner with balanced emotion, efficiency, and judgment.',
  '更理性冷静': 'More rational',
  '更像一个安静的执行者，适合偏任务型的工作流。': 'A quieter executor for task-oriented workflows.',
  '简体中文': 'Simplified Chinese',
  '语言': 'Language',
  '输入语言': 'Choose language',
  '时区': 'Time zone',
  '输入时区': 'Choose time zone',
  '展开语言': 'Open language',
  '展开时区': 'Open time zone',
  '继续输入即可保留自定义值': 'Keep typing to preserve a custom value',
  '没有匹配项': 'No matches',
  '自定义主题': 'Custom theme',
  '只把默认强调换成白色，整体还是现在这套暗色界面。': 'Only changes the default accent to white; the dark interface stays the same.',
  '沿用当前主题配置，直接切换整体界面氛围。': 'Uses the current theme setup to switch the overall interface mood.',
  '从颜色选择器临时选出的强调色。': 'Accent color temporarily picked from the color selector.',
  '已导入主题配置': 'Theme configuration imported',
  '导入失败，只支持 JSON 或 #RRGGBB': 'Import failed. Only JSON or #RRGGBB is supported.',
  '主题配置已复制': 'Theme configuration copied',
  '复制失败，当前环境没有开放剪贴板权限': 'Copy failed. Clipboard permission is unavailable in this environment.',
  '助手名称': 'Assistant Name',
  '这一页直接把基础设置和提示词放在一起，不再做分页。': 'Basic settings and prompts are on one page.',
  '给助手起个名字': 'Name your assistant',
  '默认聊天模型': 'Default Chat Model',
  '未单独设置时，就按这里作为助手默认模型。': 'Used as the assistant default model when no separate model is set.',
  '生成设置': 'Generation Settings',
  '这些参数会真实进入模型请求；推理强度仍然由输入框控制。': 'These parameters are sent to the model request. Reasoning effort is still controlled in the composer.',
  '控制输出随机性，关闭时使用模型默认值': 'Controls output randomness. Off uses the model default.',
  '控制候选词采样范围，关闭时使用模型默认值': 'Controls candidate token sampling. Off uses the model default.',
  '上下文消息数量': 'Context Message Count',
  '只限制发给模型的历史消息，不影响侧边栏和聊天记录显示': 'Only limits history sent to the model; sidebar and chat history are unaffected.',
  '流式输出': 'Streaming Output',
  '关闭后等待完整回答再显示': 'When off, waits for the full response before showing it.',
  '提示词': 'Prompt',
  '写清楚助手的语气、角色、做事方式和边界。': 'Describe the assistant tone, role, workflow, and boundaries.',
  '你的称呼': 'Your name',
  '名字': 'Name',
  '聊天和个性化里显示的名字': 'Name shown in chat and personalization.',
  '应用界面的显示语言': 'Display language for the app interface.',
  '用于时间相关的默认上下文': 'Default context for time-related requests.',
  '关于你': 'About You',
  '写一些你的性格、偏好和说话方式，方便助手更贴着你回答。': 'Write your personality, preferences, and speaking style so the assistant can respond more naturally.',
  '主题': 'Theme',
  '使用浅色、深色，或匹配系统设置': 'Use light, dark, or system mode.',
  '主题模式': 'Theme mode',
  '浅色': 'Light',
  '深色': 'Dark',
  '系统': 'System',
  '深色主题': 'Dark Theme',
  '导入': 'Import',
  '复制主题': 'Copy Theme',
  '选择深色主题': 'Select dark theme',
  '强调色': 'Accent Color',
  '背景': 'Background',
  'UI 字体': 'UI Font',
  '代码字体': 'Code Font',
  '对比度': 'Contrast',
  'UI 字号': 'UI Font Size',
  '调整 Codex UI 使用的基准字号': 'Adjust the base font size used by the Codex UI.',
  '代码字体大小': 'Code Font Size',
  '调整聊天和差异视图中代码使用的基础字号': 'Adjust the base code font size used in chat and diff views.',
  '这里控制会发给模型的工具列表。保存后，OpenAI Responses、Chat Completions、Claude 和 Gemini 都会使用同一份开关。': 'Controls the tool list sent to models. After saving, OpenAI Responses, Chat Completions, Claude, and Gemini all use the same switches.',
  '已开启': 'Enabled',
  '保存工具设置': 'Save Tool Settings',
  '真正的上下文工程': 'Real context engineering',
  '信息': 'Info',
  '加入我们': 'Join Us',
  '配置你的助手和工作环境。': 'Configure your assistant and workspace.',
  '返回聊天': 'Back to Chat',
  '这里使用和主聊天框一样的模型选择器，切换后会更新设置里的默认聊天模型。': 'Uses the same model picker as the main chat composer. Switching here updates the default chat model in settings.',
  '设置默认': 'Set default',

  'OpenAI Responses 接口，主聊天的真实链路会优先走这一类。': 'OpenAI Responses API. The main chat path prefers this type.',
  'OpenAI 兼容的 /chat/completions 供应商，模型列表从 /models 拉取。': 'OpenAI-compatible /chat/completions provider. Models are fetched from /models.',
  'Google Gemini 接口，按 Gemini 的模型列表格式拉取。': 'Google Gemini API. Models are fetched in Gemini format.',
  'Anthropic Claude 接口，按 Claude 的模型列表格式拉取。': 'Anthropic Claude API. Models are fetched in Claude format.',
  '这个地址没有返回可添加的模型。': 'This endpoint returned no models that can be added.',
  '搜索供应商...': 'Search providers...',
  '筛选供应商': 'Filter providers',
  '没有匹配的供应商': 'No matching providers',
  '换个关键词，或者直接新建一个。': 'Try another keyword, or create one directly.',
  '供应商名称（可选）': 'Provider name (optional)',
  '添加供应商': 'Add Provider',
  '供应商名称': 'Provider name',
  '拉取中': 'Fetching',
  '删除当前供应商': 'Delete current provider',
  '删除供应商': 'Delete Provider',
  'API 密钥': 'API Key',
  '密钥只保存在本地，用来拉取模型列表和后续请求。': 'The key is stored locally and used to fetch models and make requests.',
  '隐藏 API 密钥': 'Hide API key',
  '显示 API 密钥': 'Show API key',
  '输入 API Key': 'Enter API key',
  '清除': 'Clear',
  '检测': 'Check',
  'API 地址': 'API URL',
  '这里填基础地址就行，会按接口类型自动拼出真实请求路径。': 'Enter the base URL. The real request path is built from the API type.',
  '预览：': 'Preview: ',
  '填写后这里会显示实际请求路径': 'The real request path will appear here after you fill this in',
  '模型': 'Models',
  '搜索已添加模型': 'Search added models',
  '清空全部': 'Clear All',
  '删除本组': 'Delete Group',
  '删除模型': 'Delete model',
  '还没有添加模型': 'No models added yet',
  '点“管理模型”后，会先拉远端列表，再由你手动把需要的模型加进来。': 'Use "Manage Models" to fetch the remote list, then manually add the models you need.',
  '管理模型': 'Manage Models',
  '添加模型': 'Add Model',
  '管理供应商模型': 'Manage provider models',
  '关闭模型管理': 'Close model manager',
  '搜索远端模型': 'Search remote models',
  '重新拉取': 'Fetch Again',
  '正在请求模型列表': 'Fetching model list',
  '这一步只会拿到候选模型，不会自动把整批模型全塞进供应商。': 'This only fetches candidate models. It will not add the whole list automatically.',
  '移除本组': 'Remove Group',
  '添加本组': 'Add Group',
  '添加': 'Add',
  '没有匹配的模型': 'No matching models',
  '换个关键词，或者重新拉取一次。': 'Try another keyword, or fetch again.',

  '执行一次性 PowerShell 命令，适合检查环境、运行测试和调试。': 'Run one-off PowerShell commands for environment checks, tests, and debugging.',
  '启动命令并返回输出；长时间运行的进程会给出 process_id。': 'Start a command and return output. Long-running processes provide a process_id.',
  '向 exec_command 启动的仍在运行的进程写入输入。': 'Write input to a still-running process started by exec_command.',
  '使用 Codex 风格 patch 修改、创建、删除或移动工作区文件。': 'Modify, create, delete, or move workspace files using Codex-style patches.',
  '按 Codex list_dir 风格列出目录内容，支持分页和递归深度。': 'List directory contents in Codex list_dir style, with pagination and recursive depth.',
  '读取工作区中的文本文件。': 'Read text files in the workspace.',
  '读取工作区图片并以 data URL 形式返回给模型。': 'Read workspace images and return them to the model as data URLs.',
  '在本地 Node.js kernel 中运行 JavaScript 片段。': 'Run JavaScript snippets in a local Node.js kernel.',
  '重置本地 JavaScript kernel。': 'Reset the local JavaScript kernel.',
  '获取指定时区的当前时间。': 'Get the current time for a specified time zone.',
};

const ATTRIBUTE_TRANSLATIONS: Record<string, string> = {
  '关闭': 'Close',
};

function applyDynamicTranslation(text: string): string {
  let output = text;

  output = output.replace(/本轮调用了 (\d+) 个工具。/g, 'This round used $1 tools.');
  output = output.replace(/调用了 (\d+) 个工具/g, 'Used $1 tools');
  output = output.replace(/已切换到 (.+?)。/g, (_, label: string) => `Switched to ${translatePhrase(label, 'en-US')}.`);
  output = output.replace(/模型已切换到 (.+?)。/g, 'Model switched to $1.');
  output = output.replace(/推理强度已切换到 (.+?)。/g, (_, label: string) => `Reasoning effort switched to ${translatePhrase(label, 'en-US')}.`);
  output = output.replace(/移除项目“(.+?)”会删除它下面的项目对话，确定继续吗？/g, 'Remove project "$1" and delete its project chats?');
  output = output.replace(/归档项目“(.+?)”下的 (\d+) 条对话？/g, 'Archive $2 chats under project "$1"?');
  output = output.replace(/读取附件失败：(.+)/g, 'Failed to read attachment: $1');
  output = output.replace(/附件：(.+)/g, 'Attachments: $1');
  output = output.replace(/退出码 (\d+) · 成功/g, 'Exit code $1 · Succeeded');
  output = output.replace(/退出码 (\d+) · 失败/g, 'Exit code $1 · Failed');
  output = output.replace(/(\d+) 次改动/g, '$1 changes');
  output = output.replace(/(\d+) 个节点/g, '$1 nodes');
  output = output.replace(/手动页会优先围绕节点 #(.+?) 来看。/g, 'The manual page will prioritize node #$1.');
  output = output.replace(/节点 #([\d /-]+)/g, 'Node #$1');
  output = output.replace(/选择第 (\d+) 个节点/g, 'Select node $1');
  output = output.replace(/跳转到主聊天第 (\d+) 条消息/g, 'Jump to message $1 in main chat');
  output = output.replace(/定位到第 (\d+) 个节点，约 ([\d,]+) 个 token/g, 'Locate node $1, about $2 tokens');
  output = output.replace(/工具调用 ([\d,]+) Token/g, 'Tool calls $1 tokens');
  output = output.replace(/([\d,]+) Token/g, '$1 tokens');
  output = output.replace(/(\d+) 个模型/g, '$1 models');
  output = output.replace(/删除模型 (.+)/g, 'Delete model $1');

  for (const [source, target] of COMMON_REPLACEMENTS) {
    output = output.replaceAll(source, target);
  }

  return output;
}

const COMMON_REPLACEMENTS: Array<[string, string]> = [
  ['我的项目', 'My Projects'],
  ['我的对话', 'My Chats'],
  ['新项目', 'New Project'],
  ['新对话', 'New Chat'],
  ['手动页会优先围绕', 'The manual page will prioritize'],
  [' 来看。', '.'],
  ['节点 #', 'Node #'],
  ['预览：', 'Preview: '],
  ['当前工作区供应商：', 'Current workspace provider: '],
];

const textNodeOriginals = new WeakMap<Text, string>();
const elementAttributeOriginals = new WeakMap<Element, Map<string, string>>();
const LOCALIZE_SKIP_SELECTOR = '[data-localize-skip="true"]';

export function normalizeSupportedLocale(locale: unknown): UiLocale {
  return typeof locale === 'string' && SUPPORTED_LOCALES.has(locale) ? (locale as UiLocale) : 'en-US';
}

export function isEnglishLocale(locale: unknown) {
  return normalizeSupportedLocale(locale) === 'en-US';
}

export function translatePhrase(value: string, locale: unknown): string {
  if (!isEnglishLocale(locale)) {
    return value;
  }

  const leading = value.match(/^\s*/)?.[0] || '';
  const trailing = value.match(/\s*$/)?.[0] || '';
  const trimmed = value.trim();
  const exact = TEXT_TRANSLATIONS[trimmed];
  return leading + (exact || applyDynamicTranslation(trimmed)) + trailing;
}

function translateAttributePhrase(value: string, locale: unknown): string {
  if (!isEnglishLocale(locale)) {
    return value;
  }

  const leading = value.match(/^\s*/)?.[0] || '';
  const trailing = value.match(/\s*$/)?.[0] || '';
  const trimmed = value.trim();
  return leading + (ATTRIBUTE_TRANSLATIONS[trimmed] || translatePhrase(trimmed, locale)) + trailing;
}

export function localizeUiText(value: string, locale: unknown): string {
  return translatePhrase(value, locale);
}

function translateTextNode(node: Text, locale: UiLocale) {
  if (!node.nodeValue || !node.nodeValue.trim()) {
    return;
  }
  if (node.parentElement?.closest(LOCALIZE_SKIP_SELECTOR)) {
    return;
  }

  const storedOriginal = textNodeOriginals.get(node);
  if (locale === 'zh-CN') {
    const translatedStoredOriginal = storedOriginal ? translatePhrase(storedOriginal, 'en-US') : '';
    if (storedOriginal && node.nodeValue === translatedStoredOriginal) {
      node.nodeValue = storedOriginal;
      return;
    }
    textNodeOriginals.set(node, node.nodeValue);
    return;
  }

  const expectedValue = storedOriginal
    ? translatePhrase(storedOriginal, locale)
    : '';
  const original = storedOriginal && node.nodeValue === expectedValue ? storedOriginal : node.nodeValue;

  if (original !== storedOriginal) {
    textNodeOriginals.set(node, original);
  }

  const nextValue = translatePhrase(original, locale);
  if (node.nodeValue !== nextValue) {
    node.nodeValue = nextValue;
  }
}

function translateAttributes(element: Element, locale: UiLocale) {
  if (element.closest(LOCALIZE_SKIP_SELECTOR)) {
    return;
  }

  const attributes = ['aria-label', 'placeholder', 'title'];
  const originals = elementAttributeOriginals.get(element) || new Map<string, string>();

  attributes.forEach((attribute) => {
    const current = element.getAttribute(attribute);
    if (!current || !current.trim()) {
      return;
    }

    const storedOriginal = originals.get(attribute);
    if (locale === 'zh-CN') {
      const translatedStoredOriginal = storedOriginal ? translateAttributePhrase(storedOriginal, 'en-US') : '';
      if (storedOriginal && current === translatedStoredOriginal) {
        element.setAttribute(attribute, storedOriginal);
        return;
      }
      originals.set(attribute, current);
      return;
    }

    const expectedValue = storedOriginal
      ? translateAttributePhrase(storedOriginal, locale)
      : '';
    const original = storedOriginal && current === expectedValue ? storedOriginal : current;

    originals.set(attribute, original);
    const nextValue = translateAttributePhrase(original, locale);
    if (current !== nextValue) {
      element.setAttribute(attribute, nextValue);
    }
  });

  if (originals.size) {
    elementAttributeOriginals.set(element, originals);
  }
}

function translateTree(root: ParentNode, locale: UiLocale) {
  if (root instanceof Element && root.closest(LOCALIZE_SKIP_SELECTOR)) {
    return;
  }

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  let current = walker.nextNode();

  while (current) {
    if (current.nodeType === Node.TEXT_NODE) {
      translateTextNode(current as Text, locale);
    } else if (current.nodeType === Node.ELEMENT_NODE) {
      const element = current as Element;
      if (!['SCRIPT', 'STYLE'].includes(element.tagName)) {
        translateAttributes(element, locale);
      }
    }
    current = walker.nextNode();
  }
}

export function localizeAppDom(root: HTMLElement, locale: unknown) {
  const activeLocale = normalizeSupportedLocale(locale);
  document.documentElement.lang = activeLocale === 'en-US' ? 'en' : 'zh-CN';
  translateTree(root, activeLocale);

  const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      if (mutation.type === 'characterData' && mutation.target.nodeType === Node.TEXT_NODE) {
        translateTextNode(mutation.target as Text, activeLocale);
        return;
      }

      if (mutation.type === 'attributes' && mutation.target.nodeType === Node.ELEMENT_NODE) {
        translateAttributes(mutation.target as Element, activeLocale);
        return;
      }

      mutation.addedNodes.forEach((node) => {
        if (node.nodeType === Node.TEXT_NODE) {
          translateTextNode(node as Text, activeLocale);
        } else if (node.nodeType === Node.ELEMENT_NODE) {
          const element = node as Element;
          if (!element.closest(LOCALIZE_SKIP_SELECTOR)) {
            translateAttributes(element, activeLocale);
            translateTree(element, activeLocale);
          }
        }
      });
    });
  });

  observer.observe(root, {
    attributes: true,
    attributeFilter: ['aria-label', 'placeholder', 'title'],
    childList: true,
    characterData: true,
    subtree: true,
  });

  return () => observer.disconnect();
}
