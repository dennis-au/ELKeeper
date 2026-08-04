export async function copyText(value: string, container: HTMLElement = document.body, source?: HTMLInputElement | HTMLTextAreaElement | null) {
  const clipboard = navigator.clipboard;
  if (window.isSecureContext && clipboard?.writeText) {
    try {
      await clipboard.writeText(value);
      return;
    } catch {
      // HTTP deployments and restrictive browser policies can reject Clipboard API access.
    }
  }

  const temporary = !source;
  const input = source || document.createElement('textarea');
  input.value = value;
  if (temporary) {
    input.setAttribute('readonly', '');
    input.setAttribute('aria-hidden', 'true');
    input.style.position = 'absolute';
    input.style.left = '-9999px';
    input.style.top = '0';
    input.style.opacity = '0';
    input.style.pointerEvents = 'none';
    container.appendChild(input);
  }
  input.focus({ preventScroll: true });
  input.select();
  input.setSelectionRange(0, input.value.length);
  const copied = typeof document.execCommand === 'function' && document.execCommand('copy');
  if (temporary) input.remove();
  if (!copied) throw new Error('Clipboard access was unavailable.');
}
