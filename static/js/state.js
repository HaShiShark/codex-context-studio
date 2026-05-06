// --- 核心状态 ---

let sidebarMode = 'projects';
let toastTimeout;

const appState = {
    sessions: [],
    conversations: {},
    projectName: '',
    currentSessionId: '',
    currentModel: 'gpt-5.4',
    currentReasoning: 'medium',
    models: [],
    reasoningOptions: [],
    currentWorkspaceLabel: '',
    workspaceExpanded: true,
    isSending: false,
};

function getConversation(sessionId = appState.currentSessionId) {
    if (!sessionId) {
        return [];
    }
    if (!appState.conversations[sessionId]) {
        appState.conversations[sessionId] = [];
    }
    return appState.conversations[sessionId];
}

function setConversation(sessionId, records) {
    appState.conversations[sessionId] = records;
}

function normalizeConversation(records = []) {
    let lastUserText = '';
    return records.map((record) => {
        const role = record.role === 'assistant' ? 'an' : 'user';
        const text = String(record.text || '');
        const toolEvents = Array.isArray(record.toolEvents) ? record.toolEvents : [];
        const normalized = {
            role,
            text,
            toolEvents,
            pending: false,
            sourceText: role === 'an' ? lastUserText : '',
        };
        if (role === 'user') {
            lastUserText = text;
        }
        return normalized;
    });
}

function ensureStructuredState() {
    if (!Array.isArray(appState.projects)) {
        appState.projects = [];
    }
    if (!Array.isArray(appState.chatSessions)) {
        appState.chatSessions = [];
    }
    if (!appState.projectExpansions || typeof appState.projectExpansions !== 'object') {
        appState.projectExpansions = {};
    }
    if (typeof appState.currentProjectId !== 'string') {
        appState.currentProjectId = '';
    }
    if (typeof appState.currentProjectSessionId !== 'string') {
        appState.currentProjectSessionId = '';
    }
    if (typeof appState.currentChatSessionId !== 'string') {
        appState.currentChatSessionId = '';
    }
}

function applySidebarPayload(data = {}) {
    ensureStructuredState();

    if (Array.isArray(data.projects)) {
        appState.projects = data.projects;
    }
    if (Array.isArray(data.chat_sessions)) {
        appState.chatSessions = data.chat_sessions;
    }

    const existingProjectIds = new Set(appState.projects.map((project) => project.id));
    Object.keys(appState.projectExpansions).forEach((projectId) => {
        if (!existingProjectIds.has(projectId)) {
            delete appState.projectExpansions[projectId];
        }
    });

    appState.projects.forEach((project, index) => {
        if (!(project.id in appState.projectExpansions)) {
            appState.projectExpansions[project.id] = index === 0;
        }
    });

    if (!appState.currentProjectId || !existingProjectIds.has(appState.currentProjectId)) {
        appState.currentProjectId = appState.projects[0] ? appState.projects[0].id : '';
    }
}

function getProjectById(projectId = appState.currentProjectId) {
    ensureStructuredState();
    return appState.projects.find((project) => project.id === projectId) || null;
}

function getCurrentProject() {
    return getProjectById(appState.currentProjectId);
}

function getSessionMeta(sessionId) {
    if (!sessionId) {
        return null;
    }

    ensureStructuredState();

    for (const project of appState.projects) {
        if (!Array.isArray(project.sessions)) {
            continue;
        }
        const session = project.sessions.find((item) => item.id === sessionId);
        if (session) {
            return {
                scope: 'project',
                project,
                projectId: project.id,
                session,
            };
        }
    }

    const chatSession = appState.chatSessions.find((item) => item.id === sessionId);
    if (chatSession) {
        return {
            scope: 'chat',
            project: null,
            projectId: null,
            session: chatSession,
        };
    }

    return null;
}
