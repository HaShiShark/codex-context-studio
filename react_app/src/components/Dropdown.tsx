import { useEffect, useRef } from 'react';
import type { MouseEvent, ReactNode } from 'react';

interface DropdownProps {
  align?: 'left' | 'right';
  buttonClassName?: string;
  buttonChildren: ReactNode;
  children: ReactNode;
  disabled?: boolean;
  isOpen: boolean;
  onClose?: () => void;
  onToggle: (event: MouseEvent<HTMLButtonElement>) => void;
}

export default function Dropdown({
  align = 'left',
  buttonClassName = 'tool-btn-capsule',
  buttonChildren,
  children,
  disabled = false,
  isOpen,
  onClose,
  onToggle,
}: DropdownProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen || !onClose) {
      return;
    }

    const closeDropdown = onClose;

    function handleDocumentMouseDown(event: globalThis.MouseEvent) {
      const target = event.target;
      if (target instanceof Node && containerRef.current?.contains(target)) {
        return;
      }
      closeDropdown();
    }

    document.addEventListener('mousedown', handleDocumentMouseDown);
    return () => document.removeEventListener('mousedown', handleDocumentMouseDown);
  }, [isOpen, onClose]);

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
    <div className="dropdown-container" ref={containerRef}>
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
