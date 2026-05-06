import { useEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, KeyboardEvent, MouseEvent, RefObject } from 'react';

import type {
  ComposerAttachment,
  ContextMapState,
  DropdownId,
  MessageRecord,
  PermissionOption,
  ReasoningOption,
  ViewName,
} from '../types';
import Composer from './Composer';
import MessageList from './MessageList';

interface ChatViewProps {
  attachments: ComposerAttachment[];
  composerValue: string;
  currentModel: string;
  currentPermission: PermissionOption;
  currentReasoningLabel: string;
  currentReasoningValue: string;
  disabled?: boolean;
  dropdownId: DropdownId;
  fileInputRef: RefObject<HTMLInputElement | null>;
  hasMessages: boolean;
  headerTitle: string;
  isSending?: boolean;
  messageListRef: RefObject<HTMLDivElement | null>;
  messages: MessageRecord[];
  contextMap: ContextMapState;
  onAttachmentInputChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onToggleContextMap: () => void;
  onComposerChange: (event: ChangeEvent<HTMLTextAreaElement>) => void;
  onComposerKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onComposerPlus: () => void;
  onCopyMessage: (text: string) => void;
  onDeleteMessage: (messageIndex: number) => void;
  onEditMessage: (messageIndex: number) => void;
  onRegenerateMessage: (text: string) => void;
  onRemoveAttachment: (attachmentId: string) => void;
  onOpenModelPicker: () => void;
  onSelectPermission: (option: PermissionOption) => void;
  onSelectReasoning: (option: ReasoningOption) => void;
  onSend: () => void;
  onStop: () => void;
  onToggleDropdown: (dropdownId: Exclude<DropdownId, null>, event: MouseEvent<HTMLButtonElement>) => void;
  permissionOptions: PermissionOption[];
  reasoningOptions: ReasoningOption[];
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  view: ViewName;
  welcomeAnimationKey: string;
  welcomeText: string;
}

export default function ChatView({
  attachments,
  composerValue,
  currentModel,
  currentPermission,
  currentReasoningLabel,
  currentReasoningValue,
  disabled = false,
  dropdownId,
  fileInputRef,
  hasMessages,
  headerTitle,
  isSending = false,
  messageListRef,
  messages,
  contextMap,
  onAttachmentInputChange,
  onToggleContextMap,
  onComposerChange,
  onComposerKeyDown,
  onComposerPlus,
  onCopyMessage,
  onDeleteMessage,
  onEditMessage,
  onRegenerateMessage,
  onRemoveAttachment,
  onOpenModelPicker,
  onSelectPermission,
  onSelectReasoning,
  onSend,
  onStop,
  onToggleDropdown,
  permissionOptions,
  reasoningOptions,
  textareaRef,
  view,
  welcomeAnimationKey,
  welcomeText,
}: ChatViewProps) {
  const welcomeLetters = useMemo(() => Array.from(welcomeText), [welcomeText]);
  const [visibleLetterCount, setVisibleLetterCount] = useState(welcomeLetters.length);
  const previousAnimationKeyRef = useRef<string | null>(null);
  const visibleWelcomeLetters = welcomeLetters.slice(0, visibleLetterCount);

  useEffect(() => {
    if (!welcomeLetters.length) {
      setVisibleLetterCount(0);
      return;
    }

    if (hasMessages) {
      previousAnimationKeyRef.current = welcomeAnimationKey;
      setVisibleLetterCount(welcomeLetters.length);
      return;
    }

    if (previousAnimationKeyRef.current === welcomeAnimationKey) {
      return;
    }
    previousAnimationKeyRef.current = welcomeAnimationKey;

    const prefersReducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    if (prefersReducedMotion) {
      setVisibleLetterCount(welcomeLetters.length);
      return;
    }

    setVisibleLetterCount(0);

    let interval: number | undefined;
    const startTimer = window.setTimeout(() => {
      setVisibleLetterCount(1);

      interval = window.setInterval(() => {
        setVisibleLetterCount((count) => {
          if (count >= welcomeLetters.length) {
            window.clearInterval(interval);
            return count;
          }

          return count + 1;
        });
      }, 34);
    }, 120);

    return () => {
      window.clearTimeout(startTimer);
      if (interval) {
        window.clearInterval(interval);
      }
    };
  }, [hasMessages, welcomeAnimationKey, welcomeLetters]);

  return (
    <div className={`view-panel ${view === 'chat' ? 'active' : ''}`} id="view-chat">
      <div className="header-container">
        <header className="header">
          <div className="header-title" id="chat-header-title">
            {headerTitle}
          </div>
          <div className="header-actions" style={{ display: 'flex', alignItems: 'center' }}>
            {contextMap.stage === 0 && (
              <i
                className="ph-light ph-layout control-btn-main"
                onClick={onToggleContextMap}
                title="Toggle right sidebar"
              />
            )}
          </div>
        </header>

        <div className={`chat-container ${hasMessages ? 'has-messages' : ''}`} id="chat-container">
          <h1 className="welcome-text" id="welcome-text-h1" aria-label={welcomeText} key={welcomeText}>
            <span className="welcome-text-letters" aria-hidden="true">
              {visibleWelcomeLetters.map((letter, index) => (
                <span
                  className={letter === ' ' ? 'welcome-letter is-space' : 'welcome-letter'}
                  key={`${index}-${letter}`}
                >
                  {letter === ' ' ? '\u00A0' : letter}
                </span>
              ))}
            </span>
          </h1>

          <MessageList
            listRef={messageListRef}
            messages={messages}
            onCopy={onCopyMessage}
            onDeleteMessage={onDeleteMessage}
            onEdit={onEditMessage}
            onRegenerate={onRegenerateMessage}
          />

          <div className="composer-dock">
            <Composer
              attachments={attachments}
              composerValue={composerValue}
              currentModel={currentModel}
              currentPermission={currentPermission}
              currentReasoningLabel={currentReasoningLabel}
              currentReasoningValue={currentReasoningValue}
              disabled={disabled}
              dropdownId={dropdownId}
              fileInputRef={fileInputRef}
              isSending={isSending}
              onAttachmentInputChange={onAttachmentInputChange}
              onChange={onComposerChange}
              onComposerPlus={onComposerPlus}
              onKeyDown={onComposerKeyDown}
              onOpenModelPicker={onOpenModelPicker}
              onPermissionSelect={onSelectPermission}
              onReasoningSelect={onSelectReasoning}
              onRemoveAttachment={onRemoveAttachment}
              onSend={onSend}
              onStop={onStop}
              onToggleDropdown={onToggleDropdown}
              permissionOptions={permissionOptions}
              reasoningOptions={reasoningOptions}
              textareaRef={textareaRef}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
