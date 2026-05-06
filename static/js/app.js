// --- 初始化 & 启动 ---

async function initializeApp() {
    try {
        ensureStructuredState();

        const data = await apiFetch('/api/init');
        appState.projectName = data.project_name || 'hashcode';
        appState.models = data.models || [data.default_model || 'gpt-5.4'];
        appState.reasoningOptions = data.reasoning_options || [
            { value: 'none', label: '快速' },
            { value: 'low', label: '低' },
            { value: 'medium', label: '中' },
            { value: 'high', label: '高' },
        ];
        appState.currentModel = data.default_model || appState.models[0];
        appState.currentReasoning = appState.reasoningOptions.find((option) => option.value === 'medium')
            ? 'medium'
            : appState.reasoningOptions[0].value;
        appState.conversations = {};

        Object.entries(data.conversations || {}).forEach(([sessionId, records]) => {
            setConversation(sessionId, normalizeConversation(records));
        });

        applySidebarPayload(data);
        const project = getCurrentProject();
        appState.currentWorkspaceLabel = project ? project.title : appState.projectName;
        appState.currentSessionId = '';
        appState.currentProjectSessionId = '';
        appState.currentChatSessionId = '';

        renderModelMenu();
        renderReasoningMenu();
        renderSidebar();
        renderConversation();
        updateHeaderTitle();
    } catch (error) {
        showToast(error.message);
        renderSidebar();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const tx = document.getElementsByTagName("textarea");
    for (let i = 0; i < tx.length; i++) {
        tx[i].setAttribute("style", "height:" + (tx[i].scrollHeight) + "px;overflow-y:hidden;");
        tx[i].addEventListener("input", OnInput, false);
    }

    initializeApp();
});
