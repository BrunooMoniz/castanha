# F5P4: instalação recusa ausência de prova

Risco VERMELHO. Implementador: Codex. Nenhuma instalação executada, nenhum
processo real sinalizado, nenhuma alteração em XPS, dados pessoais ou main.

- O instalador reconhece o CLI antigo chamado por caminho relativo. Um CLI vivo
  impede a troca, inclusive quando começa a captura depois da primeira observação.
- Diretório de trabalho apagado não equivale a processo morto. Erros de leitura
  e enumeração recusam a instalação; só a ausência da própria entrada do PID
  permite ignorá-lo.
- O início de captura agora segura o mesmo lock exclusivo do instalador, desde
  a leitura do estado até a publicação do PID. Contenção recusa com mensagem e
  erros de áudio liberam a trava sem serem ocultados.
- O preflight executa o início de captura do candidato e do runtime anterior em
  ambiente temporário. Recusa comentários, constantes sem uso, outro lock,
  lock compartilhado ou soltura antecipada. Não cria áudio real.
- A primeira transição desde `b97131e` é bloqueada antes de qualquer mutação:
  aquele runtime não coopera com a trava e cercar somente os links não impediria
  novos starts diretos no checkout. O coordenador separou essa transição em
  F5PT, que exige cerca e rollback próprios. F5P4 não é instalável/live no XPS.

Reversão preservada: journal antes das mutações, restauração durável dos dois
links e reinício somente do daemon identificado por PID, início, boot, cwd,
executável e usuário, com pidfd. Falha de reversão permanece explícita.

Provas duráveis em `tests/test_deployment.py`: processos e barreiras isolados,
lock real em subprocessos, recusa inicial antes de links/journal/daemon/estado,
captura tardia, falhas de `/proc` e os contratos anteriores de rollback/identidade.
O `b97131e` real também foi extraído em diretório temporário e recusado pelo
preflight comportamental.

Validação em 05/09/2026: 41 testes focados e 237 testes na suíte completa
passaram; `git diff --check` sem erros. Baseline focado anterior: 32 testes verdes.

Entrega somente por commit local e bundle privado, por instrução do coordenador:
`origin` foi confirmado público e não recebeu push. Revisão final obrigatória
em Claude fresco, a ser despachada pelo coordenador sobre o SHA entregue;
nenhum passe de revisão final é alegado neste changelog.
