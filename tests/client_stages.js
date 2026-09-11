// Execute the actual QML JavaScript functions. This does not claim QML rendering.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');
const root = path.resolve(__dirname, '..');
function library(file, globals = {}) {
  const source = fs.readFileSync(path.join(root, file), 'utf8').replace(/^\.(pragma|import).*$/gm, '');
  const scope = vm.createContext(globals);
  vm.runInContext(source, scope);
  return scope;
}
const I18N = library('i18n.js');
const DeliveryStatus = library('DeliveryStatus.js', {I18N});
// Preserve the existing QML behavior suite as executable regression cases here
// when QtTest is unavailable, without pretending this exercises a Qt component.
const qml = fs.readFileSync(path.join(root, 'tests/qml/tst_delivery.qml'), 'utf8');
const cases = qml.slice(qml.indexOf('  function'), qml.lastIndexOf('}'));
const scope = vm.createContext({DeliveryStatus, verify: assert.ok, compare: assert.strictEqual});
vm.runInContext(cases, scope);
for (const name of Object.keys(scope).filter(n => n.startsWith('test_') && !n.endsWith('_data'))) {
  if (scope[name + '_data']) for (const data of scope[name + '_data']()) scope[name](data);
  else scope[name]();
}
const note = {slug: 'fixture', has_transcript: true, transcription_pending: false,
  transcription_status: 'complete', can_retry: true, retry_stage: 'summary',
  summary_status: 'pending', summary_error: 'Cota diária esgotada', zinom: {status: 'pending'}};
assert.equal(DeliveryStatus.processingLine(note, 'pt'), 'Transcrição concluída · Resumo pendente: Cota diária esgotada');
assert.equal(DeliveryStatus.zinomLine(note, 'pt'), 'Envio ao Zinom pendente');
assert.ok(!DeliveryStatus.uploadComplete(note));
assert.equal(DeliveryStatus.retryLabel(note, 'pt'), 'Retomar resumo');
assert.ok(!DeliveryStatus.processingLine(note, 'pt').includes('Transcrição pendente'));
const stale = {slug: 'fixture', transcription_pending: true, summary_status: 'pending'};
const projected = DeliveryStatus.projectedLastResult(stale, [{...note, summary_status: ''}]);
assert.equal(projected.transcription_pending, false);
assert.equal(projected.summary_status, '');
assert.equal(stale.transcription_pending, true);
assert.equal(I18N.resolveLanguage(''), 'pt');
assert.equal(I18N.resolveLanguage('en_US.UTF-8'), 'en');
assert.equal(I18N.resolveLanguage('pt_BR.UTF-8'), 'pt');
assert.equal(DeliveryStatus.transcriptionLine({...note, transcription_status: 'pending'}, 'pt'), 'Transcrição pendente: 󰑐 tenta de novo');
assert.equal(DeliveryStatus.zinomLine({...note, zinom: {status: 'tombstoned'}}, 'pt'), 'Excluído no Zinom');
assert.equal(DeliveryStatus.zinomLine({...note, zinom: {status: 'pending', facts_ingested: 2}}, 'pt'), 'Envio ao Zinom pendente');
console.log('Shared QML JavaScript regression cases passed');
