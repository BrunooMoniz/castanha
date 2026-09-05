# F5, captura durável e memória íntegra

Risco: VERMELHO (persistência e memória). Branch: `BrunooMoniz/zinom-u-f5-castanha`, base `b97131e`.
Entrega de código para revisão independente pelo coordenador. Não instalado no XPS, nenhum histórico pessoal alterado, nenhum AGY executado.

## Critérios e comportamento

- Original copiado com fsync e publicação atômica no Bronze antes de transcrever. Cada captura tem job, hash SHA-256, áudio e proveniência próprios.
- `sync --all` retoma jobs sem depender do áudio temporário e inclui reuniões sem Silver. O limite opcional conta pendências, não apenas as 20 notas recentes.
- Jobs remotos usam chave estável derivada do áudio/modo, resultado persistente e `flock` para evitar processamento concorrente duplicado. SCP: 30 s; cada SSH: 15 s, BatchMode e keepalive. O Whisper trabalha desacoplado da conexão.
- Mock não é fallback automático. Quando explicitamente usado, seu texto fica no job local e é excluído da transcrição real, Silver e entrega. Gravações reais posteriores continuam aproveitáveis. Nenhum histórico é apagado.
- Silêncio posterior não invalida fala anterior. Status e provider são preservados por gravação; a reunião agrega somente as fontes válidas.
- Falha da LLM não converte convidados em participantes nem fabrica fatos.
- Um `brain_update` com “not found” deixa tombstone local e interrompe a entrega. Não há remember substituto nem fatos posteriores. O id anterior sobrevive a falhas.
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

Depois: 112 testes verdes. Incluem os seis contratos externos intactos e 16 contraprovas adicionais de queda, checkpoint, fila, proveniência, silêncio, mock e transporte. O ensaio remoto executa de verdade os comandos de shell em diretório isolado, com transcritor sintético desacoplado e resultado persistente; confirma uma transcrição e um upload apesar de retries.

Dois testes antigos de `test_zinom_adapter.py` exigiam publicar `brain_fact` sem origem. Foram atualizados conforme a decisão explícita do coordenador, exigindo ausência dessa chamada, zero fatos ingeridos, trios preservados, recibo e estado pendente. Nenhum dos dois arquivos de contratos externos foi alterado.

Hashes dos contratos externos:

```text
fb2091ad9458ac248a0179203e345ee5c29bc0b53eb759b6688c4732108ebe56  test_brain_integrity_contract.py
62614181a6817dc84b828680fe0ea50d95bf289833e91d436489a225ac096325  test_capture_durability_contract.py
```

Logs em `docs/evidence/f5/`.

## Instalação pelo coordenador, após revisão

1. Revisar o commit com Claude e repetir a suíte intacta. Receber do coordenador externo o diff privado ATUAL da worktree XPS ativa `retry-transcription-and-agenda` e reconciliar explicitamente em release separada; não usar snapshot antigo como substituto. Preparar checkout de release separado pelo Orca ou snapshot do commit em diretório novo. Não trocar branch, limpar, resetar ou puxar alterações na worktree XPS ativa.
2. No XPS, confirmar que não há gravação/finalização ativa. Registrar o destino atual de `~/.local/bin/castanha`, do link do plugin e o estado do daemon. Preservar esses destinos para rollback. Não executar `install.sh`: ele remove diretórios de plugin e poderia alcançar o checkout ativo.
3. Na release isolada, rodar a suíte completa. Conferir `ffmpeg`, `ffprobe`, `pactl` e Python 3.10+. Na VPS de transcrição, conferir `flock`, `nohup` e `/root/castanha-transcribe.py`. Não é necessária migração de banco nem limpeza do cache remoto.
4. Executar o ensaio externo `PYTHONPATH=. python3 -B tests/qa_pipewire_capture.py` antes da troca e contra a release instalada. Ele usa dois sinks sintéticos, não microfone físico, preserva defaults e limpa os recursos criados. O coordenador forneceu esse arquivo com hash `a0f72435a24c6312a8dd7ec6ba12f467b9d411a16b2d37d8d3e300e0811b5995`; ele permanece fora do commit por determinação da QA.
5. Publicar pelo mecanismo health-gated do ambiente, apontando a CLI para `bin/castanha` da release e reiniciando somente o daemon Castanha se ele já estava ativo. Como não houve mudança de QML, o checkout ativo do plugin pode permanecer preservado. A troca deve restaurar o destino anterior automaticamente se o smoke falhar.
6. Smoke: CLI `status --json`, leitura de notas, ensaio sintético, captura com queda simulada e retomada em armazenamento de teste. Somente depois rodar `castanha sync --all` no acervo real. Verificar notas com recibo e fatos ainda em `pending_lineage`, sem promessa de ingestão atômica concluída.

## Reversão e limites

Rollback escrito antes de qualquer instalação: restaurar o link anterior da CLI e o daemon anterior pelo mesmo mecanismo health-gated. Preservar Bronze, Gold, `.jobs`, áudios e recibos novos. A versão antiga não conhece os checkpoints novos; não usá-la para drenar a fila nem reingerir capturas mock. Nenhuma reversão envolve apagar memória ou fatos por subject/predicate.

Esta VPS não tem servidor PipeWire/Pulse: o ensaio opt-in parou no primeiro `pactl get-default-source`, Connection refused, antes de criar qualquer sink. Não há prova local de captura física ou de Whisper real nesta entrega. A QA externa repetirá PipeWire contra a release. Revisão por Claude está reservada pelo coordenador e não foi atribuída a esta execução.

A idempotência comprovada cobre jobs e reenvios com recibo conhecido. O endpoint atual de remember não oferece chave de idempotência para resolver sozinho uma resposta perdida após aceitação remota; esse caso permanece limite do contrato servidor. Fatos seguem bloqueados por ausência de linhagem, sem ativação automática futura.
