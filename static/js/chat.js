// --- 聊天/消息渲染与发送 ---

const MESSAGE_LIST_BOTTOM_THRESHOLD_PX = 96;
let shouldStickMessageListToBottom = true;
let lastRenderedConversationSessionId = '';
let lastRenderedConversationLength = 0;
let messageListScrollListenerBound = false;

function isMessageListNearBottom(messageList) {
    return messageList.scrollHeight - messageList.scrollTop - messageList.clientHeight <= MESSAGE_LIST_BOTTOM_THRESHOLD_PX;
}

function ensureMessageListScrollTracking(messageList) {
    if (messageListScrollListenerBound) {
        return;
    }

    messageListScrollListenerBound = true;
    messageList.addEventListener('scroll', () => {
        shouldStickMessageListToBottom = isMessageListNearBottom(messageList);
    }, { passive: true });
}

function formatMessageText(text, toolEvents = []) {
    let html = escapeHtml(text).replace(/\n/g, '<br>');

    if (Array.isArray(toolEvents) && toolEvents.length) {
        const extra = toolEvents
            .map((event) => {
                const name = escapeHtml(event.name || 'tool');
                const preview = escapeHtml(event.output_preview || '').replace(/\n/g, '<br>');
                return `<br><span style="opacity: 0.75;">[${name}] ${preview}</span>`;
            })
            .join('');
        html += `<br><br><span style="opacity: 0.9;">工具调用摘要</span>${extra}`;
    }

    return html;
}

