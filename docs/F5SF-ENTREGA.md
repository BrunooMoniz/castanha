# F5SF: integridade do retry e recuperação da captura

Risco: VERMELHO na instalação. Base exata: `bf7b927c3697dc73da9ac00daf63ead8f1a7aaa8`.
Escopo ampliado pelo coordenador para os achados de F5S4 e F5SS. Implementador: Codex.

- Retry legado preserva a origem de cada gravação antes de alterar o provider agregado. Texto conhecido como mock fica em `.mock-history`, junto dos metadados e das notas anteriores, sem alterar o áudio. Como o legado não identifica a origem de cada trecho, um agregado que contém mock é isolado por inteiro. Bronze agregado, Silver, Gold e envio passam a conter somente o texto real recuperado. Resposta mock nova também fica separada.
- Uma fatia Groq sem campo `text` do tipo string provoca erro recuperável ou fallback. A primeira fatia válida não basta para concluir o job inteiro. Texto vazio explícito continua aceito como resposta sem fala; payload ausente não equivale a silêncio.
- Sync manual e inventário da retomada automática reconciliam a captura cujo último job já foi concluído. A publicação de `idle` acontece sob o lock da reunião e exige PID inteiro positivo comprovadamente morto por `ProcessLookupError`. PID vivo, ilegível, reutilizado ou inacessível não autoriza limpar o estado. Reuniões já entregues ou tombstonadas também são reconciliadas, sem reenvio.
- Pendência de token não herda a confirmação de entrega da nota anterior. O id antigo continua disponível para `brain_update`, com fatos e origem preservados. Novos recibos confirmados registram o hash local dos metadados e das notas; a fila só suprime a espera de fatos quando o conteúdo continua igual. Recibos legados sem hash mantêm compatibilidade, e uma tentativa pendente invalida sua confirmação para o conteúdo atual. Pendências antigas cujo motivo já registra token ausente também voltam à fila, mesmo com status de nota herdado.

## Prova e revisão

Os probes dos dois P1 F5SS foram incorporados como testes duráveis. Rodados contra a base original, os dois testes falham com sete contraprovas, incluindo as três entradas de recuperação com e sem entrega anterior. A suíte original teve 198 testes verdes antes das alterações.

Os testes novos também cobrem mock novo depois de texto real, retry real indisponível, morte antes de gerar Silver, proveniência e áudio intactos, nova captura liberada, ausência de reenvio, identidade reavaliada após adquirir o lock, lock mantido durante a escrita de idle, payloads Groq inválidos e recebimento de conteúdo novo depois da restauração do token.

Os seis contratos independentes, nos dois arquivos abaixo, permanecem byte-idênticos:

```text
fb2091ad9458ac248a0179203e345ee5c29bc0b53eb759b6688c4732108ebe56  tests/test_brain_integrity_contract.py
62614181a6817dc84b828680fe0ea50d95bf289833e91d436489a225ac096325  tests/test_capture_durability_contract.py
```

Resultados finais e SHA do candidato serão registrados no relatório externo desta entrega. A revisão fresca solicitada é Codex review e skeptic, sob waiver informado pelo coordenador. Isso não constitui revisão por outra família.

## Reversão e limites

Não instalar neste trabalho. Antes de futura instalação, preservar a release anterior e usar o mecanismo health-gated do projeto. Reversão restaura os links e o daemon da release anterior, preservando Bronze, áudio, `.jobs`, `.mock-history` e recibos. Não drenar os dados novos com a implementação antiga, que não conhece o isolamento do legado. Nenhum histórico deve ser apagado.

Flags permanecem OFF; não houve instalação, push, ativação, acesso a acervo real ou chamadas ao servidor de memória. Testes usam armazenamento temporário, áudio fictício e transporte substituído. Não houve mudança de UI, bridge Bronze, channel_transcription, implementação VPS nem scripts de deployment/transition. Qt, PipeWire e desktop XPS instalado não fazem parte desta prova. A resposta perdida depois da aceitação remota de `remember` continua dependendo de idempotência no servidor.

## F5SF2: contrato seguro no retry da CLI

Risco: AMARELO. Base exata: `61e5308427a6b3ff7e01756e58899fd6437c2a65`.
A única falha da full anterior foi reproduzida isoladamente: o teste esperava
saída 0 de uma transcrição mock, mas a CLI devolvia corretamente 2.

A cobertura agora diferencia os dois resultados. Mock fica em `.mock-history`,
sem entrar no texto agregado, nas notas Silver/Gold ou na origem para memória;
a reunião segue sem conclusão e o áudio permanece intacto. A tentativa positiva
usa uma resposta controlada, explicitamente identificada como `real-fake` e
atestada como fixture apenas no subprocesso de teste. Ela exercita o parser da
CLI, o engine, a leitura do áudio Ogg e a persistência reais, sem transcrição
externa. Esse atestado não é uma nova capacidade de produção nem prova de
reconhecimento de fala.

O teste positivo primeiro comprova a falha sem transcritor, depois recupera a
pendência com `retry` sem slug. Confere a origem por gravação, o texto único,
Silver/Gold, o áudio preservado e a remoção da pendência. Outra chamada sem slug
retorna lista vazia e preserva byte a byte metadados, transcrição e notas.

Mudança restrita a teste e documentação. Engine, CLI de produção,
`MockTranscriber` e os seis contratos independentes permanecem inalterados.
Resultados dos focados, da única full autorizada pelo slot e SHA do sucessor
ficam no relatório externo. Reviews serão conduzidos pelo coordenador depois
desta entrega; este incremento não declara revisão independente concluída.
Sem rede, acervo real, instalação ou deploy.
