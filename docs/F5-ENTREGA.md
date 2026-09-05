# F5, captura durável e memória íntegra

Risco: VERMELHO (persistência e memória). Base comum: `b97131e`.
A primeira entrega foi `b59ced5`; ela não representa o candidato integrado atual.
O candidato integra a linha de agenda `9ca9c6a`, captura durável e correções locais.
Registrar o SHA exato e repetir as provas abaixo antes de instalar. Nenhum AGY executado.

## Critérios e comportamento

- Original copiado com fsync e publicação atômica no Bronze antes de transcrever. Cada captura tem job, hash SHA-256, áudio e proveniência próprios.
- `sync --all` retoma jobs sem depender do áudio temporário e inclui reuniões sem Silver. O limite opcional conta pendências, não apenas as 20 notas recentes.
- Jobs remotos usam chave estável derivada do áudio/modo, resultado persistente e `flock` para evitar processamento concorrente duplicado. SCP: 30 s; cada SSH: 15 s, BatchMode e keepalive. O Whisper trabalha desacoplado da conexão.
- Mock não é fallback automático. Quando explicitamente usado, seu texto fica no job local e é excluído da transcrição real, Silver e entrega. Gravações reais posteriores continuam aproveitáveis. Nenhum histórico é apagado.
- Silêncio posterior não invalida fala anterior. Status e provider são preservados por gravação; a reunião agrega somente as fontes válidas.
- Falha da LLM não converte convidados em participantes nem fabrica fatos.
- Erros HTTP, JSON-RPC e `not_found` genéricos deixam erro recuperável, nunca exclusão presumida.
  Somente código estruturado de exclusão (`source_tombstoned` ou `memory_deleted`) proveniente
  de `brain_update` permite tombstone local. Não há remember substituto. O id anterior sobrevive.
- Notas são enviadas inteiras. Se o servidor rejeitar tamanho, a entrega permanece com erro, nunca sucesso truncado.
- Ausência de credencial deixa entrega pendente. O recibo da nota é persistido assim que chega.
- Fatos atômicos não são enviados: `brain_fact` não oferece linhagem no schema atual. A nota narrativa carrega origem recuperável; os trios ficam em `zinom.facts_pending`, `facts_status=pending_lineage`, com slug, gravações/hashes e recibo remember. Nota entregue não significa fatos concluídos. F4 deverá oferecer o contrato servidor antes de ativar essa etapa.

Schema conferido na tool disponível `zinom_brain_fact` e em `/home/moniz/notion-mcp/src/rag/brain-fact-tool.ts`, somente leitura. Não foram inventados campos na API nem acessado banco.

## Prova

Comandos executados na worktree:

```sh
python3 -m unittest discover -s tests -p '*contract.py' -v
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s tests -p test_durable_jobs.py -v
git diff --check
```

Antes: seis contratos externos falharam; suíte de 96 testes teve sete falhas, incluindo a falha preexistente da CLI que exigia pactl para apagar áudio. A reprodução inicial sem pactl encontrou cinco falhas e erro de dependência; o pacote foi extraído somente em `/tmp/f5-test-deps`, sem instalar serviço. Com pactl no PATH de teste, os seis vermelhos foram reproduzidos.

Primeira entrega `b59ced5`: 112 testes verdes. Esta é evidência HISTÓRICA, não prova do candidato
atual. A revisão Claude conferiu `d2579f6`: 191 testes, com duas falhas na VPS sem `notify-send`;
a suíte no XPS passou integralmente. `95b471c` corrige essa dependência com dois testes negativos.
O P0 da revisão acrescenta testes para HTTP 404, método desconhecido, erro genérico e exclusão
estruturada. Cada sucessor precisa de novo resultado ligado ao SHA, não reutilizar logs antigos.

Dois testes antigos de `test_zinom_adapter.py` exigiam publicar `brain_fact` sem origem. Foram atualizados conforme a decisão explícita do coordenador, exigindo ausência dessa chamada, zero fatos ingeridos, trios preservados, recibo e estado pendente. Nenhum dos dois arquivos de contratos externos foi alterado.

