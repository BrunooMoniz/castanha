# F13, prova sintética de retomada real

Risco VERMELHO, limitado a áudio sintético e Postgres descartável. Nenhum arquivo
de produto mudou. Cliente base `011b30a`; fixture de síntese e texto de geração
versionados em `19f72c7442ee093bdb20e8c5b688f631ae61d457`.

## Observado em 06/09/2026

- FLAC original SHA256 `6800c9c62e64f37a160058528616600b1a624c1fb045724f8c7488a894d63fd8`, preservado.
- Worker instalado SHA256 `e38007da8ac6fd17f0fd0b99f39618c15cb8a2bc6e5087209970839fe76ba422`, sem alteração.
- Uma submissão real SSH/SCP ao large-v3 local: um upload, um lançamento.
- O canário encerrou somente seu próprio processo SSH após o lançamento detached.
  O cliente recebeu pendência, não texto inventado.
- Terminal Orca independente na VPS (`term_1fb6a579-5fc4-4635-bdf9-e3634dd12688`)
  observou o worker PID 2777157 ainda executando e depois o resultado concluído.
- Job `c8529b69d29e265535685876474219aff4b8376a9fc2b30901bb3b1f714c3951`.
  Replay pelo cliente: uma consulta SSH, zero uploads e zero relançamentos.
- Hash e mtime_ns do resultado remoto permaneceram iguais após replay.
- `build_transcript_request` real produziu o envelope integral, sem fatos inventados.
  A suíte complementar do engine `castanha-recovery-http.integration.test.ts`
  enviou-o ao app HTTP/MCP real com autenticação e Postgres real isolado.
  Dois replays deixaram exatamente um documento, um job e uma chave; o texto
  integral foi confirmado no banco e consultado por `brain_get_document` HTTP.

Teste complementar do engine: commit `8ad5324d7e232481f30ea4641375d6ada034e8ed`,
sobre `e63e75e`. Executar somente em banco de sucata migrado:

```sh
F13_SCRATCH_PG_URL=postgresql://postgres@127.0.0.1:32768/f13_asr_recovery_it \
F13_CASTANHA_REQUEST="$PASTA_DE_PROVA/bronze-request.json" \
node_modules/.bin/tsx --test src/__tests__/castanha-recovery-http.integration.test.ts
```

Sem essas duas variáveis, o teste é marcado como não executado, nunca como
integração validada. O banco não é truncado e cada execução usa uma conta
sintética nova. Jobs pendentes preexistentes bloqueiam o ensaio.

Recibos sintéticos completos estão em `fixtures/f13-real-recovery.json`.
`python3 -B -m unittest tests.test_f13_evidence -v` verifica sua coerência
sem alegar que repetir uma fixture equivale a repetir o serviço real.

## Repetição explícita do canário

Não execute automaticamente em testes unitários. Exige aprovação de uma inferência
local. O script aceita somente o hash do áudio sintético acima, nunca reunião real.

```sh
python3 -B tests/f13_real_recovery.py submit --audio "$AUDIO_SINTETICO" \
  --host zinom-vps-2 --evidence "$PASTA_NOVA_DE_PROVA"
```

A submissão grava intenção antes de agir. Não repita `submit` se houver intenção,
resultado prévio ou dúvida: preserve o mesmo job e inspecione. Em um terminal
independente na VPS, usando o mesmo script e o código Castanha no PYTHONPATH:

```sh
python3 -B tests/f13_real_recovery.py observe --job "$JOB_ID" --evidence "$RECIBO_NOVO"
```

Depois, no cliente:

```sh
python3 -B tests/f13_real_recovery.py replay --audio "$AUDIO_SINTETICO" \
  --host zinom-vps-2 --evidence "$PASTA_DE_PROVA"
```

`observe` só lê e `replay` recusa qualquer segunda chamada de transporte,
upload ou lançamento. Se o resultado ainda não existir, use `observe`.

## Limites e reversão

O XPS ficou ligado. Foi interrompida a conexão do canário, não a rede global,
o runtime Orca ou o worker. O terminal independente prova consulta pela VPS,
não uma resposta Telegram/WhatsApp. Silver/Gold ainda dependem do finalizador
local e não foram exercitados. Um canal mono foi processado nesta prova;
submissão dupla continua coberta separadamente por testes com provider fixture.

Qualidade lexical não é perfeita: a fixture anterior encontrou alteração de
singular/plural. Aqui não chamamos ausência lexical de omissão nem avaliamos
ranking semântico. O engine usa vetor zero para não chamar modelo externo;
HTTP, auth, chunker, transações e recuperação de documento são reais.

Reversão operacional: encerrar somente os processos/terminal do canário que
este ensaio criou. Preservar áudio, request, resultado e recibos. Não apagar jobs
alheios, não editar o worker instalado, não parar o container PG compartilhado.
O banco exclusivo `f13_asr_recovery_it` foi mantido como evidência. Uma tentativa
inicial do harness usou `job.account_id` em vez de `job.accountId`; foram removidas
somente suas três linhas sintéticas antes da repetição corrigida, sem dados reais.
