import type { ChangeEvent, KeyboardEvent, MouseEvent, RefObject } from 'react';

import type { ComposerAttachment, DropdownId, PermissionOption, ReasoningOption } from '../types';
import { formatBytes } from '../utils';
import Dropdown from './Dropdown';

interface ComposerProps {
  attachments: ComposerAttachment[];
  composerValue: string;
  currentModel: string;
  currentPermission: PermissionOption;
  currentReasoningLabel: string;
  currentReasoningValue: string;
  disabled?: boolean;
  dropdownId: DropdownId;
  fileInputRef: RefObject<HTMLInputElement | null>;
  isSending?: boolean;
  onAttachmentInputChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onChange: (event: ChangeEvent<HTMLTextAreaElement>) => void;
  onComposerPlus: () => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onOpenModelPicker: () => void;
  onPermissionSelect: (option: PermissionOption) => void;
  onReasoningSelect: (option: ReasoningOption) => void;
  onRemoveAttachment: (attachmentId: string) => void;
  onSend: () => void;
  onStop: () => void;
  onToggleDropdown: (dropdownId: Exclude<DropdownId, null>, event: MouseEvent<HTMLButtonElement>) => void;
  permissionOptions: PermissionOption[];
  reasoningOptions: ReasoningOption[];
  textareaRef: RefObject<HTMLTextAreaElement | null>;
}

export default function Composer({
  attachments,
  composerValue,
  currentModel,
  currentPermission,
  currentReasoningLabel,
  currentReasoningValue,
  disabled = false,
  dropdownId,
  fileInputRef,
  isSending = false,
  onAttachmentInputChange,
  onChange,
  onComposerPlus,
  onKeyDown,
  onOpenModelPicker,
  onPermissionSelect,
  onReasoningSelect,
  onRemoveAttachment,
  onSend,
  onStop,
  onToggleDropdown,
  permissionOptions,
  reasoningOptions,
  textareaRef,
}: ComposerProps) {
  const composerLocked = disabled || isSending;
  const pickerControlsDisabled = disabled;
  const commitDropdownOption = (event: MouseEvent<HTMLDivElement>, commit: () => void) => {
    event.preventDefault();
    event.stopPropagation();
    commit();
  };

  return (
    <div className="input-wrapper">
      <input hidden disabled={composerLocked} multiple onChange={onAttachmentInputChange} ref={fileInputRef} type="file" />

      {attachments.length > 0 ? (
        <div className="composer-attachment-strip">
          {attachments.map((attachment) => (
            <div className="composer-attachment-chip" key={attachment.id}>
              <div className="composer-attachment-icon">
                <i className={`ph-light ${attachment.kind === 'image' ? 'ph-image' : 'ph-file-text'}`} />
              </div>
              <div className="composer-attachment-meta">
                <div className="composer-attachment-name">{attachment.name}</div>
                <div className="composer-attachment-size">{formatBytes(attachment.size_bytes)}</div>
              </div>
              <button
                className="composer-attachment-remove"
                disabled={composerLocked}
                onClick={() => attachment.id && onRemoveAttachment(attachment.id)}
                type="button"
              >
                <i className="ph-light ph-x" />
              </button>
            </div>
          ))}
        </div>
      ) : null}

      <textarea
        className="chat-input"
        disabled={composerLocked}
        id="chat-input"
        onChange={onChange}
        onKeyDown={onKeyDown}
        placeholder="直接描述你要处理的事，Enter 发送，Shift + Enter 换行。"
        ref={textareaRef}
        rows={1}
        value={composerValue}
      />

      <div className="input-toolbar">
        <div className="toolbar-left">
          <button className="tool-btn-circle" disabled={composerLocked} type="button" onClick={onComposerPlus}>
            <i className="ph-light ph-plus" />
          </button>

          <Dropdown
            buttonChildren={
              <>
                <i className={`ph-light ${currentPermission.icon}`} /> <span>{currentPermission.label}</span>{' '}
                <i className="ph-light ph-caret-down" />
              </>
            }
            disabled={pickerControlsDisabled}
            isOpen={dropdownId === 'permission'}
            onToggle={(event) => onToggleDropdown('permission', event)}
          >
            {permissionOptions.map((option) => (
              <div
                className={`dropdown-item ${option.value === currentPermission.value ? 'selected' : ''}`}
                key={option.value}
                onMouseDown={(event) => commitDropdownOption(event, () => onPermissionSelect(option))}
              >
                <div className="dropdown-item-left">
                  <i className={`ph-light ${option.icon} menu-icon`} />
                  {option.label}
                </div>
                <i className="ph-light ph-check check-icon" />
              </div>
            ))}
          </Dropdown>
        </div>

        <div className="toolbar-right">
          <button
            className="tool-btn-capsule chat-model-picker-trigger"
            disabled={pickerControlsDisabled}
            type="button"
            onClick={(event) => event.stopPropagation()}
            onMouseDown={(event) => {
              if (event.button !== 0) {
                return;
              }
              event.preventDefault();
              event.stopPropagation();
              onOpenModelPicker();
            }}
          >
            <span>{currentModel}</span> <i className="ph-light ph-caret-down" />
          </button>

          <Dropdown
            align="right"
            buttonChildren={
              <>
                <span>{currentReasoningLabel}</span> <i className="ph-light ph-caret-down" />
              </>
            }
            disabled={pickerControlsDisabled}
            isOpen={dropdownId === 'intensity'}
            onToggle={(event) => onToggleDropdown('intensity', event)}
          >
            {reasoningOptions.map((option) => (
              <div
                className={`dropdown-item ${option.value === currentReasoningValue ? 'selected' : ''}`}
                key={option.value}
                onMouseDown={(event) => commitDropdownOption(event, () => onReasoningSelect(option))}
              >
                <div className="dropdown-item-left">{option.label}</div>
                <i className="ph-light ph-check check-icon" />
              </div>
            ))}
          </Dropdown>

          <button
            className={`tool-btn-circle composer-send-btn ${isSending ? 'is-stop-action' : 'is-send-action'}`}
            disabled={!isSending && disabled}
            type="button"
            onClick={isSending ? onStop : onSend}
            title={isSending ? '停止回复' : '发送消息'}
          >
            <i className={`ph-light ${isSending ? 'ph-stop' : 'ph-arrow-up'}`} />
          </button>
        </div>
      </div>
    </div>
  );
}
