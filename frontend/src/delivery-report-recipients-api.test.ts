import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import { invalidateApiCaches } from './api';
import {
  getReportRecipients,
  saveReportRecipients,
} from './delivery-report-recipients-api';
import { DeliveryReportPolicyError } from './delivery-report-policy-api';

describe('report recipients API', () => {
  beforeEach(() => localStorage.setItem('accessToken', 'synthetic-test-token'));
  afterEach(() => {
    localStorage.removeItem('accessToken');
    sinon.restore();
    invalidateApiCaches();
  });
  it('reads the authenticated recipients endpoint', async () => {
    const data = {
      members: [],
      per_member: false,
      recipients: 'account_owner',
      sponsoring_lead_user_id: null,
      opted_out_user_ids: [],
    };
    const stub = sinon
      .stub(window, 'fetch')
      .resolves(new Response(JSON.stringify(data)));
    expect(await getReportRecipients()).to.deep.equal(data);
    expect(String(stub.firstCall.args[0])).to.contain(
      '/api/v1/delivery-report/recipients'
    );
    expect(
      new Headers(stub.firstCall.args[1]?.headers).get('Authorization')
    ).to.equal('Bearer synthetic-test-token');
  });
  it('sends only the changed role, including explicit null to reset an override', async () => {
    const stub = sinon.stub(window, 'fetch').resolves(new Response('{}'));
    await saveReportRecipients({ roles: { 'synthetic-user': null } });
    expect(stub.firstCall.args[1]?.method).to.equal('PUT');
    expect(JSON.parse(String(stub.firstCall.args[1]?.body))).to.deep.equal({
      roles: { 'synthetic-user': null },
    });
  });
  it('preserves the server refusal code and message', async () => {
    sinon.stub(window, 'fetch').resolves(
      new Response(
        JSON.stringify({
          detail: { code: 'owner_role', message: 'The owner role is fixed.' },
        }),
        { status: 422 }
      )
    );
    let caught: unknown;
    try {
      await saveReportRecipients({ roles: { 'synthetic-user': 'viewer' } });
    } catch (error) {
      caught = error;
    }
    expect(caught).to.be.instanceOf(DeliveryReportPolicyError);
    expect((caught as DeliveryReportPolicyError).code).to.equal('owner_role');
    expect((caught as Error).message).to.equal('The owner role is fixed.');
  });
  it('handles an unavailable plugin returning a non-JSON error', async () => {
    sinon
      .stub(window, 'fetch')
      .resolves(new Response('Not found', { status: 404 }));
    let caught: unknown;
    try {
      await getReportRecipients();
    } catch (error) {
      caught = error;
    }
    expect(caught).to.be.instanceOf(DeliveryReportPolicyError);
    expect((caught as DeliveryReportPolicyError).status).to.equal(404);
    expect((caught as Error).message).to.contain('report recipients');
  });
});
