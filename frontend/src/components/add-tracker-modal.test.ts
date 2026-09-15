import { html, fixture, expect, oneEvent, waitUntil } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './add-tracker-modal.ts';
import { AddTrackerModal } from './add-tracker-modal';
import * as api from '../api';

describe('AddTrackerModal', () => {
  let element: AddTrackerModal;
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').resolves(new Response(JSON.stringify([])));
  });

  afterEach(() => {
    sandbox.restore();
    localStorage.clear();
  });

  const setupStubs = (el: AddTrackerModal) => {
    const validateStub = sandbox.stub();
    const addStub = sandbox.stub();
    const updateStub = sandbox.stub();
    const listProjectsStub = sandbox.stub();

    el._api = {
      ...api,
      validateTrackerToken: validateStub,
      addTracker: addStub,
      updateTracker: updateStub,
      listProjectsForOrg: listProjectsStub,
    };

    return { validateStub, addStub, updateStub, listProjectsStub };
  };

  describe('Add Mode', () => {
    beforeEach(async () => {
      element = await fixture(html`<add-tracker-modal></add-tracker-modal>`);
      await element.updateComplete;
    });

    it('renders correctly in initial add state (step 1)', async () => {
      const dialog = element.shadowRoot?.querySelector('sl-dialog');
      expect(dialog).to.exist;
      expect(dialog?.label).to.equal('Add Tracker');

      const nameInput = element.shadowRoot?.querySelector<HTMLInputElement>(
        'sl-input[name="name"]'
      );
      expect(nameInput).to.exist;
      expect(nameInput?.value).to.be.empty;

      const typeSelect = element.shadowRoot?.querySelector<HTMLSelectElement>(
        'sl-select[name="type"]'
      );
      expect(typeSelect).to.exist;
      expect(typeSelect?.value).to.equal('github');

      const nextButton = element.shadowRoot?.querySelector(
        'sl-button[variant="primary"]'
      );
      expect(nextButton).to.exist;
      expect(nextButton?.textContent?.trim()).to.equal('Next');
    });

    describe('Edit Mode', () => {
      const mockTracker = {
        id: '123',
        name: 'Test Tracker',
        tracker_type: 'gitlab',
        url: 'https://gitlab.com',
        connection_details: { username: 'testuser' },
        scope_rules: [
          {
            rule_type: 'INCLUDE',
            scope_type: 'ORGANIZATION',
            identifier: 'org1',
          },
        ],
      };

      beforeEach(async () => {
        element = await fixture(
          html`<add-tracker-modal .tracker=${mockTracker}></add-tracker-modal>`
        );
        await element.updateComplete;
      });

      it('renders correctly in initial edit state (step 1)', async () => {
        const dialog = element.shadowRoot?.querySelector('sl-dialog');
        expect(dialog).to.exist;
        expect(dialog?.label).to.equal('Edit Tracker');

        const nameInput = element.shadowRoot?.querySelector<HTMLInputElement>(
          'sl-input[name="name"]'
        );
        expect(nameInput?.value).to.equal(mockTracker.name);

        const typeSelect = element.shadowRoot?.querySelector<HTMLSelectElement>(
          'sl-select[name="type"]'
        );
        expect(typeSelect?.value).to.equal(mockTracker.tracker_type);

        const tokenInput = element.shadowRoot?.querySelector<HTMLInputElement>(
          'sl-input[name="api_key"]'
        );
        expect(tokenInput?.value).to.equal('unchanged');

        const saveButton = element.shadowRoot?.querySelector(
          'sl-button[variant="primary"]'
        );
        expect(saveButton?.textContent?.trim()).to.equal('Next');
      });
    });

    it('updates state on form input', async () => {
      element = await fixture(html`<add-tracker-modal></add-tracker-modal>`);
      await element.updateComplete;

      const nameInput = element.shadowRoot?.querySelector<HTMLInputElement>(
        'sl-input[name="name"]'
      );
      nameInput!.value = 'New Tracker Name';
      nameInput?.dispatchEvent(new Event('sl-input'));
      await element.updateComplete;
      expect(nameInput?.value).to.equal('New Tracker Name');

      const typeSelect = element.shadowRoot?.querySelector<HTMLSelectElement>(
        'sl-select[name="type"]'
      );
      typeSelect!.value = 'jira';
      typeSelect?.dispatchEvent(new Event('sl-change'));
      await element.updateComplete;
      expect(typeSelect?.value).to.equal('jira');
    });

    describe('Step 1 -> Step 2 Navigation', () => {
      beforeEach(async () => {
        element = await fixture(html`<add-tracker-modal></add-tracker-modal>`);
        await element.updateComplete;
      });

      it('transitions to step 2 on successful validation', async () => {
        const { validateStub } = setupStubs(element);
        validateStub.resolves({
          success: true,
          orgs: [{ id: 'org1', name: 'Org One' }],
        });

        const nextButton = element.shadowRoot?.querySelector<HTMLElement>(
          'sl-button[variant="primary"]'
        );
        nextButton?.click();
        await element.updateComplete;
        await element.updateComplete;

        expect(validateStub).to.have.been.calledOnce;
        const dialog = element.shadowRoot?.querySelector('sl-dialog');
        expect(dialog?.querySelector('h2')?.textContent).to.equal(
          'Configure Project Scope'
        );
      });

      it('shows an error message on failed validation', async () => {
        const { validateStub } = setupStubs(element);
        validateStub.resolves({ success: false, message: 'Invalid token' });

        const nextButton = element.shadowRoot?.querySelector<HTMLElement>(
          'sl-button[variant="primary"]'
        );
        nextButton?.click();
        await element.updateComplete;
        await element.updateComplete;

        expect(validateStub).to.have.been.calledOnce;
        const errorMessage = element.shadowRoot?.querySelector('.error');
        expect(errorMessage).to.exist;
        expect(errorMessage?.textContent).to.equal('Invalid token');
        const dialog = element.shadowRoot?.querySelector('sl-dialog');
        expect(dialog?.querySelector('h2')).to.not.exist;
      });

      describe('Saving', () => {
        it('calls addTracker and dispatches tracker-added on save in add mode', async () => {
          element = await fixture(
            html`<add-tracker-modal></add-tracker-modal>`
          );
          const { validateStub, addStub } = setupStubs(element);
          validateStub.resolves({
            success: true,
            orgs: [{ id: 'org1', name: 'Org One' }],
          });
          addStub.resolves({ id: '456' });

          const nextButton = element.shadowRoot?.querySelector<HTMLElement>(
            'sl-button[variant="primary"]'
          );
          nextButton?.click();
          await element.updateComplete;
          await element.updateComplete; // Wait for re-render

          const addButton = element.shadowRoot?.querySelector<HTMLElement>(
            'sl-button[variant="primary"]'
          );
          const listener = oneEvent(element, 'tracker-added');
          addButton?.click();
          const { detail } = await listener;

          expect(addStub).to.have.been.calledOnce;
          expect(detail.tracker.id).to.equal('456');
        });

        it('calls updateTracker and dispatches tracker-updated on save in edit mode', async () => {
          const mockTracker = {
            id: '123',
            name: 'Test Tracker',
            tracker_type: 'github',
            url: 'https://api.github.com',
            scope_rules: [],
          };
          element = await fixture(
            html`<add-tracker-modal
              .tracker=${mockTracker}
            ></add-tracker-modal>`
          );
          const { validateStub, updateStub } = setupStubs(element);
          validateStub.resolves({
            success: true,
            orgs: [{ id: 'org1', name: 'Org One' }],
          });
          updateStub.resolves({ id: '123' });

          const nextButton = element.shadowRoot?.querySelector<HTMLElement>(
            'sl-button[variant="primary"]'
          );
          nextButton?.click();
          await element.updateComplete;
          await element.updateComplete; // Wait for re-render

          const saveButton = element.shadowRoot?.querySelector<HTMLElement>(
            'sl-button[variant="primary"]'
          );
          const listener = oneEvent(element, 'tracker-updated');
          saveButton?.click();
          const { detail } = await listener;

          expect(updateStub).to.have.been.calledOnce;
          expect(detail.tracker.id).to.equal('123');
        });
      });

      describe('GitHub App trackers', () => {
        const installations = [
          {
            id: '11111111-1111-4111-8111-111111111111',
            installation_id: 4242,
            target_type: 'Organization',
            target_id: 9001,
            target_login: 'example-org',
            permissions: {},
            repository_selection: 'all',
            is_suspended: false,
          },
          {
            id: '22222222-2222-4222-8222-222222222222',
            installation_id: 4343,
            target_type: 'User',
            target_id: 9002,
            target_login: 'jane-doe',
            permissions: {},
            repository_selection: 'selected',
            is_suspended: false,
          },
        ];

        const buildApi = () => {
          const stubs = {
            validateTrackerToken: sandbox.stub(),
            addTracker: sandbox.stub(),
            updateTracker: sandbox.stub(),
            listProjectsForOrg: sandbox.stub(),
            completeGitHubInstallation: sandbox.stub().resolves({}),
            getGitHubInstallations: sandbox.stub().resolves(installations),
            getTrackerAuthMethods: sandbox
              .stub()
              .resolves({ github_app_configured: true }),
            getGitHubAuthUrl: sandbox.stub(),
          };
          return { stubs, api: { ...api, ...stubs } };
        };

        it('only scopes a new tracker to the installation being bound', async () => {
          const { stubs, api: stubbedApi } = buildApi();
          stubs.addTracker.resolves({ id: '456' });

          element = await fixture(
            html`<add-tracker-modal
              ._api=${stubbedApi}
              githubInstallationId="4242"
              githubTargetLogin="example-org"
            ></add-tracker-modal>`
          );
          // firstUpdated auto-saves after the OAuth callback.
          await oneEvent(element, 'tracker-added');

          expect(stubs.completeGitHubInstallation).to.have.been.calledOnceWith({
            installation_id: '4242',
          });
          expect(stubs.addTracker).to.have.been.calledOnce;
          const payload = stubs.addTracker.firstCall.args[0];
          expect(payload.auth_type).to.equal('github_app');
          expect(payload.github_installation_id).to.equal('4242');
          expect(payload.scope_rules).to.deep.equal([
            {
              rule_type: 'INCLUDE',
              scope_type: 'ORGANIZATION',
              identifier: '9001',
            },
          ]);
        });

        it('fails clearly when the bound installation is not registered', async () => {
          const { stubs, api: stubbedApi } = buildApi();
          stubs.getGitHubInstallations.resolves([]);

          element = await fixture(
            html`<add-tracker-modal
              ._api=${stubbedApi}
              githubInstallationId="4242"
              githubTargetLogin="example-org"
            ></add-tracker-modal>`
          );
          await new Promise((resolve) => setTimeout(resolve, 300));
          await element.updateComplete;

          expect(stubs.addTracker).to.not.have.been.called;
          const errorMessage = element.shadowRoot?.querySelector('.error');
          expect(errorMessage?.textContent).to.contain('4242');
        });

        describe('editing', () => {
          const appTracker = {
            id: '123',
            name: 'GitHub - example-org',
            tracker_type: 'github',
            url: 'https://github.com',
            auth_type: 'github_app',
            oauth_installation_id: '11111111-1111-4111-8111-111111111111',
            github_installation_target_login: 'example-org',
            connection_details: {},
            scope_rules: [
              {
                rule_type: 'INCLUDE',
                scope_type: 'ORGANIZATION',
                identifier: '9001',
              },
            ],
          };

          it('does not ask for an API token', async () => {
            const { api: stubbedApi } = buildApi();
            element = await fixture(
              html`<add-tracker-modal
                ._api=${stubbedApi}
                .tracker=${appTracker}
              ></add-tracker-modal>`
            );
            await element.updateComplete;

            expect(
              element.shadowRoot?.querySelector('sl-input[name="api_key"]')
            ).to.not.exist;
            expect(
              element.shadowRoot?.querySelector(
                'sl-select[name="installation"]'
              )
            ).to.not.exist;
            expect(element.shadowRoot?.textContent).to.contain('example-org');
          });

          it('lists owners through the tracker installation and saves without a token', async () => {
            const { stubs, api: stubbedApi } = buildApi();
            stubs.validateTrackerToken.resolves({
              success: true,
              orgs: [{ id: '9001', name: 'example-org', type: 'Organization' }],
            });
            stubs.updateTracker.resolves({ id: '123' });

            element = await fixture(
              html`<add-tracker-modal
                ._api=${stubbedApi}
                .tracker=${appTracker}
              ></add-tracker-modal>`
            );
            await element.updateComplete;

            const nextButton = element.shadowRoot?.querySelector<HTMLElement>(
              'sl-button[variant="primary"]'
            );
            nextButton?.click();
            await element.updateComplete;
            await element.updateComplete;

            expect(stubs.completeGitHubInstallation).to.not.have.been.called;
            expect(stubs.getGitHubInstallations).to.not.have.been.called;
            expect(stubs.validateTrackerToken).to.have.been.calledOnceWith(
              'github',
              'unchanged',
              'https://github.com',
              undefined,
              '123'
            );
            const orgItem = element.shadowRoot?.querySelector<HTMLElement>(
              'sl-tree-item[value="9001"]'
            );
            expect(orgItem).to.exist;
            expect(orgItem?.hasAttribute('selected')).to.be.true;

            const saveButton = element.shadowRoot?.querySelector<HTMLElement>(
              'sl-button[variant="primary"]'
            );
            const listener = oneEvent(element, 'tracker-updated');
            saveButton?.click();
            await listener;

            expect(stubs.updateTracker).to.have.been.calledOnce;
            const [trackerId, payload] = stubs.updateTracker.firstCall.args;
            expect(trackerId).to.equal('123');
            expect(payload.auth_type).to.equal('github_app');
            expect(payload).to.not.have.property('api_key');
            expect(payload.scope_rules).to.deep.equal([
              {
                rule_type: 'INCLUDE',
                scope_type: 'ORGANIZATION',
                identifier: '9001',
              },
            ]);
          });

          it('keeps the existing scope when the installation sees no repositories', async () => {
            const { stubs, api: stubbedApi } = buildApi();
            stubs.validateTrackerToken.resolves({ success: true, orgs: [] });

            element = await fixture(
              html`<add-tracker-modal
                ._api=${stubbedApi}
                .tracker=${appTracker}
              ></add-tracker-modal>`
            );
            await element.updateComplete;

            const nextButton = element.shadowRoot?.querySelector<HTMLElement>(
              'sl-button[variant="primary"]'
            );
            nextButton?.click();
            await waitUntil(
              () => !!element.shadowRoot?.querySelector('.error'),
              'error message did not render'
            );

            expect(stubs.validateTrackerToken).to.have.been.calledOnce;
            const errorMessage = element.shadowRoot?.querySelector('.error');
            expect(errorMessage?.textContent).to.contain(
              'no accessible repositories'
            );
            // Still on step 1: Save is not offered, so scope_rules: [] is
            // never written.
            expect(element.shadowRoot?.querySelector('h2')).to.not.exist;
            expect(
              element.shadowRoot
                ?.querySelector('sl-button[variant="primary"]')
                ?.textContent?.trim()
            ).to.equal('Next');
            expect(stubs.updateTracker).to.not.have.been.called;
          });

          it('shows the backend error detail when listing owners fails', async () => {
            const { stubs, api: stubbedApi } = buildApi();
            stubs.validateTrackerToken.rejects(
              new Error(
                'Tracker is bound to an OAuth App installation that no longer exists.'
              )
            );

            element = await fixture(
              html`<add-tracker-modal
                ._api=${stubbedApi}
                .tracker=${appTracker}
              ></add-tracker-modal>`
            );
            await element.updateComplete;

            element.shadowRoot
              ?.querySelector<HTMLElement>('sl-button[variant="primary"]')
              ?.click();
            await waitUntil(
              () => !!element.shadowRoot?.querySelector('.error'),
              'error message did not render'
            );

            expect(
              element.shadowRoot?.querySelector('.error')?.textContent
            ).to.contain('no longer exists');
            expect(stubs.updateTracker).to.not.have.been.called;
          });
        });

        describe('existing installation picker', () => {
          const pickerSelector = 'sl-select[name="installation"]';
          const waitForPicker = async (el: AddTrackerModal) => {
            await waitUntil(
              () => !!el.shadowRoot?.querySelector(pickerSelector),
              'installation picker did not render'
            );
            return el.shadowRoot?.querySelector<HTMLSelectElement>(
              pickerSelector
            );
          };

          it('offers registered installations beside Connect with GitHub', async () => {
            const { api: stubbedApi } = buildApi();
            element = await fixture(
              html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
            );
            const picker = await waitForPicker(element);
            expect(element.shadowRoot?.textContent).to.contain(
              'Connect with GitHub'
            );
            const options = picker?.querySelectorAll('sl-option');
            expect(options?.length).to.equal(2);
            expect(options?.[0].getAttribute('value')).to.equal('4242');
            expect(options?.[0].textContent).to.contain('example-org');
            expect(options?.[1].textContent).to.contain('jane-doe');
          });

          it('binds the chosen installation and saves through the App path', async () => {
            const { stubs, api: stubbedApi } = buildApi();
            stubs.addTracker.resolves({ id: '789' });
            element = await fixture(
              html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
            );
            const picker = await waitForPicker(element);
            picker!.value = '4343';
            picker?.dispatchEvent(new Event('sl-change'));
            await element.updateComplete;

            expect(element.githubInstallationId).to.equal('4343');
            expect(element.githubTargetLogin).to.equal('jane-doe');
            expect(element.shadowRoot?.textContent).to.contain('jane-doe');

            const nextButton = element.shadowRoot?.querySelector<HTMLElement>(
              'sl-button[variant="primary"]'
            );
            const listener = oneEvent(element, 'tracker-added');
            nextButton?.click();
            await listener;

            expect(
              stubs.completeGitHubInstallation
            ).to.have.been.calledOnceWith({ installation_id: '4343' });
            expect(stubs.validateTrackerToken).to.not.have.been.called;
            const payload = stubs.addTracker.firstCall.args[0];
            expect(payload.auth_type).to.equal('github_app');
            expect(payload.github_installation_id).to.equal('4343');
            expect(payload.scope_rules).to.deep.equal([
              {
                rule_type: 'INCLUDE',
                scope_type: 'ORGANIZATION',
                identifier: '9002',
              },
            ]);
          });

          it('annotates installations already bound to a tracker', async () => {
            const { api: stubbedApi } = buildApi();
            element = await fixture(
              html`<add-tracker-modal
                ._api=${stubbedApi}
                .existingTrackers=${[
                  {
                    id: 'tracker-1',
                    name: 'GitHub - example-org',
                    tracker_type: 'github',
                    created: '2024-01-01T00:00:00Z',
                    is_valid: true,
                    oauth_installation_id:
                      '11111111-1111-4111-8111-111111111111',
                  },
                ]}
              ></add-tracker-modal>`
            );
            const picker = await waitForPicker(element);
            const options = picker?.querySelectorAll('sl-option');
            expect(options?.length).to.equal(2);
            expect(options?.[0].textContent).to.contain('example-org');
            expect(options?.[0].textContent).to.contain('already tracking');
            expect(options?.[0].hasAttribute('disabled')).to.be.false;
            expect(options?.[1].textContent).to.contain('jane-doe');
            expect(options?.[1].textContent).to.not.contain('already tracking');
          });

          it('is hidden when no installation is registered yet', async () => {
            const { stubs, api: stubbedApi } = buildApi();
            stubs.getGitHubInstallations.resolves([]);
            element = await fixture(
              html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
            );
            await waitUntil(
              () =>
                !!element.shadowRoot?.textContent?.includes(
                  'Connect with GitHub'
                ),
              'Connect with GitHub did not render'
            );
            expect(stubs.getGitHubInstallations).to.have.been.calledOnce;
            expect(element.shadowRoot?.querySelector(pickerSelector)).to.not
              .exist;
          });
        });
      });

      describe('Cancel/Close', () => {
        it('dispatches close-modal event on cancel click', async () => {
          element = await fixture(
            html`<add-tracker-modal></add-tracker-modal>`
          );
          await element.updateComplete;

          const cancelButton = element.shadowRoot?.querySelector<HTMLElement>(
            'sl-button:not([variant="primary"])'
          );
          const listener = oneEvent(element, 'close-modal');
          cancelButton?.click();
          await listener;
        });

        it('dispatches close-modal event on dialog close', async () => {
          element = await fixture(
            html`<add-tracker-modal></add-tracker-modal>`
          );
          await element.updateComplete;

          const dialog = element.shadowRoot?.querySelector('sl-dialog');
          const listener = oneEvent(element, 'close-modal');
          dialog?.dispatchEvent(new CustomEvent('sl-request-close'));
          await listener;
        });
      });
    });
  });
});
