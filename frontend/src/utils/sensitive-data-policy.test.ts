/**
 * The Sensitive data tab writes only the `sensitive_data` block. These
 * tests pin the YAML a form produces, that loading it back gives the same
 * form, and that keys and rules the form does not know survive a save.
 */
import { expect } from '@open-wc/testing';
import { parse } from 'yaml';
import {
  blockToForm,
  emptyForm,
  formErrors,
  formToBlock,
  isValidJsonPath,
  readSensitiveData,
  rekeyType,
  storedForm,
  summarize,
  withSensitiveData,
} from './sensitive-data-policy';

describe('sensitive-data-policy', () => {
  it('turns a checked type and action into one console rule', () => {
    const form = emptyForm();
    form.types.email = 'redact';
    form.types.credit_card = 'deny';
    form.types.iban = 'deny';
    expect(formToBlock(form, undefined)).to.deep.equal({
      rules: [
        {
          id: 'console-deny',
          on: ['model.request', 'model.response', 'tool.args', 'tool.result'],
          types: ['credit_card', 'iban'],
          action: 'deny',
        },
        {
          id: 'console-redact',
          on: ['model.request', 'model.response', 'tool.args', 'tool.result'],
          types: ['email'],
          action: 'redact',
        },
      ],
    });
  });

  it('writes scope, locales, patterns, keywords and reference-only entries', () => {
    const form = emptyForm();
    form.types.national_id = 'notify';
    form.locales = ['de'];
    form.customPatterns = [{ name: 'employee_id', regex: 'EMP-\\d{6}' }];
    form.keywords = [{ name: 'codenames', terms: [' Bluebird', 'Nightjar '] }];
    form.scopeMode = 'targets';
    form.scope = {
      agents: ['ignored'],
      tools: ['get_patient_record'],
      servers: [],
    };
    form.on = ['tool.args'];
    form.referenceOnly = [
      {
        id: 'patient-tools',
        scope: { agents: [], tools: ['get_patient_record'], servers: ['ehr'] },
        keepFields: ['$.consent_id', ''],
        approverView: 'original_until_decided',
      },
    ];
    expect(formToBlock(form, undefined)).to.deep.equal({
      detectors: {
        locales: ['de'],
        custom_patterns: [{ name: 'employee_id', regex: 'EMP-\\d{6}' }],
        keywords: [{ name: 'codenames', terms: ['Bluebird', 'Nightjar'] }],
      },
      rules: [
        {
          id: 'console-notify',
          on: ['tool.args'],
          types: ['national_id'],
          action: 'notify',
          scope: { tools: ['get_patient_record'] },
        },
      ],
      reference_only: [
        {
          id: 'patient-tools',
          scope: { tools: ['get_patient_record'], servers: ['ehr'] },
          keep_fields: ['$.consent_id'],
          approver_view: 'original_until_decided',
        },
      ],
    });
  });

  it('round trips a stored block and keeps fields it does not show', () => {
    const stored = {
      future_setting: { level: 3 },
      detectors: {
        types: ['email'],
        medical_record_number_pattern: '\\d{8}',
        custom_patterns: [{ name: 'emp', regex: 'E\\d+', flags: ['i'] }],
      },
      rules: [
        {
          id: 'console-redact',
          on: ['tool.args'],
          types: ['email'],
          action: 'redact',
          redact_upstream: true,
          detector_timeout_ms: 900,
          scope: { agents: ['a1'] },
        },
        { id: 'hand-written', on: ['tool.result'], action: 'deny', extra: 1 },
      ],
      reference_only: [
        {
          id: 'refs',
          scope: { tools: ['t'] },
          keep_fields: ['$.id'],
          approver_view: 'redacted',
          salt_hint: 'x',
        },
      ],
    };
    const form = blockToForm(stored);
    expect(form.types).to.deep.equal({ email: 'redact' });
    expect(form.on).to.deep.equal(['tool.args']);
    expect(form.scopeMode).to.equal('agents');
    expect(form.scope.agents).to.deep.equal(['a1']);
    expect(form.customPatterns).to.deep.equal([
      { name: 'emp', regex: 'E\\d+' },
    ]);
    expect(form.referenceOnly[0].keepFields).to.deep.equal(['$.id']);
    expect(formToBlock(form, stored)).to.deep.equal(stored);
  });

  it('leaves the default approver view out unless it was written', () => {
    const form = emptyForm();
    form.referenceOnly = [
      {
        id: 'refs',
        scope: { agents: [], tools: ['t'], servers: [] },
        keepFields: [],
        approverView: 'redacted',
      },
    ];
    expect(formToBlock(form, undefined)).to.deep.equal({
      reference_only: [{ id: 'refs', scope: { tools: ['t'] } }],
    });
    form.referenceOnly[0].keepFields = Array.from(
      { length: 33 },
      (_, i) => `$.f${i}`
    );
    expect(formErrors(form)['ref-0-fields']).to.equal(
      'Keep at most 32 fields per entry.'
    );
  });

  it('drops the console rule when its last type is unchecked', () => {
    const stored = {
      rules: [
        {
          id: 'console-redact',
          on: ['tool.args'],
          types: ['email'],
          action: 'redact',
        },
      ],
    };
    const form = blockToForm(stored);
    delete form.types.email;
    expect(formToBlock(form, stored)).to.equal(null);
  });

  it('replaces only the sensitive_data key of the policy document', () => {
    const policy =
      'version: "1.0"\n# keep me\ntools: []\nsensitive_data:\n  rules: []\n';
    const out = withSensitiveData(policy, {
      rules: [{ id: 'console-redact' }],
    });
    expect(out).to.contain('# keep me');
    expect(parse(out)).to.deep.equal({
      version: '1.0',
      tools: [],
      sensitive_data: { rules: [{ id: 'console-redact' }] },
    });
    expect(readSensitiveData(out)).to.deep.equal({
      rules: [{ id: 'console-redact' }],
    });
    expect(parse(withSensitiveData(policy, null))).to.deep.equal({
      version: '1.0',
      tools: [],
    });
  });

  it('quotes the on key so YAML 1.1 readers do not see a boolean', () => {
    const out = withSensitiveData('version: "1.0"\n', {
      rules: [{ id: 'console-redact', on: ['tool.args'], action: 'redact' }],
    });
    expect(out).to.contain('"on":');
    expect(out).not.to.match(/^\s+on:/m);
  });

  it('accepts the JSON path subset and flags anything else', () => {
    for (const path of [
      '$.consent_id',
      '$.call.id',
      '$.items[0].id',
      '$.a[*]',
      '$result.consent_id',
      '$result.grant.scope',
      '$result.items[0].id',
    ]) {
      expect(isValidJsonPath(path), path).to.equal(true);
    }
    for (const path of [
      'consent_id',
      '$',
      '$result',
      '$results.id',
      'result.id',
      '$..id',
      '$.a b',
      '$.[0]',
      "$['a']",
    ]) {
      expect(isValidJsonPath(path), path).to.equal(false);
    }
    const form = emptyForm();
    form.referenceOnly = [
      {
        id: 'r',
        scope: { agents: [], tools: ['t'], servers: [] },
        keepFields: ['$.ok', 'not a path', '$result.consent_id'],
        approverView: 'redacted',
      },
    ];
    expect(formErrors(form)).to.deep.equal({
      'ref-0-field-1':
        'Use a path like $.consent_id, $.items[0].id or $result.consent_id.',
    });
  });

  it('rejects custom names the server would reject', () => {
    const form = emptyForm();
    const tooLong = `a${'b'.repeat(64)}`;
    const atCap = `a${'b'.repeat(63)}`;
    form.customPatterns = [
      { name: tooLong, regex: 'x' },
      { name: 'email', regex: 'x' },
      { name: 'Email', regex: 'x' },
      { name: atCap, regex: 'x' },
      { name: 'codename', regex: 'x' },
    ];
    form.keywords = [
      { name: 'email', terms: ['x'] },
      { name: 'codename', terms: ['y'] },
    ];
    const errors = formErrors(form, ['email', 'credit_card']);
    expect(errors[`name-0-${tooLong}`]).to.equal(
      `"${tooLong}" must be 1-64 lower case letters, digits and underscores.`
    );
    expect(errors['name-1-email']).to.equal(
      '"email" is a built-in type; pick another name.'
    );
    expect(errors['name-2-Email']).to.equal(
      '"Email" must be 1-64 lower case letters, digits and underscores.'
    );
    expect(errors[`name-3-${atCap}`]).to.equal(undefined);
    expect(errors['name-4-codename']).to.equal(undefined);
    expect(errors['name-5-email']).to.equal(
      '"email" is a built-in type; pick another name.'
    );
    expect(errors['name-6-codename']).to.equal('"codename" is used twice.');
  });

  it('describes the form in plain language', () => {
    const form = emptyForm();
    form.types.credit_card = 'deny';
    form.types.iban = 'deny';
    form.types.email = 'redact';
    form.on = ['tool.args'];
    form.referenceOnly = [
      {
        id: 'r',
        scope: { agents: [], tools: ['get_patient_record'], servers: [] },
        keepFields: ['$.consent_id', '$result.grant_id'],
        approverView: 'redacted',
      },
    ];
    expect(
      summarize(form, {
        credit_card: 'Card number',
        iban: 'IBAN',
        email: 'Email address',
      })
    ).to.equal(
      'Card number and IBAN matches in tool inputs are blocked. ' +
        'Email address matches in tool inputs are stored as [REDACTED:email]. ' +
        'Calls to get_patient_record keep only consent_id and ' +
        'grant_id (from the result) and a fingerprint.'
    );
  });

  it('re-keys a renamed type and drops a cleared one', () => {
    const form = emptyForm();
    form.types.emp = 'redact';
    rekeyType(form, 'emp', 'employee_id');
    expect(form.types).to.deep.equal({ employee_id: 'redact' });
    rekeyType(form, 'employee_id', '  ');
    expect(form.types).to.deep.equal({});
  });

  it('computes the stored form from spans and actions', () => {
    const text = 'mail a@example.com or 555-0100';
    const matches = [
      { type: 'email', start: 5, end: 18 },
      { type: 'phone', start: 22, end: 30 },
    ];
    expect(
      storedForm(text, matches, { email: 'redact', phone: 'notify' })
    ).to.deep.equal({
      blocked: false,
      text: 'mail [REDACTED:email] or 555-0100',
    });
    expect(
      storedForm(text, matches, { email: 'redact', phone: 'deny' }).blocked
    ).to.equal(true);
  });
});
