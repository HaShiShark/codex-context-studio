interface ToastProps {
  message: string;
  visible: boolean;
}

export default function Toast({ message, visible }: ToastProps) {
  return (
    <div id="an-toast" className={`toast-notification ${visible ? 'show' : ''}`}>
      <span id="toast-msg">{message}</span>
    </div>
  );
}
