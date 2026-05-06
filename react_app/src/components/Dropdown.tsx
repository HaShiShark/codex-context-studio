import type { MouseEvent, ReactNode } from 'react';

interface DropdownProps {
  align?: 'left' | 'right';
  buttonClassName?: string;
  buttonChildren: ReactNode;
  children: ReactNode;
  disabled?: boolean;
  isOpen: boolean;
  onToggle: (event: MouseEvent<HTMLButtonElement>) => void;
}

export default function Dropdown({
  align = 'left',
  buttonClassName = 'tool-btn-capsule',
  buttonChildren,
  children,
  disabled = false,
  isOpen,
  onToggle,
}: DropdownProps) {
  function handleMouseDown(event: MouseEvent<HTMLButtonElement>) {
    if (event.button !== 0) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    onToggle(event);
  }

  function handleClick(event: MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    if (event.detail === 0) {
      onToggle(event);
    }
  }

  return (
    <div className="dropdown-container">
      <button
        className={buttonClassName}
        disabled={disabled}
        type="button"
        onClick={handleClick}
        onMouseDown={handleMouseDown}
      >
        {buttonChildren}
      </button>
      <div
        className={`dropdown-menu ${align === 'right' ? 'right-align' : ''} ${isOpen ? 'show' : ''}`.trim()}
        onMouseDown={(event) => event.stopPropagation()}
        onClick={(event) => event.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}
