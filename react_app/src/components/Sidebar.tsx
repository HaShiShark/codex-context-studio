import './Sidebar.polish.css';

import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';

import type { ProjectSummary, SessionSummary, SidebarMode, ViewName } from '../types';

interface MenuPosition {
  left: number;
  top: number;
}

interface SidebarProps {
  chatSessions: SessionSummary[];
  currentProjectId: string;
  currentSessionId: string;
  runningSessionIds: Record<string, boolean>;
  projectExpansions: Record<string, boolean>;
  projects: ProjectSummary[];
  sidebarMode: SidebarMode;
  isSidebarResizing: boolean;
  view: ViewName;
  onAddItem: () => void;
  onAutomation: () => void;
  onProjectArchive: (project: ProjectSummary) => void;
  onPlugins: () => void;
  onProjectDelete: (project: ProjectSummary) => void;
  onProjectOpenParent: (project: ProjectSummary) => void;
  onProjectPin: (project: ProjectSummary) => void;
  onProjectRename: (project: ProjectSummary) => void;
  onProjectSelect: (projectId: string) => void;
  onSearch: () => void;
  onSessionCreate: (projectId: string) => void;
  onSessionDelete: (sessionId: string) => void;
  onSessionSelect: (sessionId: string) => void;
  onSwitchView: (view: ViewName) => void;
  onToggleMode: () => void;
}

