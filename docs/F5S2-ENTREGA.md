# F5S2: recibo inválido e pendência legada

Risco: AMARELO. Base: `f50a643`. Implementador: Codex.

- Resposta vazia, JSON malformado ou identificador inválido de `brain_update` deixa erro recuperável com motivo fixo, sem reproduzir o conteúdo da resposta. O id anterior permanece apenas como destino para outra tentativa, sem virar recibo de sucesso. Não há callback de entrega nem criação de nota substituta.
- A fila volta a tentar atualizar a nota após esse erro, mesmo quando existem fatos aguardando linhagem. A espera por suporte do servidor continua valendo para a nota efetivamente entregue.
- Item legado `skipped` por ausência de credencial aparece como envio pendente, com ação de sincronizar e aviso em vez do glifo de concluído. Se houver transcrição a reprocessar, essa ação mantém prioridade, mas o glifo continua indicando pendência. Descarte deliberado e tombstone continuam fora da fila.

## Provas e limites

A suíte original passou com 196 testes antes das alterações. A suíte final passa com 198 testes, incluindo dois novos testes de regressão: 13 respostas inválidas exercitam transporte, adaptador, persistência e nova tentativa automática; o caso legado confirma permanência na fila antes e depois de tentar sem credencial. Os seis contratos independentes permanecem byte-idênticos, com os hashes registrados em `F5-ENTREGA.md`.

Comandos: `python3 -m unittest discover -s tests`, focados `test_sync.py`, `test_zinom_adapter.py`, `test_tombstone_errors.py`, `test_retry_queue.py` e `*contract.py`; `git diff --check` e `git diff --check b97131e`.

Os 21 casos de `tests/qml/tst_delivery.qml` passaram executando as funções e tabelas JavaScript em Node. Isso verifica a lógica existente de estado, sem instalar dependência; não é renderização Qt. QML real no XPS e revisão Claude em sessão fresca ficam a cargo do coordenador. Nenhum AGY ou OpenCode executado.

Não houve instalação, ativação da bridge/F4, mudança de main, acesso a dados reais ou publicação pública. O único remoto foi conferido como público; por orientação do coordenador, a entrega fica em commit local, árvore limpa e bundle `/root/castanha-f5s2-private.bundle`, verificado com `git bundle verify` dentro do checkout. Push bloqueado por ausência de remoto privado.
