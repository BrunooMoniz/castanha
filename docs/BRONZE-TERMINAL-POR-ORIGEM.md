# Exclusão por gravação, sem bloquear novas gravações

Risco: VERMELHO na operação. Esta alteração não ativa a ponte nem instala o plugin.

## Critério de aceite

Depois que a gravação A recebe exclusão ou revisão substituída, uma gravação B
independente na mesma reunião pode ser entregue. A não pode ser republicada.
Reiniciar o chamador não altera esse resultado. Recibo terminal sem checkpoint
correspondente interrompe a entrega, inclusive em tentativas posteriores.

## Correção

O estado agregado da reunião não substitui os recibos por origem da ponte Bronze.
As exclusões legadas sem recibos por gravação continuam bloqueando a reunião.
Os checkpoints terminais são conferidos antes da rede; falhas ou desativação da
ponte preservam esses recibos. Texto alterado de uma origem excluída recebe o
mesmo estado terminal local, sem novo envio.

## Prova reproduzível

`python3 -B -m unittest tests.test_bronze_sync tests.test_bronze_ingest`

Inclui duas origens, tombstone e superseded, novo chamador, checkpoint perdido,
tentativas repetidas e texto alterado depois da exclusão. Fixtures temporárias
e cliente MCP sintético, sem transcrições pessoais ou exclusões no Zinom real.

## Reversão antes da ativação

Desativar `zinom.bronze_ingest_enabled`, preservando `.brain-ingest`, `.jobs`,
recibos, áudios e transcrições. Não apagar checkpoints nem restaurar fallback
`remember`. Compor e revisar o candidato completo antes de instalar ou publicar.

Revisão nesta entrega: família Codex autorizada explicitamente pelo usuário.
Isso não equivale a revisão entre famílias nem a verificação em produção.

## F5B4: checkpoint corrompido e destino divergente

Base exata: `57c1e276d9238e4afae093494f593a1d17c8233c`. Risco VERMELHO,
por persistência da evidência de exclusão e vínculo ao destino autorizado.

O P1 foi reproduzido na base com o chamador real e MCP sintético: trocar a
origem no checkpoint de A e depois alterar seu texto produzia `ok`, com um
novo upload integral. O P2 também foi reproduzido: endpoint ou token alterado
não entrava no inventário, embora o envio manual já recusasse a divergência.

A ponte agora verifica todos os checkpoints antes de confiar em sua origem:
chave do pedido, hash do texto, fingerprint do nome, proveniência, origem do
recibo e identidade remota precisam concordar. Isso inclui o checkpoint salvo
após uma falha antes de atualizar os metadados. Arquivo ausente, corrompido ou
conflitante conserva a evidência disponível, retorna erro e permanece na fila.
Nenhuma revisão é criada para contornar a falha.

`terminal-evidence.json` guarda separadamente origem, chave e recibo terminal
por checkpoint. A gravação atômica desse registro precede a atualização terminal
do checkpoint. Se o processo morrer entre as duas gravações, a divergência
bloqueia a retomada antes da rede, exigindo recuperação explícita. O marcador
`terminal_evidence` detecta a perda posterior do registro. Terminais anteriores
à mudança só ganham esse registro após validar integralmente a evidência antiga.
Não se presume que um arquivo faltante significa que a gravação nunca foi enviada.

A origem terminal, incluindo `superseded`, é consultada antes de preparar outro
checkpoint. Alterar o texto de A preserva seu recibo e seus arquivos; B com
identidade nativa independente continua enviável quando o histórico está íntegro.
Se o histórico da reunião estiver corrompido, toda essa reunião fica bloqueada;
outras reuniões continuam na fila. Esse bloqueio conservador evita decidir a
qual gravação pertence uma evidência cuja origem não é mais confiável.

O inventário compara endpoint, workspace, conta e impressão da credencial com
`destination.json`. Rotação de token fica visível como pendência e não cria
origem, chave ou destino novos. Não há migração automática de credencial:
sem prova de que o token novo corresponde à autorização anterior, o envio é
recusado. Restaurar a configuração congelada permite retomar pela mesma chave.
A perda de `destination.json` com histórico também bloqueia, inclusive com flag
OFF, sem cair no transporte legado. O registro não guarda o token em texto.

Gate reproduzível, sem rede:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m tests.verify_bronze_f5b4
```

Além dos testes da ponte, inclui os testes locais de sincronização e adapter.
`socket.connect` e `socket.bind` estão proibidos durante o gate. O teste existente
`test_real_http_lost_response_reuses_one_server_job` exige socket HTTP e não é
executado nessa seleção; continua intacto para a suíte com slot/autorização de
rede local. A suíte completa não foi executada neste despacho.

A revisão fresca do candidato corrigido é uma etapa posterior do coordenador.
Esta entrega não reutiliza o parecer FAIL anterior como aprovação e não atesta
produção. Reversão: desligar a flag, preservar todos os registros e retomar o
commit anterior somente após avaliar a compatibilidade da evidência persistida;
não remover o registro independente para desbloquear um reenvio.

Resultado local F5B4: **92 testes focados passaram em 46,829 segundos**, com
connect/bind bloqueados. Não houve instalação, deploy, chamada MCP real,
uso de acervo pessoal ou escrita de memória durante a validação.
