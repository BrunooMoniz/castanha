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

O envelope pode omitir `account_id` na fronteira MCP, que deve preenchê-lo pela
sessão autenticada antes da validação canônica. Esse ajuste ainda depende do F4.
Conta explícita deve ser conferida pelo servidor, nunca inferida do email.

Provas: 15 testes novos de integridade, retry, ACK e corrupção. O teste HTTP local
usa o transporte MCP real: aceita o pedido, fecha a conexão antes do recibo e
confirma que outra instância do cliente reutiliza a mesma chave e um único job.
Com dois testes adicionais de notificação ausente, são 208 testes Python verdes.
Um envelope sintético de 136.000 bytes produzido pelo Python passou
no `parseBronzeRequest` real do F4 na VPS com conta de fixture explícita. Nenhum
conteúdo pessoal foi enviado e nenhum banco de produção foi conectado.

Reversão futura: desligar a seleção do novo transporte, preservar `.brain-ingest`
e não reenviar pelo legado origens já recebidas pelo F4. Checkpoints e originais
não devem ser apagados ao reverter código.
