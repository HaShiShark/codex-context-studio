// --- UI 工具函数 ---

function showToast(message) {
    const toast = document.getElementById('an-toast');
    document.getElementById('toast-msg').textContent = message;
    toast.classList.add('show');
    clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => toast.classList.remove('show'), 3000);
}

function switchView(viewName) {
    document.querySelectorAll('.view-panel').forEach(panel => panel.classList.remove('active'));
    document.querySelectorAll('.sidebar-item').forEach(item => item.classList.remove('active'));
    if (viewName === 'chat') {
        document.getElementById('view-chat').classList.add('active');
        document.getElementById('nav-chat').classList.add('active');
    } else if (viewName === 'settings') {
        document.getElementById('view-settings').classList.add('active');
        document.getElementById('nav-settings').classList.add('active');
    }
}

function toggleSidebar() {
    document.body.classList.toggle('sidebar-collapsed');
    if (document.body.classList.contains('sidebar-collapsed')) {
        showToast('已折叠侧边栏。');
    }
}

function toggleDropdown(menuId, event) {
    event.stopPropagation();
    const targetMenu = document.getElementById(menuId);
    const isShowing = targetMenu.classList.contains('show');

    document.querySelectorAll('.dropdown-menu').forEach(menu => menu.classList.remove('show'));

    if (!isShowing) {
        targetMenu.classList.add('show');
    }
}

document.addEventListener('click', () => {
    document.querySelectorAll('.dropdown-menu').forEach(menu => menu.classList.remove('show'));
});

function selectOption(btnId, menuId, value, iconClass, itemElement, event, actualValue = value) {
    event.stopPropagation();

    if (btnId === 'btn-model') {
        appState.currentModel = actualValue;
        renderModelMenu();
        document.getElementById(menuId).classList.remove('show');
        showToast(`模型已切换到 ${value}。`);
        return;
    }

    if (btnId === 'btn-intensity') {
        appState.currentReasoning = actualValue;
        renderReasoningMenu();
        document.getElementById(menuId).classList.remove('show');
        showToast(`推理强度已切换到 ${value}。`);
        return;
    }

    setButtonLabel(btnId, value, iconClass);
    const menu = document.getElementById(menuId);
    menu.querySelectorAll('.dropdown-item').forEach(item => item.classList.remove('selected'));
    itemElement.classList.add('selected');
    menu.classList.remove('show');

    if (value.includes('config')) {
        showToast('已切换到项目配置权限。');
    } else {
        showToast(`已切换到 ${value}。`);
    }
}

function setButtonLabel(btnId, value, iconClass = null) {
    const btn = document.getElementById(btnId);
    if (!btn) {
        return;
    }

    if (iconClass) {
        btn.innerHTML = `<i class="ph-light ${iconClass}"></i> <span>${value}</span> <i class="ph-light ph-caret-down"></i>`;
    } else {
        btn.innerHTML = `<span>${value}</span> <i class="ph-light ph-caret-down"></i>`;
    }
}

function getReasoningLabel(value) {
    const match = appState.reasoningOptions.find((option) => option.value === value);
    return match ? match.label : value;
}

function changeTheme(colorCode) {
    document.documentElement.style.setProperty('--an-theme', colorCode);
    const match = /^#?([0-9a-f]{6})$/i.exec(colorCode.trim());
    if (match) {
        const hex = match[1];
        const rgb = [
            parseInt(hex.slice(0, 2), 16),
            parseInt(hex.slice(2, 4), 16),
            parseInt(hex.slice(4, 6), 16),
        ].join(', ');
        document.documentElement.style.setProperty('--an-theme-rgb', rgb);
    }
    showToast('主题颜色已更新。');
}

function handleEnter(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (document.getElementById('view-chat').classList.contains('active')) {
            animateSend();
        }
    }
}

function quickFill(text) {
    switchView('chat');
    const input = document.getElementById('chat-input');
    input.value = text;
    input.focus();
    input.style.height = 'auto';
    input.style.height = (input.scrollHeight) + "px";
}

function OnInput() {
    this.style.height = "auto";
    this.style.height = (this.scrollHeight) + "px";
}

function focusComposer() {
    const input = document.getElementById('chat-input');
    if (input) {
        input.focus();
    }
}

function resetToNewChat(workspaceName = '') {
    switchView('chat');
    const input = document.getElementById('chat-input');
    input.value = '';
    input.style.height = 'auto';

    if (workspaceName) {
        appState.currentWorkspaceLabel = workspaceName;
    }

    renderConversation();
    input.focus();
}

function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
            () => showToast('已复制到剪贴板。'),
            () => fallbackCopyText(text),
        );
        return;
    }
    fallbackCopyText(text);
}

function fallbackCopyText(text) {
    const textArea = document.createElement('textarea');
    textArea.value = text;
    document.body.appendChild(textArea);
    textArea.select();
    document.execCommand('copy');
    document.body.removeChild(textArea);
    showToast('已复制到剪贴板。');
}

function updateHeaderTitle() {
    const headerTitle = document.getElementById('chat-header-title');
    if (!headerTitle) {
        return;
    }

    const meta = getSessionMeta(appState.currentSessionId);
    if (meta && meta.session && meta.session.title) {
        headerTitle.textContent = `hashcode · ${meta.session.title}`;
        return;
    }

    if (sidebarMode === 'projects') {
        const project = getCurrentProject();
        headerTitle.textContent = `hashcode · ${project ? project.title : '我的项目'}`;
        return;
    }

    headerTitle.textContent = 'hashcode · 我的对话';
}

function updateWelcomeText() {
    const welcomeText = document.getElementById('welcome-text-h1');
    if (!welcomeText) {
        return;
    }

    if (sidebarMode === 'projects') {
        const projectName = appState.projectName || 'hashcode';
        welcomeText.textContent = `What Should We Work In ${projectName}?`;
        return;
    }

    welcomeText.textContent = 'Hello！HaShiShark';
}

function clearCurrentSessionFromSettings() {
    clearSession().catch((error) => {
        showToast(error.message);
    });
}
