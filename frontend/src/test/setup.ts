import '@testing-library/jest-dom/vitest';

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

Object.defineProperty(window, 'ResizeObserver', { value: ResizeObserverMock });
Object.defineProperty(window, 'matchMedia', {
  value: (query: string) => ({ matches: false, media: query, onchange: null, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent: () => false }),
});
Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
  value: () => ({ fillStyle: '', fillRect() {}, getImageData: () => ({ data: new Uint8ClampedArray([0, 0, 0, 255]) }) }),
});
