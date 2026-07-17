import { expect } from '@open-wc/testing';
import sinon from 'sinon';

import { trackGoal, recordPathChange } from './web-analytics';
import { activityTracker } from './activity-tracker';

type WindowWithPlausible = Window & {
  plausible?: sinon.SinonSpy;
};

const win = window as unknown as WindowWithPlausible;

describe('web-analytics', () => {
  let mirrorStub: sinon.SinonStub;

  beforeEach(() => {
    sessionStorage.clear();
    win.plausible = sinon.spy();
    mirrorStub = sinon.stub(activityTracker, 'trackConversion');
  });

  afterEach(() => {
    delete win.plausible;
    sessionStorage.clear();
    mirrorStub.restore();
  });

  it('fires a plain goal event', () => {
    trackGoal('Signup');
    expect(win.plausible!.calledOnce).to.be.true;
    expect(win.plausible!.firstCall.args[0]).to.equal('Signup');
    expect(win.plausible!.firstCall.args[1]).to.be.undefined;
  });

  it('passes props through', () => {
    trackGoal('Install Copy', { variant: 'Install the CLI' });
    expect(win.plausible!.firstCall.args[1]).to.deep.equal({
      props: { variant: 'Install the CLI' },
    });
  });

  it('is a no-op when the analytics script is absent', () => {
    delete win.plausible;
    expect(() => trackGoal('Signup')).to.not.throw();
  });

  it('never throws when the tracker itself throws', () => {
    win.plausible = sinon.stub().throws(new Error('tracker boom')) as never;
    expect(() => trackGoal('Signup')).to.not.throw();
  });

  it('attaches prev_path after navigating between routes', () => {
    recordPathChange('/pricing');
    recordPathChange('/register');
    trackGoal('Signup');
    expect(win.plausible!.firstCall.args[1]).to.deep.equal({
      props: { prev_path: '/pricing' },
    });
  });

  it('does not overwrite prev_path on same-path repeats', () => {
    recordPathChange('/pricing');
    recordPathChange('/register');
    recordPathChange('/register');
    trackGoal('Signup');
    expect(win.plausible!.firstCall.args[1]).to.deep.equal({
      props: { prev_path: '/pricing' },
    });
  });

  it('has no prev_path on the first page of a session', () => {
    recordPathChange('/');
    trackGoal('Signup Click');
    expect(win.plausible!.firstCall.args[1]).to.be.undefined;
  });

  it('explicit props win over the automatic prev_path', () => {
    recordPathChange('/pricing');
    recordPathChange('/register');
    trackGoal('Signup', { prev_path: '/custom' });
    expect(win.plausible!.firstCall.args[1]).to.deep.equal({
      props: { prev_path: '/custom' },
    });
  });

  describe('first-party mirror', () => {
    it('mirrors every goal into ActivityTracker with the same name', () => {
      trackGoal('Install Copy', { variant: 'cli' });
      expect(mirrorStub.calledOnce).to.be.true;
      expect(mirrorStub.firstCall.args[0]).to.equal('Install Copy');
      expect(mirrorStub.firstCall.args[2]).to.deep.equal({ variant: 'cli' });
    });

    it('mirrors even when the Plausible snippet is absent', () => {
      delete win.plausible;
      trackGoal('Signup');
      expect(mirrorStub.calledOnce).to.be.true;
      expect(mirrorStub.firstCall.args[0]).to.equal('Signup');
    });

    it('includes prev_path in the mirrored metadata', () => {
      recordPathChange('/pricing');
      recordPathChange('/register');
      trackGoal('Signup');
      expect(mirrorStub.firstCall.args[2]).to.deep.equal({
        prev_path: '/pricing',
      });
    });

    it('a throwing mirror never breaks the goal call', () => {
      mirrorStub.throws(new Error('ws boom'));
      expect(() => trackGoal('Signup')).to.not.throw();
      expect(win.plausible!.calledOnce).to.be.true;
    });
  });
});
