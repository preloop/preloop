import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { when } from 'lit/directives/when.js';
import type { Organization, Project } from '../types';
import type { SlTree } from '@shoelace-style/shoelace';

import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/tree/tree.js';
import '@shoelace-style/shoelace/dist/components/tree-item/tree-item.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/radio-button/radio-button.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import { consoleDialogStyles } from '../styles/console-dialog';

@customElement('project-filter-modal')
export class ProjectFilterModal extends LitElement {
  static styles = [
    consoleDialogStyles,
    css`
      sl-tree {
        max-height: 300px;
        overflow-y: auto;
      }
      .filter-section {
        margin-bottom: var(--sl-spacing-medium);
      }
      .filter-label {
        font-weight: var(--sl-font-weight-semibold);
        margin-bottom: var(--sl-spacing-x-small);
        display: block;
      }
    `,
  ];

  @property({ type: Boolean }) isOpen = false;
  @property({ type: Array }) organizations: Organization[] = [];
  @property({ type: Array }) projects: Project[] = [];
  @property({ type: Array }) selectedProjectIds: string[] = [];
  @property({ type: Boolean }) showProjects = true;
  @property({ type: Boolean }) showResolution = true;
  @property({ type: String }) selectedStatus: 'opened' | 'closed' | 'all' =
    'opened';
  @property({ type: String }) selectedResolution:
    'resolved' | 'unresolved' | 'all' = 'all';

  @state() private draftSelectedProjectIds: string[] = [];
  @state() private draftSelectedStatus: 'opened' | 'closed' | 'all' = 'opened';
  @state() private draftSelectedResolution: 'resolved' | 'unresolved' | 'all' =
    'all';

  willUpdate(changedProperties: Map<string, any>) {
    // When the dialog is opened, reset the draft state from the properties
    if (changedProperties.has('isOpen') && this.isOpen) {
      this.draftSelectedProjectIds = [...this.selectedProjectIds];
      this.draftSelectedStatus = this.selectedStatus;
      this.draftSelectedResolution = this.selectedResolution;
    }
  }

  handleSelect(event: CustomEvent) {
    const selectedItems = event.detail.selection as HTMLElement[];
    const projectIds = selectedItems
      .map((item) => item.dataset.projectId)
      .filter((id) => id); // Filter out undefined from parent items

    this.draftSelectedProjectIds = [...new Set(projectIds)] as string[];
  }

  handleStatusChange(event: CustomEvent) {
    const radioGroup = event.target as any;
    this.draftSelectedStatus = radioGroup.value;
  }

  handleResolutionChange(event: CustomEvent) {
    const radioGroup = event.target as any;
    this.draftSelectedResolution = radioGroup.value;
  }

  handleApply() {
    this.dispatchEvent(
      new CustomEvent('on-apply', {
        bubbles: true,
        composed: true,
        detail: {
          projectIds: this.draftSelectedProjectIds,
          status: this.draftSelectedStatus,
          resolution: this.draftSelectedResolution,
        },
      })
    );
  }

  handleClose() {
    this.dispatchEvent(
      new CustomEvent('on-close', { bubbles: true, composed: true })
    );
  }

  render() {
    const projectsByOrg = this.projects.reduce((acc, p) => {
      // Assuming project has an organization_id. This is based on typical data structures.
      const orgId = p.organization_id;
      if (!acc.has(orgId)) {
        acc.set(orgId, []);
      }
      acc.get(orgId)!.push(p);
      return acc;
    }, new Map<number, Project[]>());

    return html`
      <sl-dialog
        label="Filters"
        .open=${this.isOpen}
        @sl-hide=${this.handleClose}
      >
        ${when(
          this.showProjects,
          () => html`
            <div
              class="filter-section"
              style="margin-top: var(--sl-spacing-medium);"
            >
              <label class="filter-label">Projects</label>
              <sl-tree
                selection="multiple"
                @sl-selection-change=${this.handleSelect}
              >
                ${this.organizations.map((org) => {
                  const projectsInOrg = projectsByOrg.get(org.id) || [];
                  const selectedProjectsInOrg = projectsInOrg.filter((p) =>
                    this.draftSelectedProjectIds.includes(p.id.toString())
                  );

                  const allSelected =
                    projectsInOrg.length > 0 &&
                    selectedProjectsInOrg.length === projectsInOrg.length;
                  const someSelected =
                    selectedProjectsInOrg.length > 0 &&
                    selectedProjectsInOrg.length < projectsInOrg.length;

                  return html`
                    <sl-tree-item
                      ?selected=${allSelected}
                      ?indeterminate=${someSelected}
                    >
                      ${org.name}
                      ${projectsInOrg.map(
                        (proj) =>
                          html`<sl-tree-item
                            data-project-id="${proj.id}"
                            ?selected=${this.draftSelectedProjectIds.includes(
                              proj.id.toString()
                            )}
                            >${proj.name}</sl-tree-item
                          >`
                      )}
                    </sl-tree-item>
                  `;
                })}
              </sl-tree>
            </div>
            <sl-divider></sl-divider>
          `
        )}

        <div class="filter-section">
          <label class="filter-label">Issue Status</label>
          <sl-radio-group
            value=${this.draftSelectedStatus}
            @sl-change=${this.handleStatusChange}
          >
            <sl-radio-button value="opened">Opened</sl-radio-button>
            <sl-radio-button value="closed">Closed</sl-radio-button>
            <sl-radio-button value="all">All</sl-radio-button>
          </sl-radio-group>
        </div>

        ${when(
          this.showResolution,
          () => html`
            <sl-divider></sl-divider>
            <div class="filter-section">
              <label class="filter-label">Resolution Status</label>
              <sl-radio-group
                value=${this.draftSelectedResolution}
                @sl-change=${this.handleResolutionChange}
              >
                <sl-radio-button value="all">All</sl-radio-button>
                <sl-radio-button value="resolved">Resolved</sl-radio-button>
                <sl-radio-button value="unresolved">Unresolved</sl-radio-button>
              </sl-radio-group>
            </div>
          `
        )}

        <sl-button slot="footer" @click=${this.handleClose}>Cancel</sl-button>
        <sl-button slot="footer" variant="primary" @click=${this.handleApply}
          >Apply</sl-button
        >
      </sl-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'project-filter-modal': ProjectFilterModal;
  }
}
