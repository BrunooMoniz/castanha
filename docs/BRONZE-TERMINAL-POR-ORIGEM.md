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