Hashes dos contratos externos:

```text
fb2091ad9458ac248a0179203e345ee5c29bc0b53eb759b6688c4732108ebe56  test_brain_integrity_contract.py
62614181a6817dc84b828680fe0ea50d95bf289833e91d436489a225ac096325  test_capture_durability_contract.py
```

Logs HISTÓRICOS de `b59ced5` em `docs/evidence/f5/`, mantidos para rastreabilidade. Provas posteriores
e limites estão em `docs/RETOMADA-AUTOMATICA.md` e `docs/INGESTAO-BRONZE.md`. Uma entrega deve citar
o commit realmente testado, a família revisora e o resultado XPS/VPS, incluindo lacunas.

## Instalação pelo coordenador, após revisão

1. Revisar o commit com Claude e repetir a suíte intacta. Receber do coordenador externo o diff privado ATUAL da worktree XPS ativa `retry-transcription-and-agenda` e reconciliar explicitamente em release separada; não usar snapshot antigo como substituto. Preparar checkout de release separado pelo Orca ou snapshot do commit em diretório novo. Não trocar branch, limpar, resetar ou puxar alterações na worktree XPS ativa.
2. No XPS, confirmar que não há gravação/finalização ativa. Registrar o destino atual de `~/.local/bin/castanha`, do link do plugin e o estado do daemon. Preservar esses destinos para rollback. Não executar `install.sh`: ele remove diretórios de plugin e poderia alcançar o checkout ativo.
3. Na release isolada, rodar a suíte completa. Conferir `ffmpeg`, `ffprobe`, `pactl` e Python 3.10+. Na VPS de transcrição, conferir `flock`, `nohup` e `/root/castanha-transcribe.py`. Não é necessária migração de banco nem limpeza do cache remoto.
4. Executar `PYTHONPATH=. python3 -B tests/qa_pipewire_capture.py`, versionado desde `9ba8f4b`,
   antes da troca e contra a release instalada. Ele usa dois sinks sintéticos, não microfone físico,
   preserva defaults e limpa os recursos criados. Rodar também os testes Qt em `tests/qml`.
5. Há mudança de QML e de `DeliveryStatus.js`: CLI E PLUGIN devem apontar para a mesma release
   aprovada. Trocar os dois links pelo mecanismo health-gated, com reversão de ambos e do daemon.
   Preservar as worktrees anteriores, mas não manter o painel antigo exibindo sucesso falso.
6. Smoke: CLI `status --json`, leitura de notas, ensaio sintético, painel real e captura com queda
   simulada em armazenamento de teste. Não drenar o acervo real pelo remember legado: ativação da
   retomada e ingestão da Nora exigem contrato F4 revisado e publicação integral com idempotência.

## Reversão e limites

Rollback escrito antes de qualquer instalação: restaurar os links anteriores da CLI E PLUGIN e o
daemon anterior pelo mesmo mecanismo health-gated. Preservar Bronze, Gold, `.jobs`, áudios e recibos
novos. A versão antiga não conhece os checkpoints novos; não usá-la para drenar a fila nem reingerir
capturas mock. Nenhuma reversão envolve apagar memória ou fatos por subject/predicate.

A VPS não tem servidor PipeWire/Pulse. A QA XPS já executou captura sintética com dois canais e os
testes Qt, mas isso não substitui o smoke do painel instalado nem prova identificação de cada voz.
Claude revisou `d2579f6` e reportou um P0 e três P1. Correções precisam de re-review antes da release.

A idempotência comprovada cobre jobs e reenvios com recibo conhecido. O endpoint atual de remember não oferece chave de idempotência para resolver sozinho uma resposta perdida após aceitação remota; esse caso permanece limite do contrato servidor. Fatos seguem bloqueados por ausência de linhagem, sem ativação automática futura.
