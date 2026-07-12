import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { Deck } from '@deck.gl/core';
import { ScatterplotLayer } from '@deck.gl/layers';
import { getEmbeddingsForProjects } from '../api';
import { when } from 'lit/directives/when.js';

const TSNE_ITERATIONS = 500;

const COLOR_PALETTE: [number, number, number][] = [
  [255, 140, 0], // DarkOrange
  [0, 191, 255], // DeepSkyBlue
  [50, 205, 50], // LimeGreen
  [255, 69, 0], // OrangeRed
  [153, 50, 204], // DarkOrchid
  [255, 215, 0], // Gold
  [70, 130, 180], // SteelBlue
  [218, 112, 214], // Orchid
];

interface ScatterplotData {
  position: [number, number];
  issueKey: string;
  title: string;
  createdAt: string;
  projectId: string;
}

@customElement('embedding-viewer')
export class EmbeddingViewer extends LitElement {
  @property({ type: String, attribute: 'project-ids' }) projectIds: string = '';
  @state() private scatterplotData: ScatterplotData[] = [];
  @state() private isLoading = false;
  @state() private projectColorMap = new Map<
    string,
    [number, number, number]
  >();

  private deck: Deck | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private tsneWorker: Worker | null = null;

  updated(changedProperties: Map<string, any>) {
    if (changedProperties.has('projectIds')) {
      this.fetchAndVisualize();
    }
    if (changedProperties.has('scatterplotData')) {
      this.renderLayers();
    }
  }

  static styles = css`
    :host {
      display: block;
      position: relative;
      width: 100%;
      height: 160px;
      background-color: #2c3e50;
      border-radius: var(--sl-border-radius-medium);
    }
    .tooltip {
      position: absolute;
      background: white;
      padding: 8px;
      border: 1px solid #ccc;
      border-radius: 4px;
      font-size: 12px;
      pointer-events: none;
      z-index: 9999;
    }
    .loading-overlay {
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(44, 62, 80, 0.8);
      color: white;
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      gap: var(--sl-spacing-medium);
      z-index: 10000;
    }
  `;

  firstUpdated() {
    const canvas = this.shadowRoot?.querySelector(
      '#deck-canvas'
    ) as HTMLCanvasElement;
    this.deck = new Deck({
      canvas,
      initialViewState: {
        longitude: 0,
        latitude: 0,
        zoom: 1,
      },
      controller: true,
      onHover: this.handleHover.bind(this),
    });

    this.resizeObserver = new ResizeObserver(() => this.handleResize());
    this.resizeObserver.observe(this);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.resizeObserver?.disconnect();
    this.deck?.finalize();
    if (this.tsneWorker) {
      this.tsneWorker.terminate();
      this.tsneWorker = null;
    }
  }

  connectedCallback() {
    super.connectedCallback();
    // Initial fetch is handled by the 'updated' lifecycle method
    // when projectIds is first set.
  }

  handleResize() {
    if (this.deck) {
      this.deck.setProps({
        width: this.clientWidth,
        height: this.clientHeight,
      });
    }
  }