function buildMessageElement(record) {
    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${record.role}`;

    const bodyDiv = document.createElement('div');
    bodyDiv.className = 'message-body';

    const bubble = document.createElement('div');
    bubble.className = 'content';
    bubble.innerHTML = formatMessageText(record.text, record.toolEvents);

    const actionsDiv = document.createElement('div');
    actionsDiv.className = 'message-actions';

    const copyBtn = document.createElement('button');
    copyBtn.className = 'action-btn';
    copyBtn.innerHTML = '<i class="ph-light ph-copy"></i>';
    copyBtn.onclick = () => copyText(record.text);
    actionsDiv.appendChild(copyBtn);

    if (record.role === 'user') {
        const editBtn = document.createElement('button');
        editBtn.className = 'action-btn';
        editBtn.innerHTML = '<i class="ph-light ph-pencil-simple"></i>';
        editBtn.onclick = () => {
            const input = document.getElementById('chat-input');
            input.value = record.text;
            OnInput.call(input);
            input.focus();
            showToast('已放回输入框，可修改后重发。');
        };
        actionsDiv.appendChild(editBtn);
    } else {
        const regenBtn = document.createElement('button');
        regenBtn.className = 'action-btn';
        regenBtn.innerHTML = '<i class="ph-light ph-arrows-clockwise"></i>';
        regenBtn.onclick = () => {
            if (!record.sourceText) {
                showToast('当前这条消息没有可重发的原始输入。');
                return;
            }
            const input = document.getElementById('chat-input');
            input.value = record.sourceText;
            OnInput.call(input);
            input.focus();
            showToast('已放回输入框，直接发送就会重新生成。');
        };
        actionsDiv.appendChild(regenBtn);
    }

    const delBtn = document.createElement('button');
    delBtn.className = 'action-btn';
    delBtn.innerHTML = '<i class="ph-light ph-trash"></i>';
    delBtn.onclick = () => {
        showToast('当前版本还不支持从后端历史中删除消息。');
    };
    actionsDiv.appendChild(delBtn);

    bodyDiv.appendChild(bubble);
    bodyDiv.appendChild(actionsDiv);
    msgDiv.appendChild(bodyDiv);
    return msgDiv;
}

function renderConversation({ forceScroll = false } = {}) {
    const msgList = document.getElementById('message-list');
    const chatContainer = document.getElementById('chat-container');
    const conversation = getConversation();
    const sessionChanged = lastRenderedConversationSessionId !== appState.currentSessionId;
    const messageAdded = !sessionChanged && conversation.length > lastRenderedConversationLength;
    const shouldScrollToBottom = forceScroll || sessionChanged || messageAdded || shouldStickMessageListToBottom;

    ensureMessageListScrollTracking(msgList);
    lastRenderedConversationSessionId = appState.currentSessionId;
    lastRenderedConversationLength = conversation.length;

    msgList.innerHTML = '';

    if (!conversation.length) {
        chatContainer.classList.remove('has-messages');
        shouldStickMessageListToBottom = true;
        updateWelcomeText();
        updateHeaderTitle();
        return;
    }

    chatContainer.classList.add('has-messages');
    conversation.forEach((record) => {
        msgList.appendChild(buildMessageElement(record));
    });
    if (shouldScrollToBottom) {
        msgList.scrollTop = msgList.scrollHeight;
        shouldStickMessageListToBottom = true;
    }
    updateHeaderTitle();
}

function renderModelMenu() {
    const menu = document.getElementById('menu-model');
    menu.innerHTML = '';

    appState.models.forEach((model) => {
        const item = document.createElement('div');
        item.className = `dropdown-item ${model === appState.currentModel ? 'selected' : ''}`;
        item.innerHTML = `
            <div class="dropdown-item-left">${model}</div>
            <i class="ph-light ph-check check-icon"></i>
        `;
        item.addEventListener('click', (event) => selectOption('btn-model', 'menu-model', model, null, item, event, model));
        menu.appendChild(item);
    });

    setButtonLabel('btn-model', appState.currentModel);
}

function renderReasoningMenu() {
    const menu = document.getElementById('menu-intensity');
    menu.innerHTML = '';

    appState.reasoningOptions.forEach((option) => {
        const item = document.createElement('div');
        item.className = `dropdown-item ${option.value === appState.currentReasoning ? 'selected' : ''}`;
        item.innerHTML = `
            <div class="dropdown-item-left">${option.label}</div>
            <i class="ph-light ph-check check-icon"></i>
        `;
        item.addEventListener('click', (event) => selectOption('btn-intensity', 'menu-intensity', option.label, null, item, event, option.value));
        menu.appendChild(item);
    });

    setButtonLabel('btn-intensity', getReasoningLabel(appState.currentReasoning));
}

async function clearSession(sessionId = appState.currentSessionId) {
    if (!sessionId) {
        showToast('当前还没有可清空的对话。');
        return;
    }

    const data = await apiFetch('/api/reset', {
        method: 'POST',
        body: JSON.stringify({ session_id: sessionId }),
    });

    applySidebarPayload(data);
    setConversation(sessionId, []);

    if (appState.currentSessionId === sessionId) {
        renderConversation();
    } else {
        renderSidebar();
    }

    showToast('当前对话已清空。');
}

async function ensureSession() {
    if (appState.currentSessionId) {
        return;
    }

    if (sidebarMode === 'projects') {
        if (!getCurrentProject()) {
            await createProject(true);
        }
        await createSession(true, {
            scope: 'project',
            projectId: appState.currentProjectId,
        });
        return;
    }

    await createSession(true, { scope: 'chat' });
}

async function animateSend() {
    const input = document.getElementById('chat-input');
    const val = input.value.trim();

    if (!val) {
        showToast('先输入你要处理的内容。');
        return;
    }

    if (appState.isSending) {
        showToast('上一条还在处理中。');
        return;
    }

    try {
        appState.isSending = true;
        await ensureSession();

        const sessionId = appState.currentSessionId;
        const conversation = [...getConversation(sessionId)];
        conversation.push({ role: 'user', text: val, toolEvents: [], pending: false, sourceText: '' });
        conversation.push({ role: 'an', text: '正在思考...', toolEvents: [], pending: true, sourceText: val });
        setConversation(sessionId, conversation);

        input.value = '';
        input.style.height = 'auto';
        switchView('chat');
        renderConversation();

        const response = await apiFetch('/api/send-message', {
            method: 'POST',
            body: JSON.stringify({
                session_id: sessionId,
                message: val,
                model: appState.currentModel,
                reasoning_effort: appState.currentReasoning,
            }),
        });

        const updatedConversation = [...getConversation(sessionId)];
        updatedConversation[updatedConversation.length - 1] = {
            role: 'an',
            text: response.answer || '',
            toolEvents: response.tool_events || [],
            pending: false,
            sourceText: val,
        };
        setConversation(sessionId, updatedConversation);
        applySidebarPayload(response);

        const session = response.session || {};
        if (session.scope === 'project') {
            appState.currentProjectId = session.project_id || appState.currentProjectId;
            appState.currentProjectSessionId = session.id;
            appState.projectExpansions[appState.currentProjectId] = true;
            const project = getCurrentProject();
            appState.currentWorkspaceLabel = project ? project.title : (appState.projectName || '');
        } else {
            appState.currentChatSessionId = session.id;
            appState.currentWorkspaceLabel = '';
        }

        appState.currentSessionId = sessionId;
        renderSidebar();
        renderConversation();

        if (Array.isArray(response.tool_events) && response.tool_events.length) {
            showToast(`本轮调用了 ${response.tool_events.length} 个工具。`);
        }
    } catch (error) {
        const sessionId = appState.currentSessionId;
        const failedConversation = [...getConversation(sessionId)];
        if (failedConversation.length) {
            failedConversation[failedConversation.length - 1] = {
                role: 'an',
                text: error.message,
                toolEvents: [],
                pending: false,
                sourceText: val,
            };
            setConversation(sessionId, failedConversation);
        }
        renderConversation();
        showToast(error.message);
    } finally {
        appState.isSending = false;
    }
}
