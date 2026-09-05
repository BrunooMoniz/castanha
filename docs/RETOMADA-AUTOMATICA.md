# Retomada automática, complemento da F5

Risco: AMARELO para código com ativação desligada; VERMELHO para ativar processamento e envio do acervo real.

O daemon pode lançar uma rodada de retomada em processo separado. Ele não espera a transcrição,
não lança dois filhos simultâneos e não inicia rodadas durante gravação, pausa ou finalização.
Um lock no Bronze impede concorrência mesmo após reiniciar o daemon. O processo filho já iniciado
pode terminar depois de o daemon sair; o checkpoint permite recuperar uma interrupção.

Cada rodada tenta até cinco reuniões. O prazo da próxima tentativa é publicado atomicamente ANTES
de chamar o processamento: começa em 60 segundos, dobra e fica limitado a 15 minutos. O arquivo
`.sync-retry.json` fica junto ao original e sobrevive ao processo. Uma reunião com erro não impede
as posteriores; metadados ou checkpoints inválidos são preservados, nunca substituídos por sucesso.

Tombstones são respeitados mesmo se houver jobs pendentes. Uma nota já entregue, com fatos esperando
suporte a linhagem, não é reenviada automaticamente só para tentar resolver essa capacidade ausente.
O comando manual `sync --all` continua disponível, ignora o prazo automático e reporta erros por item.

## Ativação e reversão

`sync.auto_retry_enabled` é **false por padrão**. Não ativar antes da revisão independente e do
contrato F4 de identidade/idempotência no servidor. O remember legado ainda não distingue resposta
perdida de pedido não recebido, portanto repetir automaticamente não é uma garantia de ausência de
duplicatas. A ativação final deve incluir esse cenário real e a retomada da Nora Weekly de 05/09,
sem truncar sua transcrição nem suas notas.

Depois dessas provas, configurar `sync.auto_retry_enabled: true` no mecanismo de instalação
health-gated. Para reverter, desligar a opção e reiniciar somente o daemon Castanha. Um filho em
andamento continua sob o lock; antes de uma intervenção nele, conferir o PID e sua operação.
Não apagar originais, `.jobs`, `.sync-retry.json` ou recibos de entrega.

## Evidência local

- Base recebida: `b59ced55`, 112 testes verdes no XPS.
- Reprodução: a fila manual abortava na primeira exceção; não existia retomada automática no daemon.
- Complemento: 12 testes adicionais em `tests/test_retry_queue.py`, total 124 verdes.
- Contratos externos de integridade permanecem intactos.
- Ensaio opt-in `PYTHONPATH=. python3 -B tests/qa_pipewire_capture.py` usa PipeWire e FFmpeg reais
  com dois sinks sintéticos, sem acessar microfone físico nem mudar defaults.
- Nenhuma configuração instalada, gravação pessoal ou memória remota alterada por esta entrega.