function ProjectPanel({
  currentProjectId,
  currentSessionId,
  isSidebarResizing,
  projectExpansions,
  projects,
  runningSessionIds,
  onProjectArchive,
  onProjectDelete,
  onProjectOpenParent,
  onProjectPin,
  onProjectRename,
  onProjectSelect,
  onSessionCreate,
  onSessionDelete,
  onSessionSelect,
}: Pick<
  SidebarProps,
  | 'currentProjectId'
  | 'currentSessionId'
  | 'isSidebarResizing'
  | 'projectExpansions'
  | 'projects'
  | 'runningSessionIds'
  | 'onProjectArchive'
  | 'onProjectDelete'
  | 'onProjectOpenParent'
  | 'onProjectPin'
  | 'onProjectRename'
  | 'onProjectSelect'
  | 'onSessionCreate'
  | 'onSessionDelete'
  | 'onSessionSelect'
>) {
  const [openProjectMenuId, setOpenProjectMenuId] = useState<string | null>(null);
  const [menuPosition, setMenuPosition] = useState<MenuPosition>({ left: 8, top: 8 });

  const closeProjectMenu = () => setOpenProjectMenuId(null);

  function getClampedMenuPosition(anchor: HTMLElement): MenuPosition {
    const edgePadding = 8;
    const menuWidth = 220;
    const menuHeight = 236;
    const gap = 8;
    const rect = anchor.getBoundingClientRect();
    const maxLeft = Math.max(edgePadding, window.innerWidth - menuWidth - edgePadding);
    const left = Math.min(Math.max(edgePadding, rect.right - menuWidth), maxLeft);
    const belowTop = rect.bottom + gap;
    const aboveTop = rect.top - menuHeight - gap;
    const rawTop = belowTop + menuHeight <= window.innerHeight - edgePadding ? belowTop : aboveTop;
    const maxTop = Math.max(edgePadding, window.innerHeight - menuHeight - edgePadding);
    const top = Math.min(Math.max(edgePadding, rawTop), maxTop);
    return { left, top };
  }

  useEffect(() => {
    if (!openProjectMenuId) {
      return;
    }

    document.addEventListener('click', closeProjectMenu);
    window.addEventListener('resize', closeProjectMenu);
    window.addEventListener('scroll', closeProjectMenu, true);
    return () => {
      document.removeEventListener('click', closeProjectMenu);
      window.removeEventListener('resize', closeProjectMenu);
      window.removeEventListener('scroll', closeProjectMenu, true);
    };
  }, [openProjectMenuId]);

  useEffect(() => {
    if (isSidebarResizing) {
      closeProjectMenu();
    }
  }, [isSidebarResizing]);

  useEffect(() => {
    if (openProjectMenuId && !projects.some((project) => project.id === openProjectMenuId)) {
      closeProjectMenu();
    }
  }, [openProjectMenuId, projects]);

  if (!projects.length) {
    return (
      <div className="list-row">
        <div className="row-content">
          <i className="ph-light ph-folder" />
          <span>还没有项目，点右侧加号先建一个。</span>
        </div>
      </div>
    );
  }

  return (
    <>
      {projects.map((project) => {
        const expanded = Boolean(projectExpansions[project.id]);
        const projectSessions = Array.isArray(project.sessions) ? project.sessions : [];

        return (
          <div
            className={`sidebar-project-block ${expanded ? 'is-expanded' : ''}`}
            key={project.id}
          >
            <div
              className={`list-row sidebar-project-row ${currentProjectId === project.id ? 'expanded' : ''}`}
              onClick={() => onProjectSelect(project.id)}
            >
              <div className="row-content">
                <span className="project-icon-stack" aria-hidden="true">
                  <i className="ph-light ph-folder project-icon project-icon-closed" />
                  <i className="ph-light ph-folder-open project-icon project-icon-open" />
                </span>
                <span>{project.title}</span>
              </div>

              <div className={`row-actions ${openProjectMenuId === project.id ? 'has-open-menu' : ''}`}>
                <span className="project-menu-anchor">
                  <i
                    className="ph-light ph-dots-three row-action-btn"
                    onClick={(event) => {
                      event.stopPropagation();
                      if (openProjectMenuId === project.id) {
                        closeProjectMenu();
                        return;
                      }
                      setMenuPosition(getClampedMenuPosition(event.currentTarget));
                      setOpenProjectMenuId(project.id);
                    }}
                    title="项目菜单"
                  />
                  {openProjectMenuId === project.id && createPortal(
                    <div
                      className="project-action-menu"
                      style={{ left: menuPosition.left, top: menuPosition.top }}
                      onClick={(event) => event.stopPropagation()}
                    >
                      <button type="button" onClick={() => { closeProjectMenu(); onProjectPin(project); }}>
                        <i className="ph-light ph-push-pin" />
                        <span>固定项目</span>
                      </button>
                      <button type="button" onClick={() => { closeProjectMenu(); onProjectOpenParent(project); }}>
                        <i className="ph-light ph-folder-open" />
                        <span>在资源管理器中打开</span>
                      </button>
                      <button type="button" onClick={() => { closeProjectMenu(); onProjectRename(project); }}>
                        <i className="ph-light ph-pencil-simple" />
                        <span>重命名项目</span>
                      </button>
                      <button type="button" onClick={() => { closeProjectMenu(); onProjectArchive(project); }}>
                        <i className="ph-light ph-archive-box" />
                        <span>归档对话</span>
                      </button>
                      <button
                        className="is-danger"
                        type="button"
                        onClick={() => {
                          closeProjectMenu();
                          onProjectDelete(project);
                        }}
                      >
                        <i className="ph-light ph-x" />
                        <span>移除</span>
                      </button>
                    </div>,
                    document.body,
                  )}
                </span>
                <i
                  className="ph-light ph-pencil-simple row-action-btn"
                  onClick={(event) => {
                    event.stopPropagation();
                    onSessionCreate(project.id);
                  }}
                  title="新建项目对话"
                />
              </div>
            </div>

            <div className={`project-session-shell ${expanded ? 'is-expanded' : ''}`}>
              <div className="project-session-shell-inner">
                {projectSessions.length ? (
                  projectSessions.map((session) => (
                    <div
                      className={`list-row indented session-row ${session.id === currentSessionId ? 'expanded' : ''} ${runningSessionIds[session.id] ? 'is-running' : ''}`}
                      key={session.id}
                      onClick={() => onSessionSelect(session.id)}
                    >
                      <div className="row-content">
                        <span>{session.title}</span>
                      </div>
                      <div className="row-actions">
                        {runningSessionIds[session.id] && (
                          <i className="ph-light ph-circle-notch session-running-indicator" title="正在回复" />
                        )}
                        <i
                          className="ph-light ph-trash row-action-btn"
                          onClick={(event) => {
                            event.stopPropagation();
                            onSessionDelete(session.id);
                          }}
                          title="删除对话"
                        />
                      </div>
                    </div>
                  ))
                ) : (
                  <div className="list-row indented sidebar-empty-row">
                    <div className="row-content">
                      <span>这个项目里还没有对话</span>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </>
  );
}

function ChatPanel({
  chatSessions,
  currentSessionId,
  runningSessionIds,
  onSessionDelete,
  onSessionSelect,
}: Pick<SidebarProps, 'chatSessions' | 'currentSessionId' | 'runningSessionIds' | 'onSessionDelete' | 'onSessionSelect'>) {
  if (!chatSessions.length) {
    return (
      <div className="list-row">
        <div className="row-content">
          <i className="ph-light ph-chat-teardrop-text" />
          <span>这里还没有对话，点右侧加号新建。</span>
        </div>
      </div>
    );
  }

  return (
    <>
      {chatSessions.map((session) => (
        <div
          className={`list-row ${session.id === currentSessionId ? 'expanded' : ''} ${runningSessionIds[session.id] ? 'is-running' : ''}`}
          key={session.id}
          onClick={() => onSessionSelect(session.id)}
        >
          <div className="row-content">
            <i className="ph-light ph-chat-teardrop-text" />
            <span>{session.title}</span>
          </div>
          <div className="row-actions">
            {runningSessionIds[session.id] && (
              <i className="ph-light ph-circle-notch session-running-indicator" title="正在回复" />
            )}
            <i
              className="ph-light ph-trash row-action-btn"
              onClick={(event) => {
                event.stopPropagation();
                onSessionDelete(session.id);
              }}
              title="删除对话"
            />
          </div>
        </div>
      ))}
    </>
  );
}

export default function Sidebar({
  chatSessions,
  currentProjectId,
  currentSessionId,
  isSidebarResizing,
  runningSessionIds,
  projectExpansions,
  projects,
  sidebarMode,
  view,
  onAddItem,
  onAutomation,
  onProjectArchive,
  onPlugins,
  onProjectDelete,
  onProjectOpenParent,
  onProjectPin,
  onProjectRename,
  onProjectSelect,
  onSearch,
  onSessionCreate,
  onSessionDelete,
  onSessionSelect,
  onSwitchView,
  onToggleMode,
}: SidebarProps) {
  const isProjectsMode = sidebarMode === 'projects';

  return (
    <aside className="sidebar sidebar-polish">
      <div className={`sidebar-item ${view === 'chat' ? 'active' : ''}`} id="nav-chat" onClick={() => onSwitchView('chat')}>
        <i className="ph-light ph-chat-teardrop-text main-icon" /> 会话
      </div>
      <div className="sidebar-item" onClick={onSearch}>
        <i className="ph-light ph-magnifying-glass main-icon" /> 搜索
      </div>
      <div className="sidebar-item" onClick={onPlugins}>
        <i className="ph-light ph-plug main-icon" /> 插件
      </div>
      <div className="sidebar-item" onClick={onAutomation}>
        <i className="ph-light ph-robot main-icon" /> 自动化
      </div>

      <div
        className={`section-header sidebar-mode-toggle ${isProjectsMode ? 'is-projects' : 'is-chats'}`}
        onClick={onToggleMode}
      >
        <div className="section-title-text sidebar-mode-label" id="sidebar-title-text">
          <span>{isProjectsMode ? '我的项目' : '我的对话'}</span>
        </div>
        <div
          className="section-add-btn"
          onClick={(event) => {
            event.stopPropagation();
            onAddItem();
          }}
        >
          <i className="ph-light ph-plus" style={{ fontSize: 14 }} />
        </div>
      </div>

      <div className="sidebar-dynamic-list" id="sidebar-list">
        <section className="sidebar-mode-panel" key={sidebarMode}>
          {isProjectsMode ? (
            <ProjectPanel
              currentProjectId={currentProjectId}
              currentSessionId={currentSessionId}
              isSidebarResizing={isSidebarResizing}
              projectExpansions={projectExpansions}
              projects={projects}
              runningSessionIds={runningSessionIds}
              onProjectArchive={onProjectArchive}
              onProjectDelete={onProjectDelete}
              onProjectOpenParent={onProjectOpenParent}
              onProjectPin={onProjectPin}
              onProjectRename={onProjectRename}
              onProjectSelect={onProjectSelect}
              onSessionCreate={onSessionCreate}
              onSessionDelete={onSessionDelete}
              onSessionSelect={onSessionSelect}
            />
          ) : (
            <ChatPanel
              chatSessions={chatSessions}
              currentSessionId={currentSessionId}
              runningSessionIds={runningSessionIds}
              onSessionDelete={onSessionDelete}
              onSessionSelect={onSessionSelect}
            />
          )}
        </section>
      </div>

      <div
        className={`sidebar-item ${view === 'settings' ? 'active' : ''}`}
        id="nav-settings"
        onClick={() => onSwitchView('settings')}
        style={{ marginTop: 10 }}
      >
        <i className="ph-light ph-gear main-icon" /> 设置
      </div>
    </aside>
  );
}