  private async fetchAndVisualize() {
    if (this.projectIds === undefined) {
      this.scatterplotData = [];
      return;
    }

    if (this.tsneWorker) {
      this.tsneWorker.terminate();
    }

    console.log('[EmbeddingViewer] Fetching with projectIds:', this.projectIds);
    const ids = this.projectIds
      .split(',')
      .map((id) => id.trim())
      .filter((id) => id);
    console.log('[EmbeddingViewer] Parsed project IDs to fetch:', ids);
    this.isLoading = true;

    try {
      const result = await getEmbeddingsForProjects(ids);
      console.log('[EmbeddingViewer] Received result from API:', result);
      const allEmbeddings = result?.data || [];
      const validData = allEmbeddings.filter(
        (item) =>
          item &&
          item.embedding &&
          Array.isArray(item.embedding) &&
          item.embedding.length > 0
      );

      if (validData.length === 0) {
        this.scatterplotData = [];
        this.isLoading = false;
        return;
      }

      // Generate color map for projects
      const uniqueProjectIds = [
        ...new Set(validData.map((item) => item.project_id)),
      ];
      const newProjectColorMap = new Map<string, [number, number, number]>();
      uniqueProjectIds.forEach((id, index) => {
        newProjectColorMap.set(id, COLOR_PALETTE[index % COLOR_PALETTE.length]);
      });
      this.projectColorMap = newProjectColorMap;

      if (validData.length === 1) {
        this.scatterplotData = [
          {
            position: [0, 0],
            issueKey: validData[0].issue_id,
            title: validData[0].issue_title,
            createdAt: validData[0].issue_created_at,
            projectId: validData[0].project_id,
          },
        ];
        console.log(
          '[EmbeddingViewer] Final scatterplot data:',
          this.scatterplotData
        );
        this.isLoading = false;
      } else {
        const vectors = validData.map((item) => item.embedding);

        const perplexity = Math.max(
          5,
          Math.min(30, Math.floor(validData.length / 3))
        );
        const options = {
          epsilon: 10,
          perplexity: perplexity,
          dim: 2,
        };

        this.tsneWorker = new Worker(
          new URL('../workers/tsne.worker.ts', import.meta.url),
          { type: 'module' }
        );

        this.tsneWorker.onmessage = (e) => {
          const output = e.data;
          this.scatterplotData = validData.map((item, i) => ({
            position: [output[i][0] * 5, output[i][1]],
            issueKey: item.issue_id,
            title: item.issue_title,
            createdAt: item.issue_created_at,
            projectId: item.project_id,
          }));
          console.log(
            '[EmbeddingViewer] Final scatterplot data:',
            this.scatterplotData
          );
          this.isLoading = false;
          if (this.tsneWorker) {
            this.tsneWorker.terminate();
            this.tsneWorker = null;
          }
        };

        this.tsneWorker.onerror = (e) => {
          console.error('Error in t-SNE worker:', e);
          this.scatterplotData = [];
          this.isLoading = false;
          if (this.tsneWorker) {
            this.tsneWorker.terminate();
            this.tsneWorker = null;
          }
        };

        this.tsneWorker.postMessage({
          vectors,
          options,
          iterations: TSNE_ITERATIONS,
        });
      }
    } catch (error) {
      console.error('Error fetching or visualizing embeddings:', error);
      this.scatterplotData = [];
      this.isLoading = false;
    }
  }

  private renderLayers() {
    if (!this.deck) return;

    const layer = new ScatterplotLayer({
      id: 'scatterplot-layer',
      data: this.scatterplotData,
      pickable: true,
      opacity: 0.8,
      stroked: true,
      filled: true,
      radiusScale: 6,
      radiusMinPixels: 3,
      radiusMaxPixels: 10,
      lineWidthMinPixels: 1,
      getPosition: (d: ScatterplotData) => d.position,
      getFillColor: (d: ScatterplotData) =>
        this.projectColorMap.get(d.projectId) || [128, 128, 128],
      getLineColor: [255, 255, 255], // White outline
    });

    this.deck.setProps({ layers: [layer] });
  }

  private handleHover({ object, x, y }: { object: any; x: number; y: number }) {
    const tooltip = this.shadowRoot?.querySelector('.tooltip') as HTMLElement;
    if (!tooltip) return;

    if (object) {
      tooltip.style.display = 'block';
      tooltip.style.left = `${x}px`;
      tooltip.style.top = `${y}px`;
      // Build with textContent rather than innerHTML: issueKey/title are
      // external issue-tracker fields and interpolating them into innerHTML is
      // a DOM XSS vector (e.g. a title of `<img src=x onerror=...>`).
      const key = document.createElement('strong');
      key.textContent = object.issueKey ?? '';
      const title = document.createTextNode(object.title ?? '');
      const created = document.createElement('small');
      created.textContent = `Created: ${new Date(
        object.createdAt
      ).toLocaleDateString()}`;
      tooltip.replaceChildren(
        key,
        document.createElement('br'),
        title,
        document.createElement('br'),
        created
      );
    } else {
      tooltip.style.display = 'none';
    }
  }

  render() {
    return html`
      <canvas id="deck-canvas" style="width: 100%; height: 100%;"></canvas>
      <div class="tooltip"></div>
      ${when(
        this.isLoading,
        () => html`
          <div class="loading-overlay">
            <sl-spinner></sl-spinner>
            <span>Loading embeddings...</span>
          </div>
        `
      )}
    `;
  }
}
