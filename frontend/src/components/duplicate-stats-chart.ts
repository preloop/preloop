import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { Chart, registerables } from 'chart.js';
import { getProjectDuplicateStats, ProjectStats } from '../api';

Chart.register(...registerables);

interface ProjectStats {
  project_id: string;
  project_name: string;
  total: number;
  duplicates: number;
}

interface DuplicateStatsResponse {
  projects: { [key: string]: ProjectStats };
}

@customElement('duplicate-stats-chart')
export class DuplicateStatsChart extends LitElement {
  @property({ type: Array }) projectIds: string[] = [];

  @property({ type: String }) selectedStatus: 'opened' | 'closed' | 'all' =
    'opened';

  @property({ type: Number }) similarityThreshold = 0.8;

  @property({ type: Boolean }) interactive = false;

  @state()
  private _loading = false;

  @state()
  private _error: string | null = null;

  @state()
  private _statsData: { [key: string]: ProjectStats } | null = null;

  private charts: Chart[] = [];

  static styles = css`
    :host {
      display: block;
    }

    #chart-container {
      display: flex;
      justify-content: space-around;
      align-items: flex-start;
      height: 100%;
      width: 100%;
      gap: var(--sl-spacing-medium);
    }

    .chart-wrapper {
      display: flex;
      flex-direction: column;
      align-items: center;
      flex: 1;
      max-width: 120px; /* Limit width of each chart item */
    }

    canvas {
      width: 100% !important;
      height: auto !important;
      aspect-ratio: 1 / 1;
    }

    .project-label {
      margin-top: var(--sl-spacing-x-small);
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-400);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      width: 100%;
      text-align: center;
    }
  `;

  updated(changedProperties: Map<string, any>) {
    if (
      changedProperties.has('projectIds') ||
      changedProperties.has('selectedStatus') ||
      changedProperties.has('similarityThreshold')
    ) {
      this.fetchData();
    }

    // Re-render the charts if the stats data is updated
    if (this._statsData && changedProperties.has('_statsData')) {
      this.renderCharts(this._statsData);
    }
  }

  async fetchData() {
    this._loading = true;
    this._error = null;
    this._statsData = null; // Clear previous stats

    try {
      const data = await getProjectDuplicateStats({
        project_ids: this.projectIds,
        status: this.selectedStatus,
        similarity_threshold: this.similarityThreshold,
      });
      // Guard against a missing projects property to prevent crashes
      if (data && data.projects) {
        this._statsData = data.projects;
      } else {
        this._statsData = {}; // Set empty stats to trigger render
      }
    } catch (error) {
      this._error =
        error instanceof Error ? error.message : 'An unknown error occurred.';
      console.error('Failed to fetch duplicate stats:', error);
    } finally {
      this._loading = false;
    }
  }

  renderCharts(stats: { [key: string]: ProjectStats }) {
    this.charts.forEach((chart) => chart.destroy());
    this.charts = [];

    const container = this.shadowRoot?.querySelector('#chart-container');
    if (!container) return;
    container.innerHTML = ''; // Clear previous charts

    const sortedStats = Object.values(stats)
      .filter((stat) => stat.duplicates > 0)
      .sort((a, b) => b.duplicates - a.duplicates)
      .slice(0, 5);

    const alarmingColor = 'hsl(350, 70%, 60%)';
    const alarmingBgColor = 'hsl(350, 70%, 65%)';

    sortedStats.forEach((stat) => {
      const wrapper = document.createElement('div');
      wrapper.className = 'chart-wrapper';

      const canvas = document.createElement('canvas');
      const label = document.createElement('div');
      label.className = 'project-label';
      label.textContent = stat.project_name;
      label.title = stat.project_name;

      wrapper.appendChild(canvas);
      wrapper.appendChild(label);
      container.appendChild(wrapper);

      const ctx = canvas.getContext('2d');
      if (!ctx) return;

      const duplicatePattern = this._createDiagonalPattern(
        ctx,
        alarmingColor,
        alarmingBgColor
      );

      const chart = new Chart(ctx, {
        type: 'pie',
        data: {
          labels: ['Similar', 'Unique'],
          datasets: [
            {
              data: [stat.duplicates, stat.total - stat.duplicates],
              backgroundColor: [duplicatePattern, 'hsl(210, 60%, 55%)'],
              hoverBackgroundColor: [
                'hsl(350, 70%, 50%)',
                'hsl(210, 60%, 65%)',
              ],
              borderWidth: 0,
              hoverOffset: 8,
            },
          ],
        },
        options: {
          onClick: () => {
            if (this.interactive) {
              this.dispatchEvent(
                new CustomEvent('project-selected', {
                  detail: { projectId: stat.project_id },
                  bubbles: true,
                  composed: true,
                })
              );
            }
          },
          onHover: (event, chartElement) => {
            const target = event.native?.target as HTMLCanvasElement;
            if (target) {
              target.style.cursor =
                this.interactive && chartElement[0] ? 'pointer' : 'default';
            }
          },
          responsive: true,
          maintainAspectRatio: true,
          plugins: {
            legend: {
              display: false,
            },
            tooltip: {
              backgroundColor: 'var(--sl-color-neutral-1000)',
              displayColors: false,
              callbacks: {
                title: () => stat.project_name,
                label: (context) => {
                  const label = context.label || '';
                  const value = context.parsed || 0;
                  return `${label}: ${value}`;
                },
              },
            },
          },
        },
      });
      this.charts.push(chart);
    });
  }

  private _createDiagonalPattern(
    chartCtx: CanvasRenderingContext2D,
    color = 'black',
    backgroundColor = 'transparent'
  ) {
    const patternCanvas = document.createElement('canvas');
    const patternCtx = patternCanvas.getContext('2d');
    if (!patternCtx) return color;

    const size = 10;
    patternCanvas.width = size;
    patternCanvas.height = size;

    patternCtx.fillStyle = backgroundColor;
    patternCtx.fillRect(0, 0, size, size);

    patternCtx.strokeStyle = color;
    patternCtx.lineWidth = 3;

    patternCtx.beginPath();
    patternCtx.moveTo(-2, size / 2 - 2);
    patternCtx.lineTo(size / 2 + 2, size + 2);
    patternCtx.stroke();

    patternCtx.beginPath();
    patternCtx.moveTo(size / 2 - 2, 0 - 2);
    patternCtx.lineTo(size + 2, size / 2 + 2);
    patternCtx.stroke();

    return chartCtx.createPattern(patternCanvas, 'repeat') || color;
  }

  render() {
    return html`
      <div id="chart-container">
        ${
          this._loading
            ? html`<div class="spinner-container">
                <sl-spinner></sl-spinner>
              </div>`
            : this._error
              ? html`<div>Error: ${this._error}</div>`
              : ''
        }
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'duplicate-stats-chart': DuplicateStatsChart;
  }
}
