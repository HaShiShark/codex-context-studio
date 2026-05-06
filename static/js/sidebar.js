// --- 侧边栏渲染 & 项目/会话管理 ---

function renderSidebar() {
    const listContainer = document.getElementById('sidebar-list');
    const titleText = document.getElementById('sidebar-title-text');
    if (!listContainer || !titleText) {
        return;
    }

    ensureStructuredState();
    listContainer.innerHTML = '';

    if (sidebarMode === 'projects') {
        titleText.innerHTML = '我的项目 <i class="ph-light ph-caret-down" style="font-size: 10px;"></i>';

        if (!appState.projects.length) {
            listContainer.innerHTML = `
                <div class="list-row">
                    <div class="row-content">
                        <i class="ph-light ph-folder"></i>
                        <span>还没有项目，点右侧加号先建一个。</span>
                    </div>
                </div>
            `;
            return;
        }

        appState.projects.forEach((project) => {
            const expanded = !!appState.projectExpansions[project.id];
            const projectRow = document.createElement('div');
            projectRow.className = `list-row ${appState.currentProjectId === project.id ? 'expanded' : ''}`;
            projectRow.innerHTML = `
                <div class="row-content">
                    <i class="ph-light ${expanded ? 'ph-folder-open' : 'ph-folder'}"></i>
                    <span>${escapeHtml(project.title)}</span>
                </div>
                <div class="row-actions">
                    <i class="ph-light ph-dots-three row-action-btn" title="项目说明"></i>
                    <i class="ph-light ph-pencil-simple row-action-btn" title="新建项目对话"></i>
                </div>
            `;

            const [infoBtn, newChatBtn] = projectRow.querySelectorAll('.row-action-btn');
            infoBtn.onclick = (event) => {
                event.stopPropagation();
                showToast(`当前项目：${project.title}`);
            };
            newChatBtn.onclick = async (event) => {
                event.stopPropagation();
                appState.currentProjectId = project.id;
                appState.projectExpansions[project.id] = true;
                await createSession(false, { scope: 'project', projectId: project.id });
            };
            projectRow.onclick = () => {
                appState.currentProjectId = project.id;
                appState.currentWorkspaceLabel = project.title;
                appState.projectExpansions[project.id] = !expanded;
                appState.currentSessionId = '';
                renderSidebar();
                renderConversation();
            };
            listContainer.appendChild(projectRow);

            if (!expanded) {
                return;
            }

            if (!Array.isArray(project.sessions) || !project.sessions.length) {
                const emptyRow = document.createElement('div');
                emptyRow.className = 'list-row indented';
                emptyRow.innerHTML = `
                    <div class="row-content">
                        <span>这个项目里还没有对话</span>
                    </div>
                `;
                listContainer.appendChild(emptyRow);
                return;
            }

            project.sessions.forEach((session) => {
                const chatRow = document.createElement('div');
                chatRow.className = `list-row indented ${session.id === appState.currentSessionId ? 'expanded' : ''}`;
                chatRow.innerHTML = `
                    <div class="row-content">
                        <span>${escapeHtml(session.title)}</span>
                    </div>
                    <div class="row-actions">
                        <i class="ph-light ph-trash row-action-btn" title="删除对话"></i>
                    </div>
                `;

                chatRow.querySelector('.row-action-btn').onclick = async (event) => {
                    event.stopPropagation();
                    await deleteSession(session.id);
                };
                chatRow.onclick = () => {
                    switchSession(session.id);
                };
                listContainer.appendChild(chatRow);
            });
        });
        return;
    }

    titleText.innerHTML = '我的对话 <i class="ph-light ph-caret-down" style="font-size: 10px;"></i>';

    if (!appState.chatSessions.length) {
        listContainer.innerHTML = `
            <div class="list-row">
                <div class="row-content">
                    <i class="ph-light ph-chat-teardrop-text"></i>
                    <span>这里还没有对话，点右侧加号新建。</span>
                </div>
            </div>
        `;
        return;
    }

    appState.chatSessions.forEach((session) => {
        const row = document.createElement('div');
        row.className = `list-row ${session.id === appState.currentSessionId ? 'expanded' : ''}`;
        row.innerHTML = `
            <div class="row-content">
                <i class="ph-light ph-chat-teardrop-text"></i>
                <span>${escapeHtml(session.title)}</span>
            </div>
            <div class="row-actions">
                <i class="ph-light ph-trash row-action-btn" title="删除对话"></i>
            </div>
        `;

        row.querySelector('.row-action-btn').onclick = async (event) => {
            event.stopPropagation();
            await deleteSession(session.id);
        };
        row.onclick = () => {
            switchSession(session.id);
        };
        listContainer.appendChild(row);
    });
}

