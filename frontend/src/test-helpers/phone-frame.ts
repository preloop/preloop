/**
 * Render a component at a phone viewport inside a same-origin frame.
 *
 * A media query answers to the viewport, not to a container, so a phone rule
 * (`@media (max-width: 640px)`) can only be exercised in a frame that is the
 * phone's width. The frame loads the component's own module, so the element
 * is defined in the frame's realm and its styles resolve there.
 */

/** iPhone 12/13/14 logical width, the phone the UX review shoots at. */
export const PHONE_WIDTH = 390;
export const PHONE_HEIGHT = 844;

export interface EmptyPhoneFrame {
  frame: HTMLIFrameElement;
  frameWindow: Window;
  frameDocument: Document;
  /** Let layout and one Lit update cycle settle inside the frame. */
  settle: (element?: HTMLElement) => Promise<void>;
  cleanup: () => void;
}

export interface PhoneFrame<
  T extends HTMLElement = HTMLElement,
> extends EmptyPhoneFrame {
  element: T;
}

export interface PhoneFrameOptions {
  /** Absolute URL of the module that defines the custom element. */
  moduleUrl: string;
  /** Tag to wait for before returning. */
  tagName: string;
  /** Markup placed in the frame body before the module loads. */
  markup?: string;
  width?: number;
  height?: number;
}

/**
 * A phone-width frame with the component's module loaded. Use this directly
 * when the element needs its environment stubbed (fetch, sockets) before it
 * connects, and mount it yourself afterwards.
 */
export async function createPhoneFrame(
  options: PhoneFrameOptions
): Promise<EmptyPhoneFrame> {
  const width = options.width ?? PHONE_WIDTH;
  const height = options.height ?? PHONE_HEIGHT;
  const frame = document.createElement('iframe');
  frame.style.cssText = `width:${width}px;height:${height}px;border:0;`;
  document.body.appendChild(frame);

  const frameDocument = frame.contentDocument as Document;
  frameDocument.open();
  frameDocument.write(
    `<!DOCTYPE html><html><head><style>html,body{margin:0;padding:0}</style></head>` +
      `<body>${options.markup ?? ''}` +
      `<script type="module" src="${options.moduleUrl}"></script></body></html>`
  );
  frameDocument.close();

  const frameWindow = frame.contentWindow as Window;
  await frameWindow.customElements.whenDefined(options.tagName);

  const settle = async (element?: HTMLElement) => {
    const updatable = element as unknown as
      { updateComplete?: Promise<void> } | undefined;
    if (updatable?.updateComplete) {
      await updatable.updateComplete;
    }
    await new Promise<void>((resolve) =>
      frameWindow.requestAnimationFrame(() => resolve())
    );
    if (updatable?.updateComplete) {
      await updatable.updateComplete;
    }
  };

  return {
    frame,
    frameWindow,
    frameDocument,
    settle,
    cleanup: () => frame.remove(),
  };
}

/** The shorthand: markup in, mounted element out. */
export async function renderInPhoneFrame<T extends HTMLElement = HTMLElement>(
  options: PhoneFrameOptions & { markup: string }
): Promise<PhoneFrame<T>> {
  const frame = await createPhoneFrame(options);
  const element = frame.frameDocument.querySelector(options.tagName) as T;
  const settle = (target?: HTMLElement) => frame.settle(target ?? element);
  await settle();
  return { ...frame, element, settle };
}
