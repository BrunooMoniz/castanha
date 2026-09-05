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

## Integração com a tarefa de agenda e reuniões longas

A worktree de origem terminou limpa em `9ca9c6a`. Esse commit foi integrado sem editar a worktree
do usuário. Permanecem os eventos de dia inteiro, ações do painel, chunking da Groq, notas em partes,
as correções da revisão anterior e todos os casos de teste das duas linhas.

O transporte SSH usa o contrato novo de job persistente: conexão limitada a 15 s, upload a 30 s,
worker desacoplado e limitado remotamente entre 30 minutos e 3 horas conforme a duração do áudio.
Não se mata nem apaga o job por uma perda de conexão. Os quatro testes antigos de transporte foram
adaptados a esse contrato: continuam verificando limites, falhas e recuperação; agora também exigem
preservação do original, ausência de limpeza destrutiva e reutilização do resultado remoto.
O teste de Bronze anterior à transcrição passa a identificar o arquivo pelo job em vez de exigir
o nome legado `audio.ogg`. Os seis contratos independentes permanecem byte a byte intactos.

O botão de retry das reuniões novas retoma `.jobs`, sem passar pelo append legado, e libera o
estado de finalização interrompida. Os campos por gravação alimentam o indicador de retry do painel.
O caminho legado mantém lock por reunião, escrita atômica e persistência imediata do recibo.
Suíte integrada: 187 testes verdes no XPS, sem instalar nem publicar.

Complemento: o finalizador registra `processing_pid`. A retomada automática pode recuperar o
estado `processing` se esse processo comprovadamente não existir mais; não interfere em processo
vivo ou em estado legado sem identidade suficiente. Mais dois testes, total 189 verdes.
