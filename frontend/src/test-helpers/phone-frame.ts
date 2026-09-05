/**
 * Render a component at a phone viewport inside a same-origin frame.
 *
 * A media query answers to the viewport, not to a container, so a phone rule
 * (`@media (max-width: 640px)`) can only be exercised in a frame that is the
 * phone's width. The frame loads the component's own module, so the element
 * is defined in the frame's realm and its styles resolve there.
 */

export interface PhoneFrame<T extends HTMLElement = HTMLElement> {
  frame: HTMLIFrameElement;
  frameWindow: Window;
  frameDocument: Document;
  element: T;
  /** Let layout and one Lit update cycle settle inside the frame. */
  settle: () => Promise<void>;
  cleanup: () => void;
}

/** iPhone 12/13/14 logical width, the phone the UX review shoots at. */
export const PHONE_WIDTH = 390;
export const PHONE_HEIGHT = 844;

export async function renderInPhoneFrame<
  T extends HTMLElement = HTMLElement,
>(options: {
  /** Absolute URL of the module that defines the custom element. */
  moduleUrl: string;
  /** Markup placed in the frame body. */
  markup: string;
  /** Tag to wait for and return. */
  tagName: string;
  width?: number;
  height?: number;
}): Promise<PhoneFrame<T>> {
  const width = options.width ?? PHONE_WIDTH;
  const height = options.height ?? PHONE_HEIGHT;
  const frame = document.createElement('iframe');
  frame.style.cssText = `width:${width}px;height:${height}px;border:0;`;
  document.body.appendChild(frame);

  const frameDocument = frame.contentDocument as Document;
  frameDocument.open();
  frameDocument.write(
    `<!DOCTYPE html><html><head><style>html,body{margin:0;padding:0}</style></head>` +
      `<body>${options.markup}` +
      `<script type="module" src="${options.moduleUrl}"></script></body></html>`
  );
  frameDocument.close();

  const frameWindow = frame.contentWindow as Window;
  await frameWindow.customElements.whenDefined(options.tagName);
  const element = frameDocument.querySelector(options.tagName) as T;

  const settle = async () => {
    const updatable = element as unknown as { updateComplete?: Promise<void> };
    if (updatable.updateComplete) {
      await updatable.updateComplete;
    }
    await new Promise<void>((resolve) =>
      frameWindow.requestAnimationFrame(() => resolve())
    );
    if (updatable.updateComplete) {
      await updatable.updateComplete;
    }
  };

  await settle();

  return {
    frame,
    frameWindow,
    frameDocument,
    element,
    settle,
    cleanup: () => frame.remove(),
  };
}
