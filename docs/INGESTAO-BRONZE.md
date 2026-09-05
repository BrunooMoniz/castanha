# Ponte Castanha para ingestão Bronze

Risco VERMELHO quando ativada, pois escreve memória real. Este módulo ainda não é
chamado pelo daemon, CLI ou fluxo instalado. A ativação depende do servidor F4,
revisão cruzada e integração do recibo com a fila e o painel.

`build_transcript_request` conserva integralmente o texto e calcula SHA-256 dos
bytes UTF-8. A transcrição é marcada como projeção do áudio. Não publica o resumo
como original, não promove convidados a presentes e não envia fatos sem passagem
de origem verificável. Resumo e Gold continuam locais até terem derivação ligada
à origem no servidor. A ausência de fuso no formato antigo preserva apenas a data,
sem presumir o fuso da máquina que está retomando a fila.

`prepare_transcript_upload` grava o pedido antes da rede. O chamador precisa manter
o lock da reunião. Cada revisão tem checkpoint próprio, que é reutilizado mesmo
se o relógio mudar no retry; registros corrompidos são preservados e recusados.

`submit_transcript_upload` usa o transporte MCP existente. Resposta perdida conserva
a chave e o payload; nunca cai para `remember`. ACK pending/processing/retry não é
sucesso de indexação. Apenas completed confirma o processamento da transcrição,
não a entrega de resumo ou fatos. O aceite final ainda exige consulta real no MCP.

O envelope omite `account_id` por padrão: F4 o preenche pela sessão autenticada.
O chamador deve passar `workspace` explícito de uma configuração autorizada, nunca
inferido do título ou email. `source_id` e `proveniencia.origem_id` são estáveis por
reunião; o hash identifica a revisão, não cria outro documento a cada correção.

Após qualquer tentativa, consulta somente pela chave. Só o erro estruturado
`unknown_idempotency_key` de `brain_ingest` autoriza reenviar o envelope congelado,
sem trocar workspace. Consulta exige identidade da origem e do job, tipos estritos,
checkpoint e estado válidos. `source_tombstoned` tipado e `superseded` são terminais;
HTTP 404 ou outra tool não prova exclusão. Nenhum desses caminhos usa `remember`.

O chamador ainda precisa escolher o checkpoint da REVISÃO ATUAL: um job antigo
completed pode continuar completed depois de outro substituí-lo. Nunca agregar
esse recibo antigo como conclusão da reunião atual. A integração de sync/adapter
e essa prova ponta a ponta ainda estão pendentes, assim como destino real de Nora.

Provas atuais: 23 testes de integridade, retry, ACK, autorização e corrupção. O teste HTTP local
usa o transporte MCP real: aceita o pedido, fecha a conexão antes do recibo e
confirma que outra instância consulta pela mesma chave e um único job.
O pedido atualizado passou no parser TypeScript REAL do F4 f45050b: 136.000 bytes
Unicode/CRLF, texto e hash exatos, workspace explícito, source_id/origem_id estáveis,
conta preenchida só na fronteira sintética. A revisão auxiliar local não encontrou
bloqueador neste diff. Nenhum conteúdo pessoal foi enviado e nenhum banco de
produção foi conectado. Essa prova não substitui review Claude ou integração real.

Reversão futura: desligar a seleção do novo transporte, preservar `.brain-ingest`
e não reenviar pelo legado origens já recebidas pelo F4. Checkpoints e originais
não devem ser apagados ao reverter código.
