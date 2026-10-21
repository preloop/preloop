import type { FleetMapNode } from '../fleet-map-api';
import { getAgentKindPresentation } from './agent-kinds';

/**
 * The mark a Fleet Map node shows: an agent's product logo from the
 * agent-kind registry the console already uses, a model's family mark (the
 * family can differ from the provider serving it: a DeepSeek model through
 * OpenRouter shows DeepSeek), a provider's own mark, and a generic icon for
 * anything unknown. Vendor marks live in /images/logos/vendors (MIT, from
 * lobe-icons); single-colour marks are drawn as a mask in the text colour so
 * they read in both themes.
 */
export interface NodeMark {
  /**
   * `img` draws a full-colour logo, `mask` a single-colour mark, `icon` a
   * Shoelace icon: by `icon` name, or from `src` when both are set it is `src`.
   */
  kind: 'img' | 'mask' | 'icon';
  src?: string;
  icon?: string;
  /** Who the mark stands for, for tests and tooltips. */
  name: string;
}

const VENDOR = '/images/logos/vendors/';

/** The Flows entry's icon in the console navbar (console-shell.ts). */
export const FLOW_ICON_SRC = '/images/flow.svg';

type Mark = [file: string, mono: boolean, name: string];

/** Model families, matched on the model identifier. Order matters. */
const FAMILIES: [RegExp, Mark][] = [
  [/deepseek/, ['deepseek-color.svg', false, 'DeepSeek']],
  [
    /claude|anthropic|opus|sonnet|haiku|fable/,
    ['claude-color.svg', false, 'Claude'],
  ],
  [/gemini|gemma/, ['gemini-color.svg', false, 'Gemini']],
  [/qwen|qwq/, ['qwen-color.svg', false, 'Qwen']],
  [/kimi|moonshot/, ['kimi-color.svg', false, 'Kimi']],
  [/glm|zhipu/, ['zhipu-color.svg', false, 'GLM']],
  [
    /mistral|mixtral|codestral|devstral|magistral/,
    ['mistral-color.svg', false, 'Mistral'],
  ],
  [/llama|meta-/, ['meta-color.svg', false, 'Meta']],
  [/grok/, ['grok.svg', true, 'Grok']],
  [/(^|[/-])(gpt|o\d|chatgpt|codex)|openai/, ['openai.svg', true, 'OpenAI']],
];

/** Providers, matched on the provider's name. */
const PROVIDERS: [RegExp, Mark][] = [
  [/openrouter/, ['openrouter.svg', true, 'OpenRouter']],
  [/anthropic/, ['anthropic.svg', true, 'Anthropic']],
  [/openai|azure/, ['openai.svg', true, 'OpenAI']],
  [/deepseek/, ['deepseek-color.svg', false, 'DeepSeek']],
  [/google|gemini|vertex/, ['gemini-color.svg', false, 'Google']],
  [/qwen|alibaba|dashscope/, ['qwen-color.svg', false, 'Qwen']],
  [/moonshot|kimi/, ['moonshot.svg', true, 'Moonshot']],
  [/zhipu|glm|z\.ai/, ['zhipu-color.svg', false, 'Zhipu']],
  [/mistral/, ['mistral-color.svg', false, 'Mistral']],
  [/meta|llama/, ['meta-color.svg', false, 'Meta']],
  [/xai|x\.ai|grok/, ['xai.svg', true, 'xAI']],
];

const vendor = ([file, mono, name]: Mark): NodeMark => ({
  kind: mono ? 'mask' : 'img',
  src: `${VENDOR}${file}`,
  name,
});

const find = (table: [RegExp, Mark][], text: string): Mark | undefined =>
  table.find(([pattern]) => pattern.test(text))?.[1];

/** A provider's mark from its name, or undefined when unknown. */
export function providerMark(name: string): NodeMark | undefined {
  const mark = find(PROVIDERS, name.toLowerCase());
  return mark ? vendor(mark) : undefined;
}

/**
 * The mark for one node. `providerName` gives a model without a known family
 * its provider's mark.
 */
export function nodeMark(
  node: Pick<FleetMapNode, 'type' | 'label' | 'detail'>,
  fallbackIcon: string,
  providerName = ''
): NodeMark {
  if (node.type === 'agent') {
    const kind = getAgentKindPresentation(node.detail || '');
    if (kind?.logo) return { kind: 'img', src: kind.logo, name: kind.label };
    if (kind?.icon) return { kind: 'icon', icon: kind.icon, name: kind.label };
    return { kind: 'icon', icon: fallbackIcon, name: 'Agent' };
  }
  if (node.type === 'model') {
    const id = `${node.detail || ''} ${node.label}`.toLowerCase();
    const family = find(FAMILIES, id);
    if (family) return vendor(family);
    return (
      providerMark(providerName) ?? {
        kind: 'icon',
        icon: fallbackIcon,
        name: 'Model',
      }
    );
  }
  if (node.type === 'provider')
    return (
      providerMark(node.label) ?? {
        kind: 'icon',
        icon: fallbackIcon,
        name: 'Provider',
      }
    );
  if (node.type === 'flow')
    return { kind: 'icon', src: FLOW_ICON_SRC, name: 'Flow' };
  return { kind: 'icon', icon: fallbackIcon, name: node.type };
}