function toggleSidebarMode() {
    sidebarMode = sidebarMode === 'projects' ? 'chats' : 'projects';
    appState.currentSessionId = '';
    if (sidebarMode === 'projects') {
        const project = getCurrentProject();
        appState.currentWorkspaceLabel = project ? project.title : (appState.projectName || '');
    } else {
        appState.currentWorkspaceLabel = '';
    }
    renderSidebar();
    renderConversation();
}

async function addSidebarItem() {
    if (sidebarMode === 'projects') {
        await createProject();
        return;
    }
    await createSession(false, { scope: 'chat' });
}

async function createProject(silent = false) {
    const data = await apiFetch('/api/projects', {
        method: 'POST',
        body: JSON.stringify({}),
    });

    applySidebarPayload(data);
    appState.currentProjectId = data.project.id;
    appState.currentWorkspaceLabel = data.project.title;
    appState.currentSessionId = '';
    appState.projectExpansions[data.project.id] = true;
    renderSidebar();
    renderConversation();
    focusComposer();

    if (!silent) {
        showToast('已创建新项目。');
    }
}

async function createSession(silent = false, options = {}) {
    ensureStructuredState();

    const scope = options.scope || (sidebarMode === 'projects' ? 'project' : 'chat');
    let projectId = options.projectId || null;

    if (scope === 'project' && !projectId) {
        if (!getCurrentProject()) {
            await createProject(true);
        }
        projectId = appState.currentProjectId || (getCurrentProject() ? getCurrentProject().id : null);
    }

    const data = await apiFetch('/api/sessions', {
        method: 'POST',
        body: JSON.stringify({
            scope,
            project_id: projectId,
        }),
    });

    applySidebarPayload(data);
    const session = data.session;
    setConversation(session.id, []);

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

    appState.currentSessionId = session.id;
    renderSidebar();
    renderConversation();
    focusComposer();

    if (!silent) {
        showToast('已创建新对话。');
    }
}

function switchSession(sessionId) {
    const meta = getSessionMeta(sessionId);
    if (!meta) {
        showToast('没找到这条对话。');
        return;
    }

    appState.currentSessionId = sessionId;
    if (meta.scope === 'project') {
        appState.currentProjectId = meta.projectId;
        appState.currentProjectSessionId = sessionId;
        appState.currentWorkspaceLabel = meta.project.title;
        appState.projectExpansions[meta.projectId] = true;
    } else {
        appState.currentChatSessionId = sessionId;
        appState.currentWorkspaceLabel = '';
    }

    switchView('chat');
    renderSidebar();
    renderConversation();
    focusComposer();
}

async function deleteSession(sessionId) {
    const meta = getSessionMeta(sessionId);
    if (!meta) {
        showToast('没找到这条对话。');
        return;
    }

    try {
        const data = await apiFetch('/api/delete-session', {
            method: 'POST',
            body: JSON.stringify({ session_id: sessionId }),
        });

        applySidebarPayload(data);
        delete appState.conversations[sessionId];

        if (meta.scope === 'project') {
            if (appState.currentProjectSessionId === sessionId) {
                appState.currentProjectSessionId = '';
            }
            if (appState.currentProjectId === meta.projectId) {
                const project = getCurrentProject();
                appState.currentWorkspaceLabel = project ? project.title : (appState.projectName || '');
            }
        } else if (appState.currentChatSessionId === sessionId) {
            appState.currentChatSessionId = '';
        }

        if (appState.currentSessionId === sessionId) {
            appState.currentSessionId = '';
        }

        renderSidebar();
        renderConversation();
        showToast('对话已删除。');
    } catch (error) {
        showToast(error.message);
    }
}
