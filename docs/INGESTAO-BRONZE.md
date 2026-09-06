# Ponte Castanha para ingestão Bronze

Risco: **VERMELHO na operação**, porque a ativação escreve memória real. Esta
entrega é código local, com as flags desligadas, sem instalação, push ou deploy.
Base exata: `db9d1568076f6695640e70283b8da524f12ecd61`. Foram aplicados somente
os commits locais `9cbab15`, `d44ad8e` e `320e8d0`, depois a ligação mínima do
caller. F5V/F5PT e os arquivos de engine, canais e instalação foram preservados.

## Comportamento entregue

O adapter seleciona a ponte somente com `zinom.bronze_ingest_enabled: true`
(booleano estrito), `zinom.enabled` ativo e token configurado. A chave nova tem
default **false** mesmo quando ausente; nenhuma configuração instalada foi alterada.
`zinom.workspace` deve vir de configuração explícita e autorizada. Ausência ou
valor inválido fica em erro recuperável, sem rede, sem adivinhar destino por
título, agenda, email ou conta. `zinom.account_id` é opcional; quando ausente,
somente a fronteira autenticada F4 resolve a conta.

A unidade de origem é a gravação nativa: `recordings[].job_id`, persistido antes
da cópia do áudio. O caller lê o job correspondente em `.jobs/<id>.json`, confere
identidade, estágio e provedor e envia **todo** o seu `transcript`, mantendo
Unicode e CRLF. `source_id` e `proveniencia.origem_id` derivam desse ID, não do
slug, título, hash do áudio ou texto. Correções e retries conservam a origem;
conteúdo e metadados versionam o envelope. Legado sem ID nativo falha fechado e
permanece disponível para recuperação, sem inventar identidade pelo nome do arquivo.

O envelope marca a transcrição como projeção do áudio. O resumo Silver não é
usado como original; Gold não gera fatos sem passagem citada. Convidados não são
promovidos a presentes. `facts` fica vazio. Texto acima de 8 MiB é recusado,
sem truncamento. Data legada sem fuso conserva apenas a data, sem inferir offset.

## Durabilidade e recuperação

`prepare_transcript_upload` publica o pedido antes da rede. Cada revisão tem seu
checkpoint em `.brain-ingest/`, com instante congelado. O sync mantém o lock da
reunião; o engine existente já mantém esse lock. A ponte acrescenta lock da
entrega e lock por origem em `.brain-ingest-locks/`, compartilhado pelas reuniões.

Depois de qualquer tentativa, somente `idempotency_key` é enviada na consulta.
Apenas `unknown_idempotency_key` estruturado de `brain_ingest` permite reenviar o
payload congelado. Erro genérico, autorização revogada e HTTP 404 não autorizam
reenvio. O transporte não repete automaticamente `brain_ingest` quando a sessão
falha. Destino e vínculo da credencial são persistidos antes da rede: endpoint,
workspace, conta explícita e SHA-256 do token, nunca o token. Trocar um desses
valores exige recuperação da configuração original; não redireciona uma tentativa
ambígua. A rotação de credencial fica, portanto, em erro recuperável até reconciliação.

Recibos exigem tipos estritos, IDs inteiros positivos dentro do limite seguro do
JavaScript, checkpoint entre 0 e 2 e `completed` somente no checkpoint 2. Consulta
exige origem, replay e identidade do job/revisão consistentes. ACK de fila não é
indexação: `pending`, `processing` e `retry` continuam pendentes; `failed` fica em
erro. `tombstoned`, `source_tombstoned` tipado e `superseded` são terminais, sem
reenviar, `remember` ou `brain_fact`. Exclusão encontrada no histórico local também
impede reingestão da mesma origem com texto alterado.

O resultado usa `zinom.source.transport = bronze` e `source.revisions`, campos
preservados pelo storage existente. A fila manual e a automática calculam a
revisão **atual** dos jobs em disco: um checkpoint antigo completed não conclui a
revisão nova. O histórico da ponte também impede fallback legado após desligar a
flag, inclusive quando o processo morreu antes de atualizar `metadata.json`.
Falha de uma reunião não interrompe as seguintes.

## Prova reproduzível

```sh
python3 -m unittest tests.test_bronze_ingest tests.test_bronze_sync tests.test_sync tests.test_retry_queue tests.test_zinom_adapter -q
python3 -m unittest discover -s tests -q
python3 -m tests.verify_bronze_f4 /caminho/zinom-u-f4-ingestao
```

A terceira prova exige fonte F4 limpa em
`f45050b3d24f70b512220bc637541b2bf6c93ff6` e `tsx` já disponível. Executa o parser
TypeScript real, sem build nem banco, com ambiente sem credenciais e payload
sintético: 136.000 bytes Unicode/CRLF, duas revisões, texto/hash exatos e origem
estável. O teste HTTP usa apenas loopback e autenticação fictícia para simular
aceitação seguida de perda da resposta. Os testes do caller usam disco temporário
e cobrem locks, fila, estados, revisão atual e persistência do recibo.

A revisão independente de outra família e os gates de composição/ativação são do
coordenador, antes da operação VERMELHA. Esta prova local não atesta consulta real
no MCP, implantação nem aceitação de produto no XPS.

## Reversão antes de qualquer operação

Desligar `zinom.bronze_ingest_enabled`, preservar `.brain-ingest/`,
`.brain-ingest-locks/`, `.jobs/`, transcrições e áudio. Não voltar a uma versão que
perca a proteção contra fallback legado enquanto houver origens recebidas pelo
F4. Recuperação consulta as chaves congeladas no mesmo destino antes de qualquer
reenvio. Nunca apagar checkpoints ou reenviar origens terminais para “destravar”.
