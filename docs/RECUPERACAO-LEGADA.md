# Recuperação explícita de gravação legada

Risco VERMELHO na operação. Nenhum acervo real é alterado pela instalação deste
módulo. O comando não é executado automaticamente pelo daemon nem pela fila.

## Critério de aceite

Uma única gravação legada pode ganhar identidade explícita de migração e ser
enviada integralmente ao Bronze, sem alterar os originais, inventar job histórico
ou publicar novamente após perder resposta, destino ou evidência terminal.

## Uso e limites

`python3 -m castanha.legacy_recovery /caminho/bronze/reuniao`

Esse comando somente cria ou valida `.legacy-recovery/manifest.json`. Não chama
o Zinom. Exige `audio.ogg` Ogg/Opus regular, `transcript_raw.txt` UTF-8 integral e
metadata de uma única gravação sem job nativo. Múltiplas gravações, mock, fluxo
nativo existente ou memória já recebida exigem reconciliação específica.

UUID novo identifica a migração, nunca um job histórico. Hashes/tamanhos de áudio,
texto e metadata demonstram integridade. Provedor global legado fica identificado
como não verificado por gravação. A transcrição não é validada semanticamente
contra o áudio por esta ferramenta; é preservada como projeção legada.

`submit_legacy_recovery(bronze, client, workspace=..., account_id=...)` exige
manifesto existente e destino autorizado explícito. Sua chamada operacional fica
condicionada ao F4H, backup, revisão e canário. O endpoint autenticado resolve a
conta. Não há destino deduzido por título, calendário ou nome do arquivo.

O envelope usa identidade `castanha:<sha256(migration:UUID)>` e referência ao SHA-256
do áudio, sem caminho privado nem nomes de convidados. O áudio não é enviado;
o texto integral é enviado como projeção, com fatos vazios. A referência preserva
vínculo ao original local, não afirma que o áudio está armazenado no servidor.

Pedido, destino e tentativa são persistidos antes da rede. Retomada usa apenas
a chave, salvo ausência tipada da chave no servidor, conforme o contrato F4.
Destino ou checkpoint perdido após tentativa bloqueia, não recria publicação.
Mudança no manifesto, originais, credencial ou destino exige reconciliação.

## Prova e reversão

`python3 -B -m unittest tests.test_legacy_recovery`

Fixtures com FFmpeg real e cliente MCP sintético. Não usam Groq, Whisper, tokens
reais, gravação pessoal ou exclusão em produção.

Antes de operação real, fazer backup dos originais e do diretório de recuperação.
Para interromper, parar de chamar a ferramenta e preservar manifesto, destino e
diretório `uploads` (checkpoints e ledger terminal independente). Não apagar esses
arquivos, não gerar UUID substituto e não usar remember
como fallback. Recuperar evidências do backup antes de qualquer nova tentativa.
Nenhuma instalação, migração de dados real ou publicação faz parte deste commit.
