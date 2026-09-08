import { expect } from '@open-wc/testing';
import {
  answerFieldCount,
  formatAnswerValue,
  questionFormSummary,
} from './question-form';

/**
 * A list row cannot show a form, so it has to say how big the job is. And a
 * decided request has to show what was answered as fields a person can read,
 * not as the JSON the agent consumed.
 */
describe('question-form helpers', () => {
  describe('questionFormSummary', () => {
    it('is null for a request with no form, so rows render as before', () => {
      expect(questionFormSummary({})).to.equal(null);
    });

    it('counts the items to pick from and the fields to fill in', () => {
      const summary = questionFormSummary({
        question_items: [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }],
        question_schema: {
          type: 'object',
          properties: {
            waived: { type: 'array' },
            reason: { type: 'string' },
          },
        },
      });
      expect(summary).to.equal('4 items to pick from, 2 fields to fill in');
    });

    it('says one item without the plural', () => {
      expect(
        questionFormSummary({
          question_items: [{ id: 'a' }],
          question_schema: { type: 'object', properties: {} },
        })
      ).to.equal('1 item to pick from');
    });

    it('does not count fields the platform fills in', () => {
      expect(
        answerFieldCount({
          type: 'object',
          properties: {
            reason: { type: 'string' },
            approver: { type: 'string', 'x-autofill': 'author' },
            decided: { type: 'string', 'x-autofill': 'date' },
          },
        })
      ).to.equal(1);
    });
  });

  describe('formatAnswerValue', () => {
    it('reads booleans as words', () => {
      expect(formatAnswerValue(true)).to.equal('Yes');
      expect(formatAnswerValue(false)).to.equal('No');
    });

    it('flattens the rows of a multi-select answer', () => {
      expect(
        formatAnswerValue([
          { id: 'CVE-1', reason: 'no fix yet' },
          { id: 'CVE-2', reason: 'not reachable' },
        ])
      ).to.equal(
        'id: CVE-1 · reason: no fix yet, id: CVE-2 · reason: not reachable'
      );
    });

    it('says so when a list came back empty', () => {
      expect(formatAnswerValue([])).to.equal('(none)');
    });
  });
});
