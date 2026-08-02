import { afterEach, describe, expect, it, vi } from 'vitest';
import { copyText } from './clipboard';

const secureContext = Object.getOwnPropertyDescriptor(window, 'isSecureContext');
const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
const execCommand = Object.getOwnPropertyDescriptor(document, 'execCommand');

function restore(target: object, key: string, descriptor?: PropertyDescriptor) {
  if (descriptor) Object.defineProperty(target, key, descriptor);
  else Reflect.deleteProperty(target, key);
}

describe('copyText', () => {
  afterEach(() => {
    restore(window, 'isSecureContext', secureContext);
    restore(navigator, 'clipboard', clipboard);
    restore(document, 'execCommand', execCommand);
    document.querySelectorAll('textarea').forEach((element) => element.remove());
  });

  it('uses the asynchronous Clipboard API in a secure context', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });

    await copyText('public value');

    expect(writeText).toHaveBeenCalledWith('public value');
  });

  it('uses a temporary selected element when Clipboard API access is unavailable', async () => {
    const command = vi.fn(() => true);
    const container = document.createElement('div');
    document.body.appendChild(container);
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false });
    Object.defineProperty(document, 'execCommand', { configurable: true, value: command });

    await copyText('secret value', container);

    expect(command).toHaveBeenCalledWith('copy');
    expect(container.querySelector('textarea')).toBeNull();
    container.remove();
  });
});
