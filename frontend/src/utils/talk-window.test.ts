import { expect } from '@open-wc/testing';
import sinon from 'sinon';

import {
  TALK_WINDOW_DEFAULT_HEIGHT,
  TALK_WINDOW_DEFAULT_WIDTH,
  openTalkWindow,
  readTalkWindowGeometry,
  resetTalkWindowsForTests,
  saveTalkWindowGeometry,
  talkRoutePath,
  talkWindowFeatures,
  talkWindowGeometryKey,
  talkWindowName,
  talkWindowUrl,
} from './talk-window';

describe('talk-window', () => {
  const agent = { id: 'agent-1', display_name: 'Hermes' };
  let openStub: sinon.SinonStub;

  beforeEach(() => {
    resetTalkWindowsForTests();
    localStorage.removeItem(talkWindowGeometryKey('agent-1'));
    openStub = sinon.stub(window, 'open');
  });

  afterEach(() => {
    sinon.restore();
    resetTalkWindowsForTests();
  });

  it('builds the route, the window url and the window name', () => {
    expect(talkRoutePath('agent-1')).to.equal('/console/agents/agent-1/talk');
    expect(talkRoutePath('agent-1', { id: 'sess-2' })).to.equal(
      '/console/agents/agent-1/talk?session=sess-2'
    );
    expect(talkWindowUrl('agent-1', 'sess-2')).to.equal(
      '/console/agents/agent-1/talk?session=sess-2&window=1'
    );
    expect(talkWindowUrl('agent-1')).to.equal(
      '/console/agents/agent-1/talk?window=1'
    );
    expect(talkWindowName('agent-1')).to.equal('preloop-talk-agent-1');
  });

  it('defaults to a 520x720 popup offset from the opener', () => {
    const geometry = readTalkWindowGeometry('agent-1');
    expect(geometry.width).to.equal(TALK_WINDOW_DEFAULT_WIDTH);
    expect(geometry.height).to.equal(TALK_WINDOW_DEFAULT_HEIGHT);
    const features = talkWindowFeatures(geometry);
    expect(features).to.contain('popup=yes');
    expect(features).to.contain('width=520');
    expect(features).to.contain('height=720');
    expect(features).to.contain(`left=${geometry.left}`);
    expect(features).to.contain(`top=${geometry.top}`);
  });

  it('round trips geometry through localStorage', () => {
    saveTalkWindowGeometry('agent-1', {
      width: 640.4,
      height: 800.6,
      left: 12,
      top: 34,
    });
    expect(readTalkWindowGeometry('agent-1')).to.deep.equal({
      width: 640,
      height: 801,
      left: 12,
      top: 34,
    });
  });

  it('ignores nonsense stored geometry', () => {
    localStorage.setItem(talkWindowGeometryKey('agent-1'), 'not json');
    expect(readTalkWindowGeometry('agent-1').width).to.equal(
      TALK_WINDOW_DEFAULT_WIDTH
    );
    localStorage.setItem(
      talkWindowGeometryKey('agent-1'),
      JSON.stringify({ width: 10, height: 10 })
    );
    expect(readTalkWindowGeometry('agent-1').height).to.equal(
      TALK_WINDOW_DEFAULT_HEIGHT
    );
  });

  it('opens a named popup with the saved geometry', () => {
    saveTalkWindowGeometry('agent-1', {
      width: 600,
      height: 700,
      left: 20,
      top: 30,
    });
    const fakeWindow = { closed: false, focus: sinon.spy() };
    openStub.returns(fakeWindow);

    const result = openTalkWindow(agent, { id: 'sess-2' });

    expect(result.outcome).to.equal('window');
    expect(openStub.calledOnce).to.be.true;
    const [url, name, features] = openStub.firstCall.args;
    expect(url).to.equal(
      '/console/agents/agent-1/talk?session=sess-2&window=1'
    );
    expect(name).to.equal('preloop-talk-agent-1');
    expect(features).to.contain('width=600');
    expect(features).to.contain('height=700');
    expect(features).to.contain('left=20');
    expect(features).to.contain('top=30');
    expect(fakeWindow.focus.calledOnce).to.be.true;
  });

  it('focuses the window a second click instead of opening another', () => {
    const fakeWindow = { closed: false, focus: sinon.spy() };
    openStub.returns(fakeWindow);

    openTalkWindow(agent);
    const second = openTalkWindow(agent);

    expect(second.outcome).to.equal('focused');
    expect(openStub.calledOnce).to.be.true;
    expect(fakeWindow.focus.callCount).to.equal(2);
  });

  it('offers a new tab when the popup is blocked', () => {
    openStub.returns(null);

    const result = openTalkWindow(agent);

    expect(result.outcome).to.equal('blocked');
    const alert = document.querySelector('sl-alert');
    expect(alert, 'a toast explains the block').to.exist;
    expect(alert!.textContent).to.contain('Your browser blocked the window');
    const action = alert!.querySelector('[data-toast-action]');
    expect(action, 'the toast carries the fallback').to.exist;
    expect(action!.textContent!.trim()).to.equal('Open in a new tab');

    openStub.resetHistory();
    openStub.returns({ closed: false, focus: () => {} });
    action!.dispatchEvent(new Event('click'));
    expect(openStub.firstCall.args[0]).to.equal('/console/agents/agent-1/talk');
    expect(openStub.firstCall.args[1]).to.equal('_blank');
    alert!.remove();
  });

  it('navigates instead of opening a window on a phone', async () => {
    const router = await import('@vaadin/router');
    const goStub = sinon.stub(router.Router, 'go');
    sinon.stub(window, 'innerWidth').value(390);

    const result = openTalkWindow(agent, 'sess-9');

    expect(result.outcome).to.equal('navigated');
    expect(openStub.called).to.be.false;
    expect(goStub.firstCall.args[0]).to.equal(
      '/console/agents/agent-1/talk?session=sess-9'
    );
  });
});
