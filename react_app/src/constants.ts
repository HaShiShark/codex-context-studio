import type { PermissionOption, ThemeOption } from './types';

export const PAPER_INK_WHITE_THEME = 'paper-ink-white';

export const INITIAL_TOAST_MESSAGE = '本地工作台已经就绪。';

export const PERMISSION_OPTIONS: PermissionOption[] = [
  {
    value: 'default',
    label: '默认权限',
    icon: 'ph-hand-palm',
  },
  {
    value: 'full',
    label: '完全访问权限',
    icon: 'ph-shield-check',
  },
  {
    value: 'custom',
    label: '项目配置权限',
    icon: 'ph-gear',
    toastMessage: '已切换到项目配置权限。',
  },
];

export const THEME_OPTIONS: ThemeOption[] = [
  { value: PAPER_INK_WHITE_THEME, label: '纸墨白' },
  { value: '#cba6f7', label: '鸢尾紫' },
  { value: '#89b4fa', label: '冷静蓝' },
  { value: '#a6e3a1', label: '轻量绿' },
];

export const SEARCH_TOAST = '搜索入口还没接线，先直接在会话里描述需求会更快。';
export const PLUGIN_TOAST = '插件能力还在准备中，当前先走本地工具链。';
export const AUTOMATION_TOAST = '自动化入口后面再补，现在先把主链路跑通。';
export const HEADER_FOLDER_TOAST = '这个入口我先没动，当前还是直接在会话里描述需求。';
export const COMPOSER_PLUS_TOAST = '这里现在支持上传文件和图片了。';
export const NEW_PROJECT_TOAST = '已创建新项目。';
export const NEW_CHAT_TOAST = '已创建新对话。';
export const CLEAR_CHAT_TOAST = '当前对话已清空。';
export const DELETE_CHAT_TOAST = '对话已删除。';
export const COPY_TOAST = '已复制到剪贴板。';
export const EDIT_TOAST = '已经放回输入框，可以修改后重发。';
export const REGENERATE_TOAST = '已经放回输入框，直接发送就会重新生成。';
export const REGENERATE_MISSING_TOAST = '这条消息没有可重发的原始输入。';
export const DELETE_MESSAGE_TOAST = '当前版本还不支持从后端历史中删除单条消息。';
export const EMPTY_SEND_TOAST = '先输入内容，或者至少带一个附件。';
export const SENDING_TOAST = '上一条还在处理中。';
export const COLLAPSE_TOAST = '侧边栏已折叠。';
export const THEME_TOAST = '主题颜色已更新。';
export const SETTINGS_HINTS_ON_TOAST = '已开启本地服务提示。';
export const SETTINGS_HINTS_OFF_TOAST = '已关闭本地服务提示。';
export const SETTINGS_SAVED_TOAST = '设置已保存。';
export const SETTINGS_API_KEY_CLEARED_TOAST = 'OpenAI API Key 已清除。';
export const ATTACHMENT_READY_TOAST = '附件已加入输入区。';
export const ATTACHMENT_REMOVED_TOAST = '附件已移除。';
